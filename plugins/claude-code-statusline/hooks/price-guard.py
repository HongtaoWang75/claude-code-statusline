#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 峰谷定价 + 余额守护 hook（PreToolUse）

功能：
1. 高峰时段（北京时间 周一至周五 09:00-12:00、14:00-18:00，法定节假日全天
   空闲）阻止工具调用。弹 WinForms 对话框：
   - 提示文字下方是放行时长下拉菜单（默认「放行一次」，另有 1/3/5/10/20/30/60
     分钟、放行本次高峰期）
   - 点「是」按所选时长放行；点「否」/ 30 秒无响应 → 继续拦截
   - 窗口图标用插件内打包的 assets/claude.ico
2. 每次工具调用注入余额，低于阈值（¥5）输出省流警告
3. 高峰前 15 分钟提前警告
4. 拦截提示词携带当前时间、本段高峰剩余/结束时刻，并给出可直接使用的
   CronCreate cron 表达式（高峰结束后 5 分钟），便于模型精确定时到空闲时段

文件：
   放行状态  ~/.claude/.dspg_override.json     {session_id, expires}
   决策文件  ~/.claude/.dspg_decision.json     {result: yes/no, minutes, ts}
   弹窗脚本  ~/.claude/.dspg_popup.ps1         （UTF-8 BOM，自动生成）

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

# 上一级 scripts/：多语言文案 + provider 识别
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import i18n  # noqa: E402
import providers  # noqa: E402

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
AUMID = "DeepSeekPriceGuard"  # Toast 左上角显示名（HKCU 注册，无需管理员）

HOME = pathlib.Path(os.path.expanduser("~"))
OVERRIDE_FILE = HOME / ".claude" / ".dspg_override.json"
DECISION_FILE = HOME / ".claude" / ".dspg_decision.json"
LAST_DECISION_FILE = HOME / ".claude" / ".dspg_last_decision.json"
MUTEX_NAME = "Local\\DSPG_Popup_Lock"   # 跨进程弹窗互斥：并发工具调用只弹一个窗
DECISION_REUSE_SEC = 60                 # 刚做过决定（含超时=拒绝）→ 60 秒内复用，不再弹
# 定时/唤醒类工具永远放行（详见 main）：被拦的话模型连"预约到空闲时段"都做不到
SCHEDULING_TOOLS = ("CronCreate", "CronList", "CronDelete", "ScheduleWakeup")
CONFIG_FILE = HOME / ".claude" / ".dspg_config.json"      # 弹窗方式 / 刷新秒数
HOOK_LOG = HOME / ".claude" / ".dspg_hook.log"            # 判定日志（每次调用一行）
PAYLOAD_DUMP = HOME / ".claude" / ".dspg_hook_payload.json"   # 最近一次 hook 原始载荷
PAYLOADS_JSONL = HOME / ".claude" / ".dspg_hook_payloads.jsonl"  # 追加留档（1MB 滚动）
PROVIDER_FILE = HOME / ".claude" / ".dspg_provider.json"   # 状态栏每帧写的当前 provider
TURN_CACHE_FILE = HOME / ".claude" / ".dspg_turn_cache.json"  # prompt_id → 是否定时回合
DEFAULT_CONFIG = {"refresh_seconds": 1, "exempt_scheduled": True, "provider": "auto"}
TRANSCRIPT_TAIL_BYTES = 4 << 20   # 判定「定时回合」只读 transcript 末尾 4MB
ACTION_PS1 = HOME / ".claude" / ".dspg_action.ps1"
POPUP_PS1 = HOME / ".claude" / ".dspg_popup.ps1"
ICON_FILE = pathlib.Path(__file__).resolve().parent.parent / "assets" / "claude.ico"

# 窗口下拉菜单：minutes = 0 放行一次；>0 放行 N 分钟；-1 = 放行到本段高峰结束
ALLOW_CHOICES = [
    (0, "放行一次"),
    (1, "放行 1 分钟"),
    (3, "放行 3 分钟"),
    (5, "放行 5 分钟"),
    (10, "放行 10 分钟"),
    (20, "放行 20 分钟"),
    (30, "放行 30 分钟"),
    (60, "放行 60 分钟"),
    (-1, "放行本次高峰期"),
]
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
    return i18n.t("periods")


def load_balance():
    """读取余额缓存；缓存属于别的 provider 时返回 None（不拿别家余额算 DeepSeek）。"""
    try:
        data = json.loads(BALANCE_CACHE.read_text(encoding="utf-8"))
        prov = data.get("provider")
        if prov and prov != "deepseek":
            return None
        return data.get("balance")
    except Exception:
        return None


_PAYLOAD = None


def hook_log(msg):
    """判定日志：每次工具调用写一行（500KB 滚动清空）。排查「某类工具被拦」靠它。"""
    try:
        if HOOK_LOG.exists() and HOOK_LOG.stat().st_size > 500_000:
            HOOK_LOG.write_text("", encoding="utf-8")
        with HOOK_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def load_config():
    """读 ~/.claude/.dspg_config.json；缺失/损坏时用默认值。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in ("refresh_seconds", "exempt_scheduled", "provider"):
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def dump_payload():
    """把 hook 原始载荷留档（最近一份 + 追加 JSONL），供排查用。

    注意：载荷含 tool_input（可能有文件内容），文件在 ~/.claude 下、不进 git。
    """
    try:
        PAYLOAD_DUMP.write_text(
            json.dumps(_PAYLOAD, ensure_ascii=False, indent=2), encoding="utf-8")
        if PAYLOADS_JSONL.exists() and PAYLOADS_JSONL.stat().st_size > 1_000_000:
            PAYLOADS_JSONL.write_text("", encoding="utf-8")
        with PAYLOADS_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_PAYLOAD, ensure_ascii=False) + "\n")
    except Exception:
        pass


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
        dump_payload()
    return _PAYLOAD


def log_decision(action, reason=""):
    """一行判定日志：动作 工具 会话 原因。"""
    p = read_payload()
    hook_log(
        f"{action:<5} tool={p.get('tool_name') or '?'} "
        f"sid={str(p.get('session_id') or '-')[:8]} {reason}".rstrip()
    )


_TAIL_CACHE = None


def _transcript_tail():
    """读 transcript 末尾 4MB 的行（进程内缓存；找不到返回空列表）。"""
    global _TAIL_CACHE
    if _TAIL_CACHE is not None:
        return _TAIL_CACHE
    tp = read_payload().get("transcript_path")
    if not tp:
        _TAIL_CACHE = []
        return _TAIL_CACHE
    try:
        fpath = pathlib.Path(tp)
        size = fpath.stat().st_size
        with fpath.open("rb") as f:
            if size > TRANSCRIPT_TAIL_BYTES:
                f.seek(size - TRANSCRIPT_TAIL_BYTES)
                f.readline()      # 丢掉可能被截断的半行
            _TAIL_CACHE = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        _TAIL_CACHE = []
    return _TAIL_CACHE


def _load_turn_cache():
    try:
        data = json.loads(TURN_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_turn_cache(cache):
    try:
        if len(cache) > 50:   # 只留最近 50 个 prompt，防无限增长
            items = sorted(cache.items(), key=lambda kv: (kv[1] or {}).get("ts", 0))
            cache = dict(items[-50:])
        TURN_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def turn_is_scheduled():
    """本回合是否由定时任务（CronCreate / ScheduleWakeup）触发。

    2026-10-22 实测：CC 把定时任务注入成一条 user 消息，记录里带
    turnOrigin="scheduled"、scheduledTaskId、promptSource="system"，且
    promptId 与 hook 载荷的 prompt_id 相同；工具结果的 user 记录没有这些字段。
    hook 载荷本身**没有**任何「本回合来源」字段，只能回读 transcript。

    回合变长后那条 user 记录会被挤出尾部窗口，所以**按 prompt_id 缓存判定结果**
    （`.dspg_turn_cache.json`），同一回合后续调用直接读缓存。
    窗口内找不到记录 → 按普通回合处理（宁可拦，不可误放）。
    """
    pid = read_payload().get("prompt_id")
    if not pid:
        return False
    cache = _load_turn_cache()
    hit = cache.get(pid)
    if isinstance(hit, dict) and "scheduled" in hit:
        return bool(hit["scheduled"])
    for line in reversed(_transcript_tail()):
        if pid not in line:   # 快速过滤，绝大多数行走不到 json 解析
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("promptId") != pid:
            continue
        # ⚠️ 同一 promptId 下还有大量「工具结果」user 记录（它们没有 turnOrigin），
        # 倒序扫描会先撞上它们 → 必须跳过，只认本轮真正的用户消息（2026-10-22 实测）。
        if rec.get("toolUseResult") is not None:
            continue
        if rec.get("type") != "user":
            continue
        verdict = bool(rec.get("turnOrigin") == "scheduled" or rec.get("scheduledTaskId"))
        cache[pid] = {"scheduled": verdict, "ts": time.time()}
        _save_turn_cache(cache)
        return verdict
    return False


def last_model_from_transcript():
    """transcript 里最后一条 assistant 响应的 model（大小写原样）。"""
    for line in reversed(_transcript_tail()):
        if '"model"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant":
            continue
        model = (rec.get("message") or {}).get("model")
        if model:
            return str(model)
    return ""


def current_provider():
    """当前 provider —— 决定要不要按 DeepSeek 峰谷价拦截。

    顺序：配置显式指定 → 状态栏每帧写的 .dspg_provider.json（10 分钟内）→
    ANTHROPIC_BASE_URL → transcript 里最后的模型名 → 兜底 DeepSeek。
    """
    cfg = str(load_config().get("provider") or "auto").lower()
    if cfg != "auto" and cfg in providers.PROVIDERS:
        return cfg
    try:
        d = json.loads(PROVIDER_FILE.read_text(encoding="utf-8"))
        if time.time() - (d.get("ts") or 0) <= 600 and d.get("provider"):
            return str(d["provider"])
    except Exception:
        pass
    url = providers.base_url()
    prov = providers.detect_provider("", url)
    if prov != "unknown":
        return prov
    model = last_model_from_transcript()
    if model:
        return providers.detect_provider(model, url)
    return "deepseek"   # 本插件本来就是 DeepSeek 守护，认不出时按 DeepSeek 处理


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


def write_override_until(session_id, until_dt):
    """放行到指定时刻（用于「放行本次高峰期」）。"""
    try:
        OVERRIDE_FILE.write_text(
            json.dumps({"session_id": session_id, "expires": until_dt.timestamp()}),
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
    """轮询决策文件，返回 {"result": "yes"/"no", "minutes": N, "ts": ...} 或 None（超时）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # PS5.1 Out-File UTF8 带 BOM → 用 utf-8-sig 读取
            d = json.loads(DECISION_FILE.read_text(encoding="utf-8-sig"))
            if d.get("result") in ("yes", "no"):
                DECISION_FILE.unlink(missing_ok=True)
                return d
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
    """写弹窗脚本：WinForms 对话框（提示文字 + 放行时长下拉菜单 + 是/否）。幂等。"""
    items = ", ".join("'" + label.replace("'", "''") + "'" for _, label in ALLOW_CHOICES)
    minutes = ", ".join(str(m) for m, _ in ALLOW_CHOICES)
    ps1 = """# DSPG popup - WinForms dialog: message + allow-duration dropdown
param([string]$Title, [string]$Message, [string]$Icon, [int]$WaitSec)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ShowInTaskbar = $true
$form.ClientSize = New-Object System.Drawing.Size(600, 260)
try { $form.Icon = New-Object System.Drawing.Icon($Icon) } catch { }
$label = New-Object System.Windows.Forms.Label
$label.Text = $Message
$label.AutoSize = $false
$label.Location = New-Object System.Drawing.Point(16, 14)
$label.Size = New-Object System.Drawing.Size(568, 156)
$form.Controls.Add($label)
$combo = New-Object System.Windows.Forms.ComboBox
$combo.DropDownStyle = 'DropDownList'
$combo.Location = New-Object System.Drawing.Point(16, 178)
$combo.Size = New-Object System.Drawing.Size(320, 26)
[void]$combo.Items.AddRange(@(%s))
$combo.SelectedIndex = 0
$form.Controls.Add($combo)
$ok = New-Object System.Windows.Forms.Button
$ok.Text = '是'
$ok.DialogResult = 'OK'
$ok.Location = New-Object System.Drawing.Point(396, 214)
$ok.Size = New-Object System.Drawing.Size(90, 28)
$form.Controls.Add($ok)
$no = New-Object System.Windows.Forms.Button
$no.Text = '否'
$no.DialogResult = 'Cancel'
$no.Location = New-Object System.Drawing.Point(494, 214)
$no.Size = New-Object System.Drawing.Size(90, 28)
$form.Controls.Add($no)
$form.AcceptButton = $ok
$form.CancelButton = $no
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [Math]::Max(1, $WaitSec) * 1000
$timer.Add_Tick({ $timer.Stop(); $form.DialogResult = 'Cancel'; $form.Close() })
$timer.Start()
$result = $form.ShowDialog()
$minutes = @(%s)
if ($result -eq 'OK') { $r = 'yes'; $m = $minutes[$combo.SelectedIndex] } else { $r = 'no'; $m = 0 }
$decision = @{ result = $r; minutes = $m; ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
$decision | ConvertTo-Json | Out-File -FilePath '%s' -Encoding UTF8
""" % (items, minutes, DECISION_FILE.as_posix())
    try:
        POPUP_PS1.write_text(ps1, encoding="utf-8-sig")
    except Exception:
        pass


def show_popup(title, message):
    """同步弹窗并等用户选择（脚本内最多等 POPUP_WAIT_SEC 秒），完成后返回。"""
    try:
        subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(POPUP_PS1), "-Title", title, "-Message", message,
             "-Icon", str(ICON_FILE), "-WaitSec", str(POPUP_WAIT_SEC)],
            timeout=POPUP_WAIT_SEC + 5,
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
    """最近一次决定 → (decision_dict, age)；没有则 (None, None)。"""
    try:
        d = json.loads(LAST_DECISION_FILE.read_text(encoding="utf-8"))
        return d, time.time() - d.get("ts", 0)
    except Exception:
        return None, None


def _remember_decision(decision):
    try:
        LAST_DECISION_FILE.write_text(json.dumps({
            "ts": time.time(),
            "result": decision.get("result"),
            "minutes": decision.get("minutes", 0),
        }), encoding="utf-8")
    except Exception:
        pass


def ask_user(message, simulate=None):
    """弹窗并等待决策；返回 {"result": "yes"/"no", "minutes": N}。

    simulate: "yes"/"no" 测试用；None=真实弹窗。
    并发安全：命名互斥体串行化——同时触发的多个工具调用只弹一个窗；
    排队的实例复用刚做过的决定（定长放行 60 秒内复用；“放行一次”只复用到并发爆发窗口 5 秒）。
    """
    if simulate == "yes":
        return {"result": "yes", "minutes": 0}
    if simulate == "no":
        return {"result": "no", "minutes": 0}
    mtx = _acquire_popup_mutex((POPUP_WAIT_SEC + 5) * 1000)
    try:
        if check_override(None):      # 排队期间别的实例可能已经拿到放行
            return {"result": "yes", "minutes": 1}
        prev, age = _last_decision()
        if prev is not None:
            minutes = int(prev.get("minutes") or 0)
            ttl = DECISION_REUSE_SEC if minutes else 5
            if age <= ttl:
                return prev
        ensure_popup_ps1()
        show_popup(i18n.t("popup.title"), message)
        decision = wait_decision(timeout=3)
        if decision is None:
            decision = {"result": "no", "minutes": 0}
        _remember_decision(decision)
        return decision
    finally:
        if mtx:
            _release_popup_mutex(mtx)


def block_message(balance_txt, reason="", now_dt=None, switch_dt=None):
    """拦截提示词（注入模型的工具结果）。

    携带当前时间与高峰结束时刻，并给出"高峰结束后 N 分钟"的 cron 表达式，
    模型可直接用 CronCreate(recurring=false) 精确定时到空闲时段继续任务；
    同理也直接告诉它几点解除拦截，避免模型靠猜。
    """
    msg = "\n[deepseek-price-guard] " + i18n.t(
        "block.head", periods=peak_periods_text(), balance=balance_txt) + "\n"
    cron_hint = ""
    if now_dt is not None and switch_dt is not None:
        # 剩余按 now 抹掉秒后算，与 main 里 peak_left 的分钟口径严格一致
        left_min = (switch_dt - now_dt.replace(second=0, microsecond=0)).total_seconds() / 60
        woke = switch_dt + datetime.timedelta(minutes=WAKE_AFTER_PEAK_MIN)
        wd = i18n.WEEKDAYS.get(i18n.LANG, i18n.WEEKDAYS["en"])[now_dt.weekday()]
        msg += i18n.t(
            "block.time", now=f"{now_dt:%Y-%m-%d %H:%M}", weekday=wd,
            left=fmt_duration(left_min), switch=f"{switch_dt:%H:%M}") + "\n"
        cron_hint = i18n.t(
            "block.cron", expr=f"{woke.minute} {woke.hour} {woke.day} {woke.month} *",
            time=f"{woke:%H:%M}", after=WAKE_AFTER_PEAK_MIN)
    if reason:
        msg += reason + "\n"
    msg += i18n.t("block.footer", cron=cron_hint or i18n.t("block.cron_default")) + "\n"
    msg += i18n.t("block.hint") + "\n"
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
        hook_log("pass  tool=- sid=- DSPG_BYPASS=1")
        return 0

    # 定时/唤醒类工具永远放行：被拦的话模型连"预约到空闲时段"都做不到，
    # 而拦截文案恰恰在教它用 CronCreate 排期——不能自相矛盾（2026-10-20 用户报）。
    if str(read_payload().get("tool_name") or "") in SCHEDULING_TOOLS:
        log_decision("pass", "scheduling-tool")
        return 0

    if args.setup_only:
        ensure_action_ps1()
        ensure_protocols()
        return 0

    # 只有 DeepSeek 有峰谷价：用 Kimi / Claude / Gemini / GLM 等端点时一律不拦
    # （provider 由 状态栏写的 .dspg_provider.json → BASE_URL → transcript 模型名判定）
    provider = current_provider()
    if not providers.is_peak_pricing(provider):
        log_decision("pass", f"provider={provider} (no peak pricing)")
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
                "\n[deepseek-price-guard] " + i18n.t(
                    "warn.near", minutes=to_peak, periods=peak_periods_text(),
                    balance=balance_txt) + "\n"
            )
            log_decision("pass", f"off-peak near-peak({to_peak}m)")
            return 0
        if balance is not None and balance < LOW_BALANCE:
            sys.stderr.write(
                "\n[deepseek-price-guard] " + i18n.t(
                    "warn.low", balance=balance_txt, threshold=f"{LOW_BALANCE:.2f}") + "\n"
            )
        log_decision("pass", "off-peak")
        return 0

    # ---- 高峰：放行检查 → 询问决策 ----
    if check_override(session_id):
        log_decision("pass", "peak override")
        return 0  # 已放行，静默

    # 定时任务触发的回合整轮放行：排期本身就是用户的选择，到点触发时不该再被拦。
    # 依据 transcript 的 turnOrigin="scheduled"（详见 turn_is_scheduled）。
    if load_config().get("exempt_scheduled", True) and turn_is_scheduled():
        log_decision("pass", "peak scheduled-turn")
        return 0

    peak_left_min = minutes_to_next_switch(h, m, d)
    peak_left = fmt_duration(peak_left_min)
    switch_dt = now_dt.replace(second=0, microsecond=0) + datetime.timedelta(minutes=peak_left_min)
    popup_msg = (
        i18n.t("peak.body", periods=peak_periods_text()) + "\n"
        + i18n.t("peak.left", left=peak_left, switch=f"{switch_dt:%H:%M}") + "\n"
        + i18n.t("balance.line", balance=balance_txt) + "\n\n"
        + i18n.t("window.tail")
    )
    decision = ask_user(popup_msg, simulate=args.simulate_popup)
    if decision.get("result") == "yes":
        minutes = int(decision.get("minutes") or 0)
        if minutes > 0:
            write_override(session_id, minutes)
            log_decision("pass", f"peak approved {minutes}min")
            sys.stderr.write(
                "\n[deepseek-price-guard] " + i18n.t(
                    "approve.msg", minutes=minutes,
                    until=datetime.datetime.now(TZ).strftime("%H:%M")) + "\n"
            )
        elif minutes < 0:
            write_override_until(session_id, switch_dt)
            log_decision("pass", "peak approved until-peak-end")
            sys.stderr.write(
                "\n[deepseek-price-guard] " + i18n.t(
                    "approve.peak", until=f"{switch_dt:%H:%M}") + "\n"
            )
        else:
            log_decision("pass", "peak approved once")
            sys.stderr.write("\n[deepseek-price-guard] " + i18n.t("approve.once") + "\n")
        return 0
    else:
        log_decision("block", "peak denied/timeout")
        sys.stderr.write(block_message(balance_txt, i18n.t("deny.reason"), now_dt, switch_dt))
        return 2


if __name__ == "__main__":
    sys.exit(main())
