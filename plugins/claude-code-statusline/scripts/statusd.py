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
LOCK = threading.Lock()
IDLE_EXIT_AFTER = 10          # 每轮 30 秒，连续这么多轮没见到 claude.exe 就退出


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


def proc_count(name):
    """用 Toolhelp32 快照数进程——不依赖 WMI（内存吃紧时 WMI 会直接瘫掉）。"""
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
        if snap == -1 or snap == 0xFFFFFFFF:
            return -1
        n = 0
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k.Process32First(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.decode("ascii", "ignore").strip().lower() == name.lower():
                n += 1
            ok = k.Process32Next(snap, ctypes.byref(e))
        k.CloseHandle(snap)
        return n
    except Exception:
        return -1


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
    """每 30 秒：① 僵尸连续两次超阈值才自动降级；② 长时间没有 claude.exe 就退出。

    阈值和"连续两次"都刻意放宽：工具调用密集时短时出现几十个 in-flight 的 taskkill
    是正常的，真正雪崩是 200~300+ 且不退。单次超阈值就降级会在正常波动时误伤
    （2026-10-17 00:53 就是这么把 1 秒刷成 5 秒的）。
    """
    idle = 0
    strikes = 0
    while True:
        time.sleep(30)
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
                log(f"连续 {IDLE_EXIT_AFTER * 30 // 60} 分钟没见到 claude.exe，退出")
                os._exit(0)


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
