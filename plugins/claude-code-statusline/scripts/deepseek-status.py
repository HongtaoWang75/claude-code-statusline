#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 状态栏脚本（statusLine）

轮询 DeepSeek 账户余额（https://api.deepseek.com/user/balance），
显示当前价格档位（高峰/5 折）及当前时段剩余时长，15 秒本地缓存避免频繁请求。

API key 来源（按优先级）：环境变量 DEEPSEEK_API_KEY
    → ~/.claude/settings.json 的 env.ANTHROPIC_AUTH_TOKEN（不写入本文件）

用法：
    python deepseek-status.py            # 状态栏用（15s 缓存）
    python deepseek-status.py --refresh  # 强制刷新（跳过缓存）
    python deepseek-status.py --json     # JSON 输出（给 slash command / 模型用）

输出示例：¥88.88 · 梁文谷 剩05:12:33 · 缓存99.8% · 439k toks×12 turns · 178tok/s
"""
import os
import sys
import io
import json
import time
import argparse
import datetime
import pathlib
import subprocess
import urllib.request

# 同目录的 i18n 模块（daemon 通过 importlib 从文件加载本脚本时不会自动加 sys.path）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import i18n  # noqa: E402      多语言文案：跟随系统显示语言，默认英语
import providers  # noqa: E402 主流 provider：识别 / 余额 / 价格表

# Windows 管道重定向时默认 GBK，emoji 会崩 → 两条流都强制 UTF-8
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

CACHE_FILE = pathlib.Path(os.path.expanduser("~/.claude/.balance_cache.json"))
BALANCE_TTL = 30  # 秒：余额缓存多旧才去联网刷新（渲染路径不联网，见 get_balance）
OVERRIDE_FILE = pathlib.Path(os.path.expanduser("~/.claude/.dspg_override.json"))
STDIN_DUMP = pathlib.Path(os.path.expanduser("~/.claude/.dspg_stdin.json"))
PROVIDER_FILE = pathlib.Path(os.path.expanduser("~/.claude/.dspg_provider.json"))
TOKEN_CACHE = pathlib.Path(os.path.expanduser("~/.claude/.dspg_token_stats.json"))
TOKEN_CACHE_VERSION = 3  # 缓存格式版本，改字段就 +1（不匹配会触发全量重扫）
TZ = datetime.timezone(datetime.timedelta(hours=8))
CONFIG_FILE = pathlib.Path(os.path.expanduser("~/.claude/.dspg_config.json"))

_STDIN_CACHE = None


def load_config():
    """读 ~/.claude/.dspg_config.json（每帧读一次，改设置即时生效）。"""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
_NO_DUMP = False  # daemon 模式（render_line）置 True：别每帧重写 stdin 留档

# ANSI 颜色：高峰橙（加粗，256 色 208=#ff8700 纯橙）、低谷绿、放行 <1min 黄色警示
ORANGE = "\033[1;38;5;208m"
GREEN = "\033[32m"
YELLOW = "\033[93m"
RED = "\033[1;31m"
RESET = "\033[0m"

# 警戒渐变三处同源：越接近红线越红（沿色相 黄 60° → 红 0°，会一路经过橙色），
#   余额      ¥1.00 起黄 → ¥0.00 纯红
#   缓存命中  <95% 起黄  → ≤90% 纯红
#   输出速率  <150 起黄  → ≤100 纯红
# 上下文用量是另一条（反向）色阶：0 tok 绿 120° → 1M tok 红 0°。
# 沿色相插值（而非 RGB 直插）才不会掉进灰褐色。渐变要连续就得用 24 位色；
# 终端不支持时退回 256 色立方体最近点（绿→红共 6 级）。
BALANCE_YELLOW_AT = 1.0          # 余额 ≤¥1 起转黄，到 ¥0 纯红
BALANCE_RED_AT = 0.0
CACHE_WARN_PCT = 0.95            # 缓存命中率 <95% 起转黄，到 90% 纯红
CACHE_RED_PCT = 0.90
RATE_WARN_TPS = 150.0            # 输出速率 <150 tok/s 起转黄，到 100 纯红
RATE_RED_TPS = 100.0
CTX_FALLBACK_TOKENS = 1_000_000  # 读不到 CC 配置时的兜底压缩阈值
CTX_ALERT_RATIO = 0.90           # 上下文占满 90% 才闪（颜色本身指示"离自动压缩还有多远"）
_AUTOCOMPACT = None              # autocompact_tokens() 的进程内缓存
BLINK_FRAMES = 6                 # 闪烁帧数：反色/正常交替 6 帧 = 闪 3 次
                                 # （帧长 = 实际刷新间隔：刷新多快就闪多快）
_CUBE6 = (0, 95, 135, 175, 215, 255)  # 256 色立方体单通道的 6 级取值

# 调色板（DSPG_PALETTE=soft 切换）：
#   vivid 纯色刺眼，且纯红 #FF0000 在 #1e1e1e 上对比度只有 4.3:1（低于 WCAG AA 4.5:1），
#        小字号状态栏里发暗、发"糊"；
#   soft  深色终端友好的降饱和版，黄端偏琥珀、红端提亮、绿端偏薄荷，对比度 6~12:1。
PALETTES = {
    "vivid": {"yellow": (255, 255, 0), "red": (255, 0, 0), "green": (0, 255, 0)},
    "soft": {"yellow": (255, 215, 95), "red": (255, 95, 95), "green": (95, 215, 95)},
}
PALETTE = PALETTES.get(os.environ.get("DSPG_PALETTE", "vivid").strip().lower(), PALETTES["vivid"])


def _truecolor_ok():
    """终端是否支持 24 位色。Windows Terminal / VS Code / 现代 conhost 都支持；
    环境变量缺失时不冒险，退回 256 色。

    DSPG_TRUECOLOR=0/1 可强制开关（万一某个终端把 24 位色渲染坏了，设 0 退回
    256 色的 6 级色阶，不用改代码）。
    """
    env = os.environ.get("DSPG_TRUECOLOR", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return True
    ct = os.environ.get("COLORTERM", "").lower()
    return "truecolor" in ct or "24bit" in ct or "truecolor" in os.environ.get("TERM", "").lower()


TRUECOLOR = _truecolor_ok()


def _lerp_rgb(a, b, t):
    """两色线性插值：t=0 取 a、t=1 取 b。"""
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _ansi_rgb(rgb):
    """RGB → ANSI 前景色前缀（24 位色；终端不支持时退到 256 色立方体最近点）。"""
    if TRUECOLOR:
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    q = lambda c: min(range(6), key=lambda i: abs(_CUBE6[i] - c))
    return f"\033[38;5;{16 + 36 * q(rgb[0]) + 6 * q(rgb[1]) + q(rgb[2])}m"


def _hex_rgb(value):
    """'#4D6BFE' → (77, 107, 254)。"""
    s = str(value).lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def themed_text(text, provider, enabled=True):
    """模型名静态显示：默认纯色主品牌色；GRADIENT_PROVIDERS 整段常驻渐变。

    动效（移动高光/多种配色）由本地扩展 theme_demo_local.py 叠加；
    enabled=False（`.dspg_config.json` 的 model_theme=false）→ 原样返回。
    """
    if not enabled or not text:
        return text
    stops = providers.THEMES.get(provider) or []
    if not stops:
        return text
    try:
        rgbs = [_hex_rgb(c) for c in stops]
    except Exception:
        return text
    if provider in getattr(providers, "GRADIENT_PROVIDERS", set()) and len(rgbs) > 1:
        n = len(text)
        out = []
        for i, ch in enumerate(text):
            pos = (i / max(1, n - 1)) * (len(rgbs) - 1)
            idx = min(int(pos), len(rgbs) - 2)
            out.append(_ansi_rgb(_lerp_rgb(rgbs[idx], rgbs[idx + 1], pos - idx)) + ch)
        return "".join(out) + RESET
    return _ansi_rgb(rgbs[0]) + text + RESET


def warn_rgb(value, warn_at, crit_at):
    """「警戒渐变」的 RGB；优于 warn_at（数值更大）或未知时返回 None（不染色）。"""
    if value is None or value > warn_at:
        return None
    t = min(1.0, max(0.0, (warn_at - value) / (warn_at - crit_at)))
    return _lerp_rgb(PALETTE["yellow"], PALETTE["red"], t)


def warn_grad(value, warn_at, crit_at):
    """「警戒渐变」ANSI 前缀（黄端 → 红端）；值优于 warn_at 或未知时返回空串。"""
    rgb = warn_rgb(value, warn_at, crit_at)
    return _ansi_rgb(rgb) if rgb else ""


def balance_color(balance):
    """余额对应的 ANSI 颜色前缀；>¥1 不染色，¥1.00 黄端 → ¥0.00 红端。"""
    return warn_grad(balance, BALANCE_YELLOW_AT, BALANCE_RED_AT)


def autocompact_tokens():
    """自动压缩阈值（token）：决定上下文色阶的「100%」。

    优先级：CC 配置 ~/.claude/settings.json 的 `autoCompactWindow`
    → 环境变量 CLAUDE_CODE_MAX_CONTEXT_TOKENS → 兜底 1M。
    颜色即「离自动压缩还有多远」：到阈值就是红端，占满 90% 开始闪。
    """
    global _AUTOCOMPACT
    if _AUTOCOMPACT is None:
        n = 0
        try:
            cfg = json.loads(pathlib.Path(
                os.path.expanduser("~/.claude/settings.json")).read_text(encoding="utf-8"))
            n = int(cfg.get("autoCompactWindow") or 0)
        except Exception:
            n = 0
        if not n:
            try:
                n = int(os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") or 0)
            except Exception:
                n = 0
        _AUTOCOMPACT = n if n > 0 else CTX_FALLBACK_TOKENS
    return _AUTOCOMPACT


def ctx_rgb(tokens):
    """上下文用量的 RGB：0 tok 绿端 → 自动压缩阈值 红端，中点是黄端。

    分母取 autocompact_tokens()（CC 的 autoCompactWindow），所以颜色直观表示
    「离自动压缩还有多远」。黄落在正中（50%）而不是 1/4 处，比单纯色相直插更
    符合「过半开始警觉」的直觉。
    """
    if tokens is None:
        return None
    t = min(1.0, max(0.0, tokens / autocompact_tokens()))
    if t < 0.5:
        return _lerp_rgb(PALETTE["green"], PALETTE["yellow"], t * 2)
    return _lerp_rgb(PALETTE["yellow"], PALETTE["red"], (t - 0.5) * 2)


def ctx_color(tokens):
    """上下文用量的 ANSI 颜色前缀（恒有颜色，见 ctx_rgb）。"""
    rgb = ctx_rgb(tokens)
    return _ansi_rgb(rgb) if rgb else ""


def _invert_ansi(rgb):
    """反色帧前缀：警告色作**背景** + 白字（闪烁时用）。"""
    if TRUECOLOR:
        return f"\033[38;2;255;255;255m\033[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    q = lambda c: min(range(6), key=lambda i: abs(_CUBE6[i] - c))
    return f"\033[97m\033[48;5;{16 + 36 * q(rgb[0]) + 6 * q(rgb[1]) + q(rgb[2])}m"


def _blink_path(sid):
    """每个会话一个闪烁状态文件。

    ⚠️ 曾经所有会话共用一个文件，结果互相覆盖：会话 B 读到的是"A 还没进入警戒"
    的旧快照，A 写入自己的条目后 B 又拿旧快照整体回写，A 的条目被抹掉 → A 下一帧
    看见"刚进入警戒"→ 又闪 3 次。表现就是**数值明明没变却隔一会儿闪一遍**
    （2026-10-17 用户报）。按会话分文件后不存在这种 lost update。
    """
    safe = "".join(c for c in str(sid) if c.isalnum() or c in "-_")[:24] or "default"
    return pathlib.Path(os.path.expanduser("~/.claude")) / f".dspg_blink_{safe}.json"


def load_blink_state(sid):
    """本会话的闪烁状态：{指标名: {"fp": "warn"/"none", "n": 已渲染帧数}}。"""
    try:
        st = json.loads(_blink_path(sid).read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_blink_state(sid, state):
    """落盘本会话状态；顺手清掉 2 小时没动过的其它会话状态文件。"""
    try:
        _blink_path(sid).write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    try:
        now = time.time()
        me = _blink_path(sid)
        for f in me.parent.glob(".dspg_blink_*.json"):
            if f != me and now - f.stat().st_mtime > 7200:
                f.unlink()
    except Exception:
        pass


def decorate(text, rgb, key, blink, alert=None):
    """给一段文字上警告色；「警戒刚出现」时前 BLINK_FRAMES 帧闪烁。

    alert 默认 = 有颜色即警戒（余额/缓存/速率）；上下文用它单独判定
    （≥自动压缩阈值的 90%）。闪烁节奏：偶数帧（0/2/4…）反色（警告色作底 + 白字）、
    奇数帧正常，帧长 = 实际刷新间隔。
    """
    if alert is None:
        alert = rgb is not None
    rec = blink.get(key) or {"fp": "none", "n": 0}
    invert = False
    if not alert:
        rec["fp"], rec["n"] = "none", 0       # 离开警戒 → 复位，下次再出现重新闪
    else:
        if rec["fp"] != "warn":               # 刚进入警戒 → 从第 0 帧开始闪
            rec["fp"], rec["n"] = "warn", 0
        invert = rec["n"] < BLINK_FRAMES and rec["n"] % 2 == 0
        rec["n"] += 1
    blink[key] = rec
    if rgb is None:
        return text
    return f"{_invert_ansi(rgb) if invert else _ansi_rgb(rgb)}{text}{RESET}"

# 高峰时段（官方：周一至周五 09:00-12:00 / 14:00-18:00 北京时间；与 deepseek-price-guard.py 一致）
PEAK_RANGES = [((9, 0), (12, 0)), ((14, 0), (18, 0))]

# 2026 年法定节假日（国务院办公厅国办发明电〔2025〕7号，2025-11-04 发布）——仅作 fallback。
# 日常读 ~/.claude/.dspg_holidays.json（本插件 scripts/fetch-holidays.py 定期从 timor.tech
# 抓取，见该脚本；计划任务 "DSPG Holiday Fetch"）；缓存缺失/损坏时退回本表。
# 官方规则是「周一至周五（**不含中国法定节假日**）」，此前只判周末 → 国庆（10/1 周四）
# 等假期被误标成高峰。调休上班的周末（1/4、2/14、2/28、5/9、9/20、10/10）按官方措辞
# 「空闲时段包括周末全天」仍算空闲，故不收录。
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

HOLIDAYS_FILE = pathlib.Path(os.path.expanduser("~/.claude/.dspg_holidays.json"))
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


def current_model(payload=None):
    """当前模型名（display_name 优先，退回 id）。"""
    model = (payload if payload is not None else read_stdin_payload()).get("model") or {}
    return str(model.get("display_name") or model.get("id") or "")


def detect_current_provider(payload=None):
    """当前 provider：模型名 → ANTHROPIC_BASE_URL（见 providers.detect_provider）。"""
    return providers.detect_provider(current_model(payload), providers.base_url())


def get_api_key(provider="deepseek"):
    """按 provider 取 key（环境变量 → settings env → 当前 CC token）。"""
    return providers.get_api_key(provider)


def fetch_balance(key, provider="deepseek"):
    """按 provider 查余额；不支持/失败返回 None。"""
    return providers.fetch_balance(provider, key)


_LAST_PROVIDER_WRITE = 0.0
_LAST_PROVIDER_SIG = None


def write_provider_file(provider, model):
    """把当前 provider/model 写给价格守护 hook（hook 载荷里没有模型信息）。

    每帧都写太浪费，签名不变时 30 秒才刷新一次 ts（hook 用 10 分钟新鲜度判断）。
    """
    global _LAST_PROVIDER_WRITE, _LAST_PROVIDER_SIG
    sig = (provider, model, providers.base_url())
    now = time.time()
    if sig == _LAST_PROVIDER_SIG and now - _LAST_PROVIDER_WRITE < 30:
        return
    try:
        PROVIDER_FILE.write_text(json.dumps({
            "provider": provider, "model": model, "base_url": sig[2], "ts": int(now),
        }, ensure_ascii=False), encoding="utf-8")
        _LAST_PROVIDER_SIG, _LAST_PROVIDER_WRITE = sig, now
    except Exception:
        pass


def is_peak(h, m, d=None):
    """高峰判定：工作日（周一-周五且非法定节假日）的 09:00-12:00 / 14:00-18:00。"""
    if d is None:
        d = datetime.datetime.now(TZ).date()
    if d.weekday() >= 5 or d in load_holidays():  # 周末 / 法定节假日全天低谷
        return False
    t = h * 60 + m
    return any(
        s[0] * 60 + s[1] <= t < e[0] * 60 + e[1]
        for s, e in PEAK_RANGES
    )


def minutes_to_next_switch(h, m, d=None):
    """当前价段还剩多少分钟（距下一次高峰↔空闲切换）。

    高峰中 = 本窗口结束（12:00/18:00）；空闲中 = 下一个高峰起点
    （当天后续窗口，或跳过周末/节假日后的 09:00）。空闲段之后必是高峰，
    故"下一个切换点"就是"下一个高峰开始"。
    """
    if d is None:
        d = datetime.datetime.now(TZ).date()
    holidays = load_holidays()
    t = h * 60 + m
    if is_peak(h, m, d):
        for s, e in PEAK_RANGES:  # 高峰中：本窗口结束即切换
            if s[0] * 60 + s[1] <= t < e[0] * 60 + e[1]:
                return e[0] * 60 + e[1] - t
    if d.weekday() < 5 and d not in holidays:  # 空闲中：今天可能还有高峰窗口
        for s, _ in PEAK_RANGES:
            sm = s[0] * 60 + s[1]
            if t < sm:
                return sm - t
    nd, days = d + datetime.timedelta(days=1), 1  # 否则找下一个高峰日的 09:00
    while nd.weekday() >= 5 or nd in holidays:
        nd += datetime.timedelta(days=1)
        days += 1
    return days * 24 * 60 - t + 9 * 60


def fmt_countdown(seconds):
    """峰谷倒计时：天:时:分:秒（满 24h 才出「天」，否则 时:分:秒）。

    与 DSH 余额气泡同款补零：02:14:07 / 1:05:50:50。
    """
    s = max(0, int(seconds))
    dd, r = divmod(s, 86400)
    hh, r = divmod(r, 3600)
    mm, ss = divmod(r, 60)
    return f"{dd}:{hh:02d}:{mm:02d}:{ss:02d}" if dd else f"{hh:02d}:{mm:02d}:{ss:02d}"


def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def fetch_and_store(provider=None):
    """联网取余额并写缓存；成功返回余额，失败返回 None。

    缓存带 provider：换了模型/端点后旧缓存自动作废，不会把别家的余额当成本家的。
    """
    provider = provider or detect_current_provider()
    key = get_api_key(provider)
    if not key:
        return None
    data = fetch_balance(key, provider)
    if not data:
        return None
    try:
        CACHE_FILE.write_text(json.dumps({
            "ts": time.time(), "provider": provider,
            "balance": data.get("balance"), "currency": data.get("currency"),
        }), encoding="utf-8")
    except Exception:
        pass
    return data.get("balance")


def spawn_balance_refresh(cached, now, provider):
    """起一个**分离**的后台进程去联网刷新余额缓存（不等它、20 秒内不重复起）。

    渲染路径不联网的意义：每次渲染耗时都稳定，不会出现"网络卡一下把这一帧拖过
    刷新间隔 → 被 CC 取消 → 顺手 taskkill"的雪崩（2026-10-16 实测踩过）。
    """
    if now - ((cached or {}).get("refreshing") or 0) < 20:
        return
    try:
        CACHE_FILE.write_text(json.dumps({**(cached or {}), "refreshing": now}), encoding="utf-8")
    except Exception:
        pass
    try:
        subprocess.Popen(
            [sys.executable, "-X", "utf8", str(pathlib.Path(__file__).resolve()),
             "--refresh-balance", provider],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 | 0x00000008,  # CREATE_NO_WINDOW | DETACHED_PROCESS
            close_fds=True)
    except Exception:
        pass


def get_balance(force=False, provider=None):
    """读余额：**渲染路径只读本地缓存、绝不联网**；缓存过期就起后台进程去刷。

    force=True（`--refresh`，给 /balance 命令用）时才阻塞联网拿最新值。
    provider 变了（换模型/换端点）→ 旧缓存作废，重新刷。
    """
    provider = provider or detect_current_provider()
    cached = load_cache()
    if cached and cached.get("provider") and cached["provider"] != provider:
        cached = None
    now = time.time()
    age = now - ((cached or {}).get("ts") or 0)
    if force:
        return fetch_and_store(provider) or ((cached or {}).get("balance"))
    if cached is None or age > BALANCE_TTL:
        spawn_balance_refresh(cached, now, provider)
    return (cached or {}).get("balance")


def read_stdin_payload():
    """读取并缓存 statusLine 的 stdin JSON。

    Claude Code 每次刷新调用一次本脚本，stdin 只能读一次，故读后缓存复用。
    内容变化时留档一份到 STDIN_DUMP，便于排查字段可用性（不同 provider/版本
    下 statusLine 输入包含的字段并不一致）。
    """
    global _STDIN_CACHE
    if _STDIN_CACHE is not None:
        return _STDIN_CACHE
    raw = "{}"
    if sys.stdin is not None and not sys.stdin.isatty():
        try:
            raw = sys.stdin.read() or "{}"
        except Exception:
            raw = "{}"
    try:
        _STDIN_CACHE = json.loads(raw)
    except Exception:
        _STDIN_CACHE = {}
    if not _NO_DUMP:  # daemon 模式下每个会话的 payload 都不同，别每帧重写留档
        try:
            if not STDIN_DUMP.exists() or STDIN_DUMP.read_text(encoding="utf-8") != raw:
                STDIN_DUMP.write_text(raw, encoding="utf-8")
        except Exception:
            pass
    return _STDIN_CACHE


def get_model_tier(payload):
    """从 statusLine 的 stdin JSON 读取当前档位，命中高阶档返回警示文本。

    Claude Code 进计划模式（Shift+Tab）会切到 Sonnet/Opus 档，而
    settings.json 里这两档可被映射到高价模型（如 v4-pro，约为 flash 的 4 倍）。
    该切换是静默的，状态栏是唯一能当场看见它的地方。
    """
    model = payload.get("model") or {}
    name = str(model.get("display_name") or model.get("id") or "")
    low = name.lower()
    if "opus" in low or "sonnet" in low:
        return f"{RED}◆{name}{RESET}"
    return ""


def get_cache_hit_ratio(payload):
    """缓存命中率（0~1），无数据返回 None。

    优先用 Claude Code 直接给的 prompt_cache.hit_ratio（v2.1.251+，只含主对话，
    不含子代理）。该字段缺失时按同一公式从 context_window.current_usage 自算：
        cache_read / (input + cache_creation + cache_read)
    实测 DeepSeek 端点下 cache_creation 恒为 0（自动缓存不单独计写入），
    命中部分全部记在 cache_read_input_tokens 里。
    """
    pc = payload.get("prompt_cache") or {}
    ratio = pc.get("hit_ratio")
    if isinstance(ratio, (int, float)):
        return float(ratio)

    cur = (payload.get("context_window") or {}).get("current_usage") or {}
    hit = cur.get("cache_read_input_tokens") or 0
    total = (
        (cur.get("input_tokens") or 0)
        + (cur.get("cache_creation_input_tokens") or 0)
        + hit
    )
    return hit / total if total else None


def _stats_file(path):
    """本 transcript 专属的统计缓存路径。

    **不能所有会话共用一个文件**：用户常同时开多个 Claude Code，各会话的
    statusLine 会互相覆盖缓存里的 path，于是每次刷新都判定成"换了文件"→ 触发
    全量重扫。长会话 transcript 有 7 MB 以上，每 15 秒全量扫一遍纯属浪费。
    """
    return pathlib.Path(os.path.expanduser("~/.claude")) / f".dspg_stats_{path.stem}.json"


def scan_transcript_usage(path):
    """增量扫描 transcript，累计本会话 usage。

    返回 {"out", "in", "cr", "total", "calls"}，分别是输出 tokens、缓存未命中的
    输入、缓存读取、四项总量、API 调用次数。"in"/"cr" 单独留一份是为了算全会话
    的平均缓存命中率——比 CC 的 prompt_cache.hit_ratio 稳定（那个只统计最近一小段，
    会突然跳到 100%）。

    "total" = input + cache_creation + cache_read + output 四项之和，与 Claude Code
    自己 stats-cache.json 的口径一致（cache_read 也计费，只是单价低），所以它就是
    "这个会话一共流过多少 token"。它会远大于 output——每轮对话都要把整个上下文
    重读一遍，命中缓存的那部分记在 cache_read 里。

    transcript 每条 assistant 记录带真实 usage（API 返回原样），是拿会话累计量最
    可靠的来源。但**同一次 API 响应会被拆成多条记录**（每个 content block 一条：
    thinking / text / tool_use），且每条都带完整相同的 usage——逐条累加会把
    output 重复计 3~5 倍（实测 115,703 vs 真实 37,302）。这些记录在文件中严格
    相邻，故只取"段首"（id 与上一条不同的那条），断点只需多存一个 last_id。

    长会话动辄几十 MB，故用 offset 断点续读：只解析上次之后新增的完整行，
    半行留到下次（JSONL 写入中途可能读到残行）。

    context_window.total_output_tokens 不能用来代替：实测它只反映"当前上下文"
    的 output（= current_usage.output_tokens），不是会话累计。

    缓存命中条件里的 `"seg_calls" in state` 不能省：写缓存和读缓存的新旧代码
    交错时（改代码期间 statusLine 照跑），会出现 version 对得上、字段却缺一半的
    缓存。此时若用兜底值顶替 seg_calls，就会把累计轮数当成段内轮数——而增量扫描
    早已越过 compact_boundary，再没机会纠正。缺字段一律全量重扫。
    """
    # seg_calls 必须在场：get_usage_stats 直接取 u["seg_calls"]，少了就 KeyError
    # （transcript 取不到大小/被锁时走这个早退分支，2026-10-17 daemon 日志里抓到）
    empty = {"out": 0, "in": 0, "cr": 0, "total": 0, "calls": 0, "seg_calls": 0}
    try:
        size = path.stat().st_size
    except Exception:
        return empty

    stats_file = _stats_file(path)
    try:
        state = json.loads(stats_file.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    if (state.get("path") == str(path)
            and state.get("version") == TOKEN_CACHE_VERSION
            and "seg_calls" in state
            and 0 < state.get("offset", 0) <= size):
        offset = state["offset"]
        out_t = state.get("out", 0)
        in_t = state.get("in", 0)
        cr_t = state.get("cr", 0)
        tot = state.get("total", 0)
        calls = state.get("calls", 0)
        seg = state["seg_calls"]
        last_id = state.get("last_id")
    else:  # 换文件 / 被截断（/compact、fork）/ 缓存格式升级 → 全量重扫
        offset, out_t, in_t, cr_t, tot, calls, seg, last_id = 0, 0, 0, 0, 0, 0, 0, None

    new_offset = offset
    try:
        with path.open("rb") as f:
            f.seek(offset)
            for raw_line in f:
                if not raw_line.endswith(b"\n"):
                    break  # 半行，下次再读
                new_offset += len(raw_line)
                try:
                    rec = json.loads(raw_line.decode("utf-8"))
                except Exception:
                    continue
                if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
                    # /compact 刚把上下文清空 → 当前段重新计数。
                    # 不重置的话会显示成 "61.6k tok×532turn"：上下文是压缩后的快照，
                    # 轮数却是会话累计，两个维度混在一起，看着自相矛盾。
                    seg = 0
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                rid = msg.get("id") or rec.get("uuid")
                if rid == last_id:
                    continue  # 同一次响应的后续 content block，usage 与段首相同
                last_id = rid
                usage = msg.get("usage") or {}
                o = usage.get("output_tokens") or 0
                i_ = usage.get("input_tokens") or 0
                c_ = usage.get("cache_creation_input_tokens") or 0
                r_ = usage.get("cache_read_input_tokens") or 0
                out_t += o
                in_t += i_ + c_
                cr_t += r_
                tot += i_ + c_ + r_ + o
                calls += 1
                seg += 1
    except Exception:
        return {"out": out_t, "in": in_t, "cr": cr_t, "total": tot,
                "calls": calls, "seg_calls": seg}

    try:
        stats_file.write_text(
            json.dumps({
                "version": TOKEN_CACHE_VERSION,
                "path": str(path),
                "offset": new_offset,
                "out": out_t,
                "in": in_t,
                "cr": cr_t,
                "total": tot,
                "calls": calls,
                "seg_calls": seg,
                "last_id": last_id,
            }),
            encoding="utf-8",
        )
    except Exception:
        pass
    return {"out": out_t, "in": in_t, "cr": cr_t, "total": tot,
            "calls": calls, "seg_calls": seg}


def fmt_tokens(n):
    """大数缩写：999 -> 999，12345 -> 12.3k，439000 -> 439k，162436305 -> 162.44M"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 100_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def get_usage_stats(payload):
    """返回 (平均 tok/s, 本会话总 tokens, API 调用次数, 全会话缓存命中率, 段内轮数, usage 原始字典)。

    tok/s 分子用 output tokens，分母用 Claude Code 累计 API 等待时间
    cost.total_api_duration_ms——用 API 耗时而非墙钟，因为墙钟里混着工具执行和
    等人输入的时间，会把速度摊薄。

    命中率按全会话累计算（cache_read / 全部输入），与"会话总 tokens"同一口径。
    最后一项 usage 原始字典用于按价格表估算花费（in/cr/out 分档计价）。
    """
    tpath = payload.get("transcript_path")
    if not tpath:
        return None, None, None, None, None, None
    api_ms = (payload.get("cost") or {}).get("total_api_duration_ms") or 0
    u = scan_transcript_usage(pathlib.Path(tpath))
    rate = u["out"] / (api_ms / 1000.0) if (api_ms > 0 and u["out"] > 0) else None
    denom = u["in"] + u["cr"]
    hit = u["cr"] / denom if denom > 0 else None
    return rate, (u["total"] or None), (u["calls"] or None), hit, u["seg_calls"], u


def main(argv=None):
    ap = argparse.ArgumentParser(description="DeepSeek 余额状态")
    ap.add_argument("--refresh", action="store_true", help="跳过缓存强制刷新")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--now", help="测试用：伪造当前时间 HH:MM")
    ap.add_argument("--date", help="测试用：伪造日期 YYYY-MM-DD")
    ap.add_argument("--balance", type=float, help="测试用：伪造余额（元），不联网")
    ap.add_argument("--refresh-balance", nargs="?", const="auto",
                    help="内部：联网刷新余额缓存后退出（可指定 provider）")
    args = ap.parse_args(argv)

    if args.refresh_balance:  # 后台刷新进程：只干这一件事就退出
        fetch_and_store(None if args.refresh_balance == "auto" else args.refresh_balance)
        return 0

    payload = read_stdin_payload()
    provider = detect_current_provider(payload)
    model_name = current_model(payload)
    write_provider_file(provider, model_name)

    # --balance 只改显示用的余额，不落缓存、不联网（演示/预览余额配色用）
    balance = (args.balance if args.balance is not None
               else get_balance(force=args.refresh, provider=provider))
    nowdt = datetime.datetime.now(TZ)
    h, m = nowdt.hour, nowdt.minute
    if args.now:
        h, m = (int(x) for x in args.now.split(":"))
    d = datetime.date.fromisoformat(args.date) if args.date else nowdt.date()
    peak = is_peak(h, m, d)
    remain = minutes_to_next_switch(h, m, d)
    # 秒级精度：分钟数 ×60 再扣掉当前秒（--now 伪时间时按整分算）
    remain_sec = remain * 60 - (0 if args.now else nowdt.second)
    tier = get_model_tier(payload)
    hit = get_cache_hit_ratio(payload)
    rate, session_tokens, api_calls, hit_session, seg_calls, usage_stats = get_usage_stats(payload)
    if hit_session is not None:
        hit = hit_session
    # 会话花费：按当前 provider/模型的价格表估算（DeepSeek 自动区分高峰/空闲单价）
    cost = providers.estimate_cost(provider, model_name, usage_stats, peak)  # 全会话平均，胜过 prompt_cache 的"最近一小段"口径

    if args.json:
        print(json.dumps({
            "balance": balance,
            "provider": provider,
            "provider_name": providers.provider_name(provider),
            "model": model_name,
            "session_cost": cost,
            "session_cost_currency": (providers.PROVIDERS.get(provider) or {}).get("currency"),
            "peak": peak,
            "peak_periods": i18n.t("periods"),
            "is_holiday": d in load_holidays(),
            "period_remaining_minutes": remain,
            "period_remaining_seconds": remain_sec,
            "model_tier": tier,
            "cache_hit_ratio": hit,
            "session_total_tokens": session_tokens,
            "session_output_tokens_per_sec": rate,
            "session_api_calls": api_calls,
            "updated": int(time.time()),
        }, ensure_ascii=False))
        return 0

    # 闪烁状态（每会话一个文件）：只有「警戒色刚出现」的头 6 帧会反色（见 decorate）
    sid = str(payload.get("session_id") or "default")
    blink = load_blink_state(sid)

    remain_txt = fmt_countdown(remain_sec)
    # 峰谷倒计时只对「有峰谷定价」的 provider（目前只有 DeepSeek）显示；
    # Kimi / Claude / Gemini / GLM 等一律隐藏（用户 2026-10-22 要求）。
    tag_txt = ""
    if providers.is_peak_pricing(provider):
        tag_txt = (
            f"{ORANGE}{i18n.t('peak.name')} {remain_txt}{RESET}"
            if peak
            else f"{GREEN}{i18n.t('valley.name')} {remain_txt}{RESET}"
        )

    def fmt_mmss(seconds):
        s = max(0, int(seconds))
        return f"{s // 60}:{s % 60:02d}"

    # 放行倒计时（读 guard hook 的放行文件）；<1 分钟黄色警示
    countdown = ""
    try:
        ov = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
        # 放行按会话生效：别的会话的放行不在本会话状态栏显示倒计时
        ov_sid = ov.get("session_id")
        cur_sid = str(payload.get("session_id") or "")
        if cur_sid and ov_sid and cur_sid != ov_sid:
            raise ValueError("override belongs to another session")
        remaining = ov.get("expires", 0) - time.time()
        if remaining > 0:
            countdown = i18n.t("override", time=fmt_mmss(remaining))
            if remaining < 60:
                countdown = YELLOW + countdown + RESET
    except Exception:
        pass

    # 余额：¥1 起转黄、¥0 转红；刚进入警告态时闪 3 次（见 decorate）
    sym = providers.symbol(provider)
    if balance is not None:
        shown = round(balance, 2)   # 按「显示值」判警戒，免得显示没变却在闪
        balance_txt = decorate(f"{sym}{balance:.2f}",
                               warn_rgb(shown, BALANCE_YELLOW_AT, BALANCE_RED_AT),
                               "balance", blink)
    else:
        balance_txt = i18n.t("no_balance")
    # 模型段：当前模型名，按品牌主题色上色（model_theme=false 可关）；
    # 高价档（opus/sonnet）额外加红色 ◆ 警示
    disp_model = model_name if len(model_name) <= 24 else model_name[:23] + "…"
    cfg = load_config()
    use_theme = cfg.get("model_theme", True) is not False
    model_txt = themed_text(disp_model, provider, enabled=use_theme)
    if tier and disp_model:
        model_txt = f"{RED}◆{RESET}" + model_txt
    core = " · ".join(p for p in (model_txt, balance_txt, tag_txt) if p)

    # 用量段：缓存命中率 + 「当前上下文 × 轮数」+ 平均生成速度
    # 用乘积而不是会话累计总量：单看 162M 会和 CC 底部 `/clear to save 439k` 打架，
    # 写成 439k×503 才能看出 162M 是"每轮重传整个上下文"累加出来的。
    usage = []
    if hit is not None:
        shown = round(hit * 100, 1) / 100   # 显示保留 1 位小数，警戒也按显示值判
        hit_txt = decorate(f"{hit * 100:.1f}%",
                           warn_rgb(shown, CACHE_WARN_PCT, CACHE_RED_PCT),  # <95% 黄 → ≤90% 红
                           "cache", blink)
        usage.append(i18n.t("cache", value=hit_txt))
    ctx = (payload.get("context_window") or {}).get("total_input_tokens")
    if ctx and seg_calls:
        # 上下文：色阶分母 = 自动压缩阈值（离压缩多远），≥90% 才闪。
        # 用原始值判定（它只增不减，不会在阈值两侧抖）
        tok_txt = decorate(f"{fmt_tokens(ctx)} toks", ctx_rgb(ctx), "ctx", blink,
                           alert=ctx >= autocompact_tokens() * CTX_ALERT_RATIO)
        usage.append(f"{tok_txt}×{seg_calls} turn{'s' if seg_calls > 1 else ''}")
    if rate is not None:
        shown = float(round(rate))   # 显示为整数，警戒也按显示值判
        rate_txt = decorate(f"{rate:.0f}toks/s",
                            warn_rgb(shown, RATE_WARN_TPS, RATE_RED_TPS),  # <150 黄 → ≤100 红
                            "rate", blink)
        usage.append(rate_txt)
    # 会话花费（只有价格表里有该模型时才显示）
    if cost is not None and cost > 0:
        usage.append(i18n.t("cost", value=f"{sym}{cost:.2f}"))
    if usage:
        core = f"{core} · " + " · ".join(usage)

    save_blink_state(sid, blink)
    print(f"{core} · {countdown}" if countdown else core)


def render_line(payload):
    """给 daemon 用：把 payload 当 statusLine 的 stdin 渲染一行（进程内直接复用 main）。

    不起进程、不读真 stdin、不写 stdin 留档——这就是 daemon 方案省下来的东西：
    bash 启动（103 ms）+ python 启动（74 ms）+ 各自的控制台宿主，全免。
    """
    global _STDIN_CACHE, _NO_DUMP
    _STDIN_CACHE = payload
    _NO_DUMP = True
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        main([])
    finally:
        sys.stdout = old
    return buf.getvalue().strip()


if __name__ == "__main__":
    main()
