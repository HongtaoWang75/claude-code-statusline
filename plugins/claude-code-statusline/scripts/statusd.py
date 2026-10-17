#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态栏常驻 daemon：给原生 shim（dspg-status.exe）当后端。

为什么存在：CC 每次刷新状态栏都要起「bash → python → 脚本」一整条链（实测 ~294 ms，
还各带一个控制台宿主 conhost）。机器一卡这条链就会超过刷新间隔 → 被 CC 取消 →
taskkill 清进程树 → 僵尸堆积 → 内存告急 → 更卡（2026-10-16 实测雪崩）。

本 daemon 把这些固定开销付一次：
- 监听 127.0.0.1:18799，收到一行 statusLine 的 stdin JSON → 进程内复用
  deepseek-status.py 的 render_line() 直接算 → 回一行
- 每帧把结果写一份到 ~/.claude/.dspg_line.txt（daemon 不在时 shim 拿它兜底）
- 看门狗线程每 30 秒自查：taskkill 僵尸 > 40 个 → 自动把 refreshInterval 降回 5
  秒并把原因写日志（防雪崩复发；这是"卡顿时自动降级"的保险）
- 生命周期：连续 5 分钟见不到 claude.exe 就退出（shim 会在下一次刷新时把它拉起来）

用法：
    pythonw statusd.py          # 正常启动（无窗口）
    python statusd.py --once    # 前台跑一次（调试）
"""
import importlib.util
import json
import os
import pathlib
import socket
import threading
import time

PORT = 18799
HOME = pathlib.Path(os.path.expanduser("~"))
HERE = pathlib.Path(__file__).resolve().parent
LINE_FILE = HOME / ".claude" / ".dspg_line.txt"
LOG_FILE = HOME / ".claude" / ".dspg_statusd.log"
SESSIONS_FILE = HOME / ".claude" / ".dspg_sessions.json"   # 活会话 → 工作目录（重启脚本用）
SESSIONS = {}
_LAST_SESS_SAVE = 0.0
LOCK = threading.Lock()
WATCH_INTERVAL_S = 10         # 看门狗周期：10 秒（回收挂死的 taskkill 要够勤）
IDLE_EXIT_AFTER = 30          # 连续这么多轮没见到 claude.exe（5 分钟）就退出


def log(msg):
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 200_000:
            LOG_FILE.write_text("", encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def load_status_module():
    spec = importlib.util.spec_from_file_location("dspg_status", HERE / "deepseek-status.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = load_status_module()


def iter_procs(name):
    """返回某进程名的全部 PID（Toolhelp32 快照，不依赖 WMI——内存吃紧时 WMI 会瘫）。"""
    pids = []
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]

        k = ctypes.windll.kernel32
        snap = k.CreateToolhelp32Snapshot(0x2, 0)
        if snap in (-1, 0xFFFFFFFF):
            return pids
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k.Process32First(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.decode("ascii", "ignore").strip().lower() == name.lower():
                pids.append(e.th32ProcessID)
            ok = k.Process32Next(snap, ctypes.byref(e))
        k.CloseHandle(snap)
    except Exception:
        pass
    return pids


def proc_count(name):
    return len(iter_procs(name))


def reap_taskkills():
    """自动抑制 CC 进程清理的副作用：直接 TerminateProcess 掉挂死的 taskkill.exe。

    CC 在取消/停止命令时会 spawn `taskkill /T /F` 清进程树；目标一时杀不掉时这些
    taskkill 会挂住不退出，几十秒就能堆积到 300+ 个（连带 300 个 conhost，吃掉 7.5 GB
    → 内存告急 → 机器越卡越积，2026-10-17 实测）。每 30 秒回收一次，它们就永远堆不起来；
    杀掉 taskkill 后它独占的 conhost 会随客户端消失自动退出。
    """
    n = 0
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for pid in iter_procs("taskkill.exe"):
            h = k.OpenProcess(0x0001, False, pid)   # PROCESS_TERMINATE
            if h:
                if k.TerminateProcess(h, 1):
                    n += 1
                k.CloseHandle(h)
    except Exception:
        pass
    return n


def try_downgrade(taskkills):
    """僵尸太多 → 把 refreshInterval 降回 5 秒（自动止血，只降不升，用户可手动改回）。"""
    try:
        p = HOME / ".claude" / "settings.json"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        if (cfg.get("statusLine") or {}).get("refreshInterval") == 1:
            cfg["statusLine"]["refreshInterval"] = 5
            p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"⚠ taskkill 僵尸 {taskkills} 个 → 自动把 refreshInterval 降回 5 秒（防雪崩）")
    except Exception as e:
        log(f"downgrade failed: {type(e).__name__}: {e}")


def watchdog():
    """每 10 秒：① 挂住的 taskkill 超阈值就自动回收；② 回收后仍连续两次超阈值才降级；
    ③ 长时间没有 claude.exe 就退出。

    阈值刻意放宽：工具调用密集时短时出现几十个 in-flight 的 taskkill 是正常的，
    真正雪崩是 200~300+ 且不退。单次超阈值就降级会在正常波动时误伤
    （2026-10-17 00:53 就是这么把 1 秒刷成 5 秒的）。
    """
    idle = 0
    strikes = 0
    while True:
        time.sleep(WATCH_INTERVAL_S)
        tk = proc_count("taskkill.exe")
        if tk > 60:   # 正常波动一般 0~30；超过 60 说明开始挂了 → 立刻回收
            reaped = reap_taskkills()
            log(f"taskkill 挂住 {tk} 个 → 自动回收 {reaped} 个（抑制 CC 清理的副作用）")
            tk = proc_count("taskkill.exe")
        if tk > 100:
            strikes += 1
            if strikes >= 2:
                try_downgrade(tk)
                strikes = 0
        else:
            strikes = 0
        if proc_count("claude.exe") > 0:
            idle = 0
        else:
            idle += 1
            if idle >= IDLE_EXIT_AFTER:
                log(f"连续 {IDLE_EXIT_AFTER * WATCH_INTERVAL_S // 60} 分钟没见到 claude.exe，退出")
                os._exit(0)


def note_session(payload):
    """记录活会话的工作目录：重启脚本靠它把窗口"原样"开回来。"""
    global _LAST_SESS_SAVE
    sid = str(payload.get("session_id") or "")
    if not sid:
        return
    ws = payload.get("workspace") or {}
    now = time.time()
    SESSIONS[sid] = {
        "cwd": ws.get("current_dir") or ws.get("cwd") or "",
        "transcript": payload.get("transcript_path") or "",
        "ts": now,
    }
    if now - _LAST_SESS_SAVE > 5:          # 每 5 秒最多落盘一次
        _LAST_SESS_SAVE = now
        try:
            live = {k: v for k, v in SESSIONS.items() if now - (v.get("ts") or 0) < 600}
            SESSIONS_FILE.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def handle(conn):
    try:
        conn.settimeout(15)
        chunks, total = [], 0
        while True:
            b = conn.recv(65536)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > (1 << 20):
                break
        raw = b"".join(chunks).decode("utf-8", "replace") or "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):   # 收到 null/数组等非法载荷时别炸
            payload = {}
        note_session(payload)          # 记录活会话的工作目录（重启脚本用）
        with LOCK:                     # 多会话并发时串行渲染（每帧只要几十毫秒）
            MOD._AUTOCOMPACT = None    # settings.json 改了也立刻跟上
            line = MOD.render_line(payload)
        try:
            LINE_FILE.write_text(line + "\n", encoding="utf-8")
        except Exception:
            pass
        conn.sendall((line + "\n").encode("utf-8"))
    except Exception as e:
        log(f"handle failed: {type(e).__name__}: {e}")   # 别静默失败，否则只能看到空状态栏
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # ⚠️ 单例保护必须用 SO_EXCLUSIVEADDRUSE：Windows 上 SO_REUSEADDR 允许**重复绑定**
    # 同一端口（和 Linux 相反），用它做"端口被占就退出"会失效 → daemon 越起越多。
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        pass
    try:
        srv.bind(("127.0.0.1", PORT))
    except OSError:
        return 0                       # 端口被占 = 已有 daemon 在跑，安静退出
    srv.listen(64)
    log(f"start pid={os.getpid()} port={PORT}")
    threading.Thread(target=watchdog, daemon=True).start()
    while True:
        try:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
        except Exception:
            time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
