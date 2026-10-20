#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 峰谷定价 + 余额守护 hook（PreToolUse）

功能：
1. 高峰时段（北京时间 周一至周五 09:00-12:00、14:00-18:00，法定节假日全天
   空闲）阻止工具调用；
   首次拦截时弹 Popup 模态对话框（WScript.Shell.Popup）：
   - 点「是」→ 临时放行本会话 30 分钟（~/.claude/.dspg_override.json）
   - 点「否」/ 30 秒无响应 → 继续拦截
   - 注：Windows Toast 的协议激活在未打包应用下失效（按钮/正文点击均
     不触发，实测多次），故交互统一走 Popup，100% 可靠
2. 每次工具调用注入余额，低于阈值（¥5）输出省流警告
3. 高峰前 15 分钟提前警告
4. 拦截提示词携带当前时间、本段高峰剩余/结束时刻，并给出可直接使用的
   CronCreate cron 表达式（高峰结束后 5 分钟），便于模型精确定时到空闲时段

文件：
   放行状态  ~/.claude/.dspg_override.json     {session_id, expires}
   决策文件  ~/.claude/.dspg_decision.json     {result: yes/no, ts}
   按钮回调  ~/.claude/.dspg_action.ps1        （协议注册指向的 PowerShell）
   Toast脚本 ~/.claude/.dspg_toast.ps1         （UTF-8 BOM，自动生成）

测试：
    python price-guard.py --now 10:00 --weekday 0 --simulate-popup no  # 普通周一高峰+拒绝 → exit 2
    python price-guard.py --now 10:00 --date 2026-10-01 --simulate-popup no  # 国庆假期 → 空闲放行
    python price-guard.py --now 10:00 --weekday 0 --simulate-popup yes # 高峰+同意 → exit 0 并写放行
    python price-guard.py --now 10:00 --weekday 0                      # 高峰+真实弹窗（等用户点按钮）

逃生通道：环境变量 DSPG_BYPASS=1 时完全放行。
"""
import os
import sys
import json
import time
import pathlib
import argparse
import datetime
import subprocess

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 高峰时段（官方：周一至周五 09:00-12:00 / 14:00-18:00 北京时间，不含法定节假日；
# 其余时段——夜间、午休、周末及节假日全天——为空闲价，与 deepseek-status.py 一致）
PEAK_RANGES = [((9, 0), (12, 0)), ((14, 0), (18, 0))]
WARN_BEFORE_MIN = 15        # 高峰前提前警告的分钟数
WAKE_AFTER_PEAK_MIN = 5     # 拦截后建议的定时唤醒延迟（高峰结束后 N 分钟）
LOW_BALANCE = 5.0           # 余额低于此值（元）时每次工具调用提醒
OVERRIDE_MIN = 30           # 临时放行时长（分钟）
POPUP_WAIT_SEC = 30         # Popup 模态对话框等待秒数
DECISION_TIMEOUT = 30       # 等待用户点击 Toast 的秒数，超时视为拒绝
POPUP_TITLE = "DeepSeek 高峰拦截"
AUMID = "DeepSeekPriceGuard"  # Toast 左上角显示名（HKCU 注册，无需管理员）

HOME = pathlib.Path(os.path.expanduser("~"))
OVERRIDE_FILE = HOME / ".claude" / ".dspg_override.json"
DECISION_FILE = HOME / ".claude" / ".dspg_decision.json"
LAST_DECISION_FILE = HOME / ".claude" / ".dspg_last_decision.json"
MUTEX_NAME = "Local\\DSPG_Popup_Lock"   # 跨进程弹窗互斥：并发工具调用只弹一个窗
DECISION_REUSE_SEC = 60                 # 刚做过决定（含超时=拒绝）→ 60 秒内复用，不再弹
# 定时/唤醒类工具永远放行（详见 main）：被拦的话模型连"预约到空闲时段"都做不到
SCHEDULING_TOOLS = ("CronCreate", "CronList", "CronDelete", "ScheduleWakeup")
ACTION_PS1 = HOME / ".claude" / ".dspg_action.ps1"
POPUP_PS1 = HOME / ".claude" / ".dspg_popup.ps1"
BALANCE_CACHE = HOME / ".claude" / ".balance_cache.json"
HOLIDAYS_FILE = HOME / ".claude" / ".dspg_holidays.json"
TZ = datetime.timezone(datetime.timedelta(hours=8))

# 2026 年法定节假日（国务院办公厅国办发明电〔2025〕7号，2025-11-04 发布）——仅作 fallback。
# 日常读 ~/.claude/.dspg_holidays.json（scripts/fetch-holidays.py 从 timor.tech 定期抓取，
# 计划任务 "DSPG Holiday Fetch"）；缓存缺失/损坏时退回本表。
# 官方规则是「周一至周五（**不含中国法定节假日**）」；调休上班的周末（1/4、2/14、2/28、
# 5/9、9/20、10/10）按官方措辞「空闲时段包括周末全天」仍算空闲，故不收录。
BUILTIN_HOLIDAYS = frozenset(
    datetime.date(2026, mo, da)
    for mo, das in {
        1: range(1, 4),      # 元旦 1/1-1/3
        2: range(15, 24),    # 春节 2/15-2/23
        4: range(4, 7),      # 清明 4/4-4/6
        5: range(1, 6),      # 劳动节 5/1-5/5
        6: range(19, 22),    # 端午 6/19-6/21
        9: range(25, 28),    # 中秋 9/25-9/27
        10: range(1, 8),     # 国庆 10/1-10/7
    }.items()
    for da in das
)

# --weekday 测试参数映射到 2026-03-02（周一）起的一周：3 月无节假日，0-6 均可得到
# "普通工作日/普通周末"语义；要测节假日请用 --date 2026-10-01 之类。
PROBE_MONDAY = datetime.date(2026, 3, 2)

_HOLIDAYS_CACHE = None


def load_holidays():
    """法定节假日日期集合（date 对象）。优先读自动抓取的缓存，退回内置表。"""
    global _HOLIDAYS_CACHE
    if _HOLIDAYS_CACHE is None:
        days = set()
        try:
            data = json.loads(HOLIDAYS_FILE.read_text(encoding="utf-8"))
            days = {
                datetime.date.fromisoformat(s)
                for yd in (data.get("years") or {}).values()
                for s in yd
            }
        except Exception:
            days = set()
        _HOLIDAYS_CACHE = days or BUILTIN_HOLIDAYS
    return _HOLIDAYS_CACHE
POWERSHELL = "powershell.exe"

# URL 协议：按钮点击 → Windows 启动注册表命令 → action.ps1 写决策文件
PROTOCOLS = {
    "dspg-approve": "approve",
    "dspg-reject": "reject",
}


def time_in_minutes(h, m):
    return h * 60 + m


def is_peak(h, m, d=None):
    """工作日（周一-周五且非法定节假日）高峰时段内 → True。
    周末及法定节假日全天低谷。"""
    if d is None:
        d = datetime.datetime.now(TZ).date()
    if d.weekday() >= 5 or d in load_holidays():
        return False
    t = time_in_minutes(h, m)
    return any(
        time_in_minutes(s[0], s[1]) <= t < time_in_minutes(e[0], e[1])
        for s, e in PEAK_RANGES
    )


def minutes_to_next_switch(h, m, d=None):
    """当前价段还剩多少分钟（距下一次高峰↔空闲切换）。

    高峰中 = 本窗口结束（12:00/18:00）；空闲中 = 下一个高峰起点（当天后续
    窗口，或跳过周末/节假日后的 09:00）。空闲段之后必是高峰，故"下一个
    切换点"就是"下一个高峰开始"。
    """
    if d is None:
        d = datetime.datetime.now(TZ).date()
    holidays = load_holidays()
    t = time_in_minutes(h, m)
    if is_peak(h, m, d):
        for s, e in PEAK_RANGES:  # 高峰中：本窗口结束即切换
            if time_in_minutes(s[0], s[1]) <= t < time_in_minutes(e[0], e[1]):
                return time_in_minutes(e[0], e[1]) - t
    if d.weekday() < 5 and d not in holidays:  # 空闲中：今天可能还有高峰窗口
        for s, _ in PEAK_RANGES:
            sm = time_in_minutes(s[0], s[1])
            if t < sm:
                return sm - t
    nd, days = d + datetime.timedelta(days=1), 1  # 否则找下一个高峰日的 09:00
    while nd.weekday() >= 5 or nd in holidays:
        nd += datetime.timedelta(days=1)
        days += 1
    return days * 24 * 60 - t + 9 * 60


def minutes_to_next_peak(h, m, d=None):
    """距离下一个高峰开始还剩多少分钟（已在高峰内返回 0）。"""
    if is_peak(h, m, d):
        return 0
    return minutes_to_next_switch(h, m, d)


def fmt_duration(minutes):
    """剩余时长：47m / 3h5m / 6d23h"""
    m = max(0, int(minutes))
    if m < 60:
        return f"{m}m"
    if m < 1440:
        return f"{m // 60}h{m % 60}m"
    return f"{m // 1440}d{(m % 1440) // 60}h"


def peak_periods_text():
    return "周一至周五（不含中国法定节假日）09:00-12:00 / 14:00-18:00（北京时间）"


def load_balance():
    """读取余额缓存；失败返回 None"""
    try:
        data = json.loads(BALANCE_CACHE.read_text(encoding="utf-8"))
        return data.get("balance")
    except Exception:
        return None


_PAYLOAD = None


def read_payload():
    """读一次 hook 的 stdin JSON 并缓存（session_id / tool_name 等）。

    ⚠️ stdin 只能读一次：处处 json.load(sys.stdin) 会互相吃掉数据。
    """
    global _PAYLOAD
    if _PAYLOAD is None:
        try:
            _PAYLOAD = json.load(sys.stdin)
        except Exception:
            _PAYLOAD = {}
        if not isinstance(_PAYLOAD, dict):
            _PAYLOAD = {}
    return _PAYLOAD


def read_session_id():
    """从 hook stdin JSON 读 session_id；读不到返回 None"""
    return read_payload().get("session_id")


def check_override(session_id):
    """放行文件有效且未过期 → True。
    单用户本机：不比较 session_id（hook stdin 的 session_id 时有时无，
    比较会导致放行不稳定），放行即本机全会话生效。"""
    try:
        d = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    return time.time() < d.get("expires", 0)


def write_override(session_id, minutes):
    try:
        OVERRIDE_FILE.write_text(
            json.dumps({"session_id": session_id, "expires": time.time() + minutes * 60}),
            encoding="utf-8",
        )
    except Exception:
        pass


# ---------- Toast 通知 + 协议回调 ----------

def ensure_action_ps1():
    """写按钮回调脚本（协议注册指向它）。幂等覆盖写。"""
    ps1 = (
        "# DSPG action handler - called by URL protocol activation\n"
        "param([Parameter(Mandatory=$true)][string]$Action)\n"
        "$result = if ($Action -eq 'approve') { 'yes' } else { 'no' }\n"
        "$decision = @{ result = $result; ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }\n"
        "$decision | ConvertTo-Json | Out-File -FilePath "
        f'\"{DECISION_FILE.as_posix()}\" -Encoding UTF8\n'
    )
    try:
        ACTION_PS1.write_text(ps1, encoding="utf-8-sig")
    except Exception:
        pass


def ensure_protocols():
    """注册 dspg-approve: / dspg-reject: URL 协议（HKCU，无需管理员）。幂等。"""
    try:
        for proto, action in PROTOCOLS.items():
            cmd_line = (
                f'"{POWERSHELL}" -NoProfile -ExecutionPolicy Bypass -File '
                f'"{ACTION_PS1}" -Action {action}'
            )
            subprocess.run(
                ["reg", "add", rf"HKCU\Software\Classes\{proto}", "/ve",
                 "/d", f"URL:{proto} Protocol", "/f"],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["reg", "add", rf"HKCU\Software\Classes\{proto}", "/v", "URL Protocol",
                 "/d", "", "/f"],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["reg", "add", rf"HKCU\Software\Classes\{proto}\shell\open\command",
                 "/ve", "/d", cmd_line, "/f"],
                capture_output=True, timeout=10,
            )
        # Toast 左上角友好名称
        subprocess.run(
            ["reg", "add", rf"HKCU\Software\Classes\AppUserModelId\{AUMID}",
             "/v", "DisplayName", "/d", "DeepSeek 价格守护", "/f"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def wait_decision(timeout=DECISION_TIMEOUT):
    """轮询决策文件，返回 "yes"/"no"/None（超时）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # PS5.1 Out-File UTF8 带 BOM → 用 utf-8-sig 读取
            d = json.loads(DECISION_FILE.read_text(encoding="utf-8-sig"))
            result = d.get("result")
            if result in ("yes", "no"):
                DECISION_FILE.unlink(missing_ok=True)
                return result
        except FileNotFoundError:
            pass
        except Exception:
            pass
        time.sleep(0.5)
    try:
        DECISION_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def ensure_popup_ps1():
    """写 Popup 弹窗脚本（WScript.Shell.Popup 模态对话框，100% 可靠）。幂等。"""
    ps1 = (
        "# DSPG popup - modal dialog via WScript.Shell\n"
        "param([string]$Title, [string]$Message)\n"
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$r = $ws.Popup($Message, {POPUP_WAIT_SEC}, $Title, 36)\n"
        "$result = $null\n"
        "if ($r -eq 6) { $result = 'yes' } elseif ($r -eq 7) { $result = 'no' } else { exit 0 }\n"
        "$decision = @{ result = $result; ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }\n"
        "$decision | ConvertTo-Json | Out-File -FilePath "
        f"'{DECISION_FILE.as_posix()}' -Encoding UTF8\n"
    )
    try:
        POPUP_PS1.write_text(ps1, encoding="utf-8-sig")
    except Exception:
        pass


def show_popup(title, message):
    """同步弹 Popup 等用户点击（脚本内最多等 POPUP_WAIT_SEC 秒），完成后返回。

    超时只留 +3 秒余量：旧版这里是 +10，且之后 wait_decision 还再等 30 秒，
    总计 60+ 秒 > hooks.json 的 45 秒超时 → CC 直接把 hook 杀掉，用户看到的是
    "卡很久然后失败"（2026-10-20 用户报"窗口等待不会超时"）。
    """
    try:
        subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(POPUP_PS1), "-Title", title, "-Message", message],
            timeout=POPUP_WAIT_SEC + 3,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _acquire_popup_mutex(timeout_ms):
    """跨进程互斥：并发的多个工具调用只允许一个真正弹窗，其余排队。"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.CreateMutexW(None, False, MUTEX_NAME)
        if not h:
            return None
        if k.WaitForSingleObject(h, int(timeout_ms)) == 0:   # WAIT_OBJECT_0
            return h
        k.CloseHandle(h)
    except Exception:
        pass
    return None


def _release_popup_mutex(h):
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.ReleaseMutex(h)
        k.CloseHandle(h)
    except Exception:
        pass


def _last_decision():
    """最近 60 秒内是否已经做过决定（含超时=拒绝）→ 返回 "yes"/"no"/None。"""
    try:
        d = json.loads(LAST_DECISION_FILE.read_text(encoding="utf-8"))
        if time.time() - d.get("ts", 0) <= DECISION_REUSE_SEC:
            return d.get("result")
    except Exception:
        pass
    return None


def _remember_decision(ok):
    try:
        LAST_DECISION_FILE.write_text(
            json.dumps({"ts": time.time(), "result": "yes" if ok else "no"}),
            encoding="utf-8")
    except Exception:
        pass


def ask_user(message, simulate=None):
    """弹 Popup 模态对话框并等待决策；返回 True=同意放行。

    simulate: "yes"/"no" 测试用；None=真实弹窗。
    并发安全：用命名互斥体串行化——同时触发的多个工具调用只会弹**一个**窗，
    排队的实例复用刚刚的决定（60 秒内），不会再各弹各的。
    """
    if simulate == "yes":
        return True
    if simulate == "no":
        return False
    mtx = _acquire_popup_mutex((POPUP_WAIT_SEC + 5) * 1000)
    try:
        if check_override(None):      # 排队期间别的实例可能已经拿到放行
            return True
        prev = _last_decision()       # 刚有人点过（或超时）→ 直接复用，不再弹窗
        if prev is not None:
            return prev == "yes"
        ensure_popup_ps1()
        show_popup(POPUP_TITLE, message)
        # 弹窗进程退出前已把决策写盘，这里只要很短的宽限期（旧版又等 30 秒是 bug）
        ok = wait_decision(timeout=3) == "yes"
        _remember_decision(ok)
        return ok
    finally:
        if mtx:
            _release_popup_mutex(mtx)


def block_message(balance_txt, reason="", now_dt=None, switch_dt=None):
    """拦截提示词（注入模型的工具结果）。

    携带当前时间与高峰结束时刻，并给出"高峰结束后 N 分钟"的 cron 表达式，
    模型可直接用 CronCreate(recurring=false) 精确定时到空闲时段继续任务；
    同理也直接告诉它几点解除拦截，避免模型靠猜。
    """
    msg = (
        "\n[deepseek-price-guard] 当前处于 DeepSeek 价格高峰时段（"
        + peak_periods_text()
        + "），价格为高峰价（空闲时段 5 折），当前余额 "
        + balance_txt
        + "。本次工具调用已被阻止。\n"
    )
    cron_hint = ""
    if now_dt is not None and switch_dt is not None:
        # 剩余按 now 抹掉秒后算，与 main 里 peak_left 的分钟口径严格一致
        left_min = (switch_dt - now_dt.replace(second=0, microsecond=0)).total_seconds() / 60
        woke = switch_dt + datetime.timedelta(minutes=WAKE_AFTER_PEAK_MIN)
        wd = "一二三四五六日"[now_dt.weekday()]
        msg += (
            f"时间信息（北京时间）：现在 {now_dt:%Y-%m-%d %H:%M}（周{wd}），"
            f"本段高峰还剩 {fmt_duration(left_min)}，至 {switch_dt:%H:%M} 结束。\n"
        )
        cron_hint = (
            f"，可直接用 cron 表达式 \"{woke.minute} {woke.hour} {woke.day} {woke.month} *\""
            f"（recurring=false → {woke:%H:%M} 触发，即高峰结束后 {WAKE_AFTER_PEAK_MIN} 分钟）"
        )
    if reason:
        msg += reason + "\n"
    msg += (
        "请停止所有工作并结束本轮回复。如任务未完成，用 CronCreate 安排下一次"
        "空闲时段的定时唤醒" + (cron_hint or "（建议下个高峰结束后约 5 分钟）")
        + "，然后停止回复。\n"
        "（如需临时放行：等弹窗出现时点「是」，或启动会话时设 DSPG_BYPASS=1 完全放行）\n"
    )
    return msg


def main():
    ap = argparse.ArgumentParser(description="DeepSeek 峰谷定价 + 余额守护")
    ap.add_argument("--now", help="测试用：伪造当前时间 HH:MM")
    ap.add_argument("--weekday", type=int, choices=range(7), help="测试用：伪造星期 0=周一..6=周日")
    ap.add_argument("--date", help="测试用：伪造日期 YYYY-MM-DD（优先于 --weekday）")
    ap.add_argument("--session", help="测试用：伪造 session_id")
    ap.add_argument("--simulate-popup", choices=["yes", "no"], help="测试用：模拟弹窗结果")
    ap.add_argument("--setup-only", action="store_true", help="只注册协议+回调脚本，不拦截")
    args = ap.parse_args()

    if os.environ.get("DSPG_BYPASS") == "1":
        return 0

    # 定时/唤醒类工具永远放行：被拦的话模型连"预约到空闲时段"都做不到，
    # 而拦截文案恰恰在教它用 CronCreate 排期——不能自相矛盾（2026-10-20 用户报）。
    if str(read_payload().get("tool_name") or "") in SCHEDULING_TOOLS:
        return 0

    if args.setup_only:
        ensure_action_ps1()
        ensure_protocols()
        return 0

    if args.now:
        h, m = (int(x) for x in args.now.split(":"))
    else:
        now = datetime.datetime.now(TZ)
        h, m = now.hour, now.minute
    if args.date:  # 测试用：伪造日期（可测节假日）
        d = datetime.date.fromisoformat(args.date)
    elif args.weekday is not None:  # 兼容旧参数：映射到 2026-03 普通一周
        d = PROBE_MONDAY + datetime.timedelta(days=args.weekday)
    else:
        d = None  # 真实今天
    now_dt = datetime.datetime.now(TZ)
    if args.now or d is not None:  # 测试模式：时刻与真实时钟解耦
        now_dt = datetime.datetime.combine(d or now_dt.date(), datetime.time(h, m), TZ)

    session_id = args.session or read_session_id()
    balance = load_balance()
    balance_txt = f"¥{balance:.2f}" if balance is not None else "N/A"

    # ---- 非高峰：临峰警告 / 低余额警告 ----
    if not is_peak(h, m, d):
        to_peak = minutes_to_next_peak(h, m, d)
        if to_peak <= WARN_BEFORE_MIN:
            sys.stderr.write(
                f"\n[deepseek-price-guard] 距 DeepSeek 价格高峰还有约 {to_peak} 分钟"
                f"（高峰时段 {peak_periods_text()}，价格翻倍），当前余额 {balance_txt}。"
                "请尽快收尾当前任务；未完成的大任务建议暂停，安排到空闲时段继续。\n"
            )
            return 0
        if balance is not None and balance < LOW_BALANCE:
            sys.stderr.write(
                f"\n[deepseek-price-guard] DeepSeek 余额仅 {balance_txt}"
                f"（低于 ¥{LOW_BALANCE:.2f}）。请省着用：\n"
                "- 精简回复输出，避免冗余内容\n"
                "- 合并小工具调用为一次，避免重复读取大文件\n"
                "- 能本地完成的操作不调外部 API\n"
                "- 必要时提醒用户充值\n"
            )
        return 0

    # ---- 高峰：放行检查 → 弹窗决策 ----
    if check_override(session_id):
        return 0  # 已放行，静默

    peak_left_min = minutes_to_next_switch(h, m, d)
    peak_left = fmt_duration(peak_left_min)
    switch_dt = now_dt.replace(second=0, microsecond=0) + datetime.timedelta(minutes=peak_left_min)
    popup_msg = (
        f"DeepSeek 价格高峰时段（{peak_periods_text()}），价格翻倍（空闲 5 折）。\n"
        f"本段高峰还剩 {peak_left}（至 {switch_dt:%H:%M} 结束），结束后自动解除拦截。\n"
        f"当前余额 {balance_txt}。\n\n"
        f"点击「是」临时放行 {OVERRIDE_MIN} 分钟；"
        "点「否」或等待 = 拒绝"
    )
    ok = ask_user(popup_msg, simulate=args.simulate_popup)

    if ok:
        write_override(session_id, OVERRIDE_MIN)
        sys.stderr.write(
            f"\n[deepseek-price-guard] 用户同意临时放行 {OVERRIDE_MIN} 分钟"
            f"（至 {datetime.datetime.now(TZ).strftime('%H:%M')} 后）。"
            "本次工具调用放行。\n"
        )
        return 0
    else:
        sys.stderr.write(block_message(balance_txt, "（用户拒绝了放行或等待超时）", now_dt, switch_dt))
        return 2


if __name__ == "__main__":
    sys.exit(main())
