#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deepseek-price-guard 状态栏配色 / 倒计时演示

每一行都是**真实脚本**（deepseek-status.py）的输出：伪造一份 statusLine 的
stdin JSON + 伪造 transcript，让数字口径和真实状态栏完全一致（28 轮、约 105k
上下文）。

  ① 余额渐变     ¥1.00 转黄 → ¥0.00 转红（>¥1 不染色）
  ② 上下文渐变   0 tok 绿 → 1M tok 红（绿→黄→红交通灯色阶）
  ③ 缓存命中     <95% 转黄 → ≤90% 转红
  ④ 输出速率     <150 tok/s 转黄 → ≤100 tok/s 转红
  ⑤ 峰谷倒计时   天:时:分:秒（满 24h 才出「天」），turn 数 >1 自动加 s
  ⑥ 动画         余额从 ¥1 掉到 ¥0 的过程

跑法：python demo-statusline.py      （Windows Terminal 里看，24 位色才够顺滑）
"""
import importlib.util
import itertools
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = pathlib.Path(__file__).resolve().parent
STATUS_PY = HERE / "deepseek-status.py"
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
BAR = "▌"
_TAG = itertools.count()


def enable_vt():
    """老式 conhost（非 Windows Terminal）默认不解析 ANSI，手动打开 VT 处理。"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def load_status_module():
    """按路径加载 deepseek-status.py（文件名带连字符，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("dspg_status", STATUS_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_fake_payload(tmp, hit=0.997, rate=178.0, ctx=105000):
    """伪造 transcript + statusLine stdin JSON。

    hit  → 缓存命中率（写进 transcript 的 input/cache_read 比例）
    rate → 目标 tok/s（反推 cost.total_api_duration_ms，28 轮 × 200 输出 token）
    ctx  → 当前上下文 token 数

    每次调用用**不同的文件名**：状态栏脚本按文件名缓存增量统计，同名文件换了
    内容会串数据。
    """
    per_out, turns, total_in = 200, 28, 104300
    cr = int(total_in * hit)
    inp = total_in - cr
    api_ms = int(round(per_out * turns / rate * 1000))
    tp = pathlib.Path(tmp) / f"demo-transcript-{next(_TAG):02d}.jsonl"
    lines = [
        json.dumps({"type": "assistant", "message": {"id": f"msg_{i:03d}", "usage": {
            "input_tokens": inp, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cr, "output_tokens": per_out}}})
        for i in range(turns)
    ]
    tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "transcript_path": str(tp),
        "model": {"display_name": "deepseek-flash[1m]"},
        "cost": {"total_api_duration_ms": api_ms},
        "context_window": {"total_input_tokens": ctx},
    }


def render(payload, balance=None, date=None, now=None):
    """跑真实脚本，返回它这一行输出（原样）。"""
    cmd = [sys.executable, "-X", "utf8", str(STATUS_PY)]
    if balance is not None:
        cmd += ["--balance", f"{balance:.2f}"]
    if date:
        cmd += ["--date", date]
    if now:
        cmd += ["--now", now]
    r = subprocess.run(cmd, input=json.dumps(payload).encode("utf-8"),
                       capture_output=True, timeout=30)
    return r.stdout.decode("utf-8", "replace").strip()


def row(mod, payload, balance, date=None, now=None, note=""):
    line = render(payload, balance=balance, date=date, now=now)
    col = mod.balance_color(balance) or DIM
    tag = f"  {DIM}{note}{RESET}" if note else ""
    print(f"  {col}{BAR}{RESET} {line}{tag}")


def strip(fn, lo, hi, n=48, left="", right=""):
    bar = "".join(f"{fn(lo + (hi - lo) * i / (n - 1))}█" for i in range(n))
    return f"\n  {DIM}{left}{RESET} {bar}{RESET} {DIM}{right}{RESET}"


def animate(mod, cycles=3):
    frames = [1.0 - i / 40 for i in range(41)]
    for _ in range(cycles):
        for v in frames:
            col = mod.balance_color(v)
            k = round(v * 30)
            sys.stdout.write(
                f"\r  余额  {col}¥{v:5.2f}{RESET}  "
                f"{col}{'█' * k}{RESET}{DIM}{'░' * (30 - k)}{RESET}   "
            )
            sys.stdout.flush()
            time.sleep(0.03)
        time.sleep(0.9)
    sys.stdout.write("\r" + " " * 72 + "\r")


def main():
    enable_vt()
    # 设置终端标题（便于在窗口列表里定位本演示窗口）
    sys.stdout.write("\033]0;DSPG 状态栏演示\007")
    sys.stdout.flush()
    mod = load_status_module()
    tmp = tempfile.mkdtemp(prefix="dspg-demo-")

    print()
    print(f"{BOLD}  deepseek-price-guard · 状态栏配色演示{RESET}")
    print("  " + "─" * 68)
    print(f"  {DIM}以下每一行都是真实脚本输出（伪造 stdin/transcript，数字口径不变）{RESET}")

    print(f"\n  {BOLD}① 余额：¥1.00 转黄 → ¥0.00 转红（>¥1 不染色）{RESET}\n")
    for b, note in [(1.20, ">¥1 不染色"), (1.00, "黄"), (0.85, ""), (0.70, ""),
                    (0.55, "橙"), (0.40, ""), (0.25, ""), (0.10, ""), (0.00, "红")]:
        row(mod, make_fake_payload(tmp), b, note=note)
    print(strip(mod.balance_color, 1.0, 0.0, left="¥1.00", right="¥0.00"))

    print(f"\n  {BOLD}② 上下文用量：0 tok 绿 → 自动压缩阈值红（分母 = CC autoCompactWindow）{RESET}\n")
    full = mod.autocompact_tokens()
    print(f"  {DIM}当前阈值：{full:,} tok（读 ~/.claude/settings.json 的 autoCompactWindow）{RESET}")
    for frac, note in [(0.10, "现在这档"), (0.25, ""), (0.50, "黄"),
                       (0.75, ""), (0.90, "进 90%，开始闪"), (0.99, "≈满格")]:
        row(mod, make_fake_payload(tmp, ctx=int(full * frac)), 11.09, note=note)
    print(strip(mod.ctx_color, 0, full, left="0 tok", right=f"{full:,} tok"))

    print(f"\n  {BOLD}③ 缓存命中：<95% 转黄 → ≤90% 转红{RESET}\n")
    for h, note in [(0.997, "现在这档"), (0.950, "黄"), (0.940, ""), (0.925, ""),
                    (0.910, ""), (0.900, "红"), (0.880, "≤90 仍红")]:
        row(mod, make_fake_payload(tmp, hit=h), 11.09, note=note)
    print(strip(lambda v: mod.warn_grad(v, 0.95, 0.90), 0.96, 0.89, left="96%", right="89%"))

    print(f"\n  {BOLD}④ 输出速率：<150 tok/s 转黄 → ≤100 转红{RESET}\n")
    for r_, note in [(200, ""), (149.5, "黄"), (140, ""), (125, ""),
                     (110, ""), (100, "红"), (85, "≤100 仍红")]:
        row(mod, make_fake_payload(tmp, rate=r_), 11.09, note=note)
    print(strip(lambda v: mod.warn_grad(v, 150.0, 100.0), 155, 95, left="155", right="95 tok/s"))

    print(f"\n  {BOLD}⑤ 峰谷倒计时：天:时:分:秒（满 24h 才出「天」）；turn >1 加 s{RESET}\n")
    row(mod, make_fake_payload(tmp), 11.09, date="2026-10-10", now="10:00", note="周六→周一 09:00")
    row(mod, make_fake_payload(tmp), 11.09, date="2026-10-09", now="18:30", note="周五晚→周一（2 天多）")
    row(mod, make_fake_payload(tmp), 11.09, date="2026-10-12", now="11:00", note="高峰中，剩 1 小时")
    row(mod, make_fake_payload(tmp), 11.09, date="2026-10-12", now="08:30", note="开盘前 30 分钟")

    print(f"\n  {BOLD}⑥ 余额下跌动画（¥1 → ¥0）{RESET}\n")
    animate(mod)
    print()

    # 清理：临时 transcript + 演示写入的统计缓存（真实会话的不动）
    shutil.rmtree(tmp, ignore_errors=True)
    for f in (pathlib.Path.home() / ".claude").glob(".dspg_stats_demo-transcript-*.json"):
        try:
            f.unlink()
        except Exception:
            pass

    if sys.stdin and sys.stdin.isatty():
        try:
            input("  演示结束，按回车关闭本窗口… ")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
