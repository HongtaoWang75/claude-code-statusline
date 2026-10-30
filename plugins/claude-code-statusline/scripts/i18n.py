#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多语言文案（hook 与状态栏共用）。

语言解析顺序（"跟随 CC" = 跟随 CC 所在系统的显示语言；CC 的 TUI 本身没有
独立语言开关，界面字符串只有英文）：
  1. 环境变量 DSPG_LANG（en / zh，强制覆盖，调试用）
  2. ~/.claude/.dspg_config.json 的 "language"（"auto" = 跟随系统；也可 en / zh）
  3. Windows 用户界面语言（GetUserDefaultUILanguage）：中文 → zh
  4. 默认 "en"

注意：settings.json 的 `language` 是**模型回复语言**，不是界面语言，故不用它。

新增语言：在 STRINGS 里加一本字典即可（缺的键自动回退到英文）。
"""
import json
import os
import pathlib

HOME = pathlib.Path(os.path.expanduser("~"))
CONFIG_FILE = HOME / ".claude" / ".dspg_config.json"

_ZH_HINTS = ("zh", "chinese", "中文", "简体", "繁體", "繁体", "汉语", "漢語", "華語", "华语")
_EN_HINTS = ("en", "english", "英文", "英語", "英语")


def normalize_language(value):
    """把各种写法归一到支持的语言代码；不认识的一律英文（默认）。"""
    s = str(value or "").strip().lower()
    if not s:
        return None
    if any(h in s for h in _ZH_HINTS):
        return "zh"
    if any(h in s for h in _EN_HINTS):
        return "en"
    return "en"


def system_language():
    """系统（Windows 用户界面）语言：中文 → zh，其余 → en。"""
    try:
        import ctypes
        # GetUserDefaultUILanguage: LANGID，低 10 位是主语言 ID；0x04 = LANG_CHINESE
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "zh" if (langid & 0x3FF) == 0x04 else "en"
    except Exception:
        pass
    try:
        import locale
        name = (locale.getlocale()[0] or "").lower()
        return "zh" if name.startswith("zh") else "en"
    except Exception:
        return "en"


def detect_language():
    env = os.environ.get("DSPG_LANG")
    if env:
        return normalize_language(env) or "en"
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        lang = cfg.get("language")
        if lang and str(lang).lower() != "auto":
            return normalize_language(lang) or "en"
    except Exception:
        pass
    return system_language()


LANG = detect_language()

# 星期（block_message 里「周四」用）
WEEKDAYS = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "zh": ["一", "二", "三", "四", "五", "六", "日"],
}

STRINGS = {
    "en": {
        # ---- 通用 ----
        "periods": "Mon-Fri (excluding Chinese public holidays) 09:00-12:00 / 14:00-18:00 (Beijing time)",
        # ---- 弹窗 / TUI 询问 ----
        "popup.title": "DeepSeek peak-price guard",
        "peak.body": "DeepSeek peak pricing is active ({periods}); the price is doubled (idle hours are 50% off).",
        "peak.left": "About {left} left in this peak window (ends at {switch}); the guard lifts automatically after that.",
        "balance.line": "Current balance: {balance}.",
        "window.tail": "Pick how long to allow, then click Yes; No or timeout = deny",
        "popup.cmd": "Command to run",
        "popup.hint": "Enter = allow once  ·  Esc = deny  ·  Up/Down = choose duration",
        "approve.msg": "User allowed temporary access for {minutes} minutes (until {until}). This tool call is allowed.",
        "approve.once": "User allowed this tool call once.",
        "approve.peak": "User allowed until the end of this peak window (until {until}).",
        "deny.reason": "(user denied or the prompt timed out)",
        # ---- 拦截提示词（给模型看） ----
        "block.head": "DeepSeek peak pricing is active ({periods}); the peak price applies (idle hours are 50% off). Current balance {balance}. This tool call has been blocked.",
        "block.time": "Time (Beijing): {now} ({weekday}). About {left} left in this peak window; it ends at {switch}.",
        "block.cron": ", e.g. cron expression \"{expr}\" (recurring=false -> fires at {time}, {after} minutes after the peak ends)",
        "block.footer": "Stop all work and end this reply. If the task is unfinished, use CronCreate to schedule a wake-up in the next idle window{cron}, then stop replying.",
        "block.cron_default": " (about 5 minutes after the next peak ends)",
        "block.hint": "(To allow temporarily: click Yes on the popup, or start the session with DSPG_BYPASS=1 to bypass entirely)",
        # ---- 其他 stderr 提示 ----
        "warn.near": "DeepSeek peak pricing starts in about {minutes} minutes ({periods}; the price doubles). Current balance {balance}. Wrap up the current task; pause large unfinished tasks and resume them in idle hours.",
        "warn.low": "DeepSeek balance is only {balance} (below ¥{threshold}). Please economize:\n- Keep replies concise, avoid redundant content\n- Merge small tool calls; avoid re-reading large files\n- Do local work locally; avoid external API calls\n- Remind the user to top up if needed",
        # ---- 状态栏 ----
        "peak.name": "peak",
        "valley.name": "valley",
        "override": "override {time}",
        "no_balance": "bal N/A",
        "cache": "cache {value}",
        "cost": "cost {value}",
    },
    "zh": {
        "periods": "周一至周五（不含中国法定节假日）09:00-12:00 / 14:00-18:00（北京时间）",
        "popup.title": "DeepSeek 高峰拦截",
        "peak.body": "DeepSeek 价格高峰时段（{periods}），价格翻倍（空闲时段 5 折）。",
        "peak.left": "本段高峰还剩 {left}（至 {switch} 结束），结束后自动解除拦截。",
        "balance.line": "当前余额 {balance}。",
        "window.tail": "选择放行时长后点「是」；点「否」或等待 = 拒绝",
        "popup.cmd": "待执行命令",
        "popup.hint": "Enter = 放行一次  ·  Esc = 拒绝  ·  ↑/↓ = 选择时长",
        "approve.msg": "用户同意临时放行 {minutes} 分钟（至 {until} 后）。本次工具调用放行。",
        "approve.once": "用户放行本次工具调用。",
        "approve.peak": "用户放行至本段高峰结束（{until}）。",
        "deny.reason": "（用户拒绝了放行或等待超时）",
        "block.head": "当前处于 DeepSeek 价格高峰时段（{periods}），价格为高峰价（空闲时段 5 折），当前余额 {balance}。本次工具调用已被阻止。",
        "block.time": "时间信息（北京时间）：现在 {now}（周{weekday}），本段高峰还剩 {left}，至 {switch} 结束。",
        "block.cron": "，可直接用 cron 表达式 \"{expr}\"（recurring=false → {time} 触发，即高峰结束后 {after} 分钟）",
        "block.footer": "请停止所有工作并结束本轮回复。如任务未完成，用 CronCreate 安排下一次空闲时段的定时唤醒{cron}，然后停止回复。",
        "block.cron_default": "（建议下个高峰结束后约 5 分钟）",
        "block.hint": "（如需临时放行：等弹窗出现时点「是」，或启动会话时设 DSPG_BYPASS=1 完全放行）",
        "warn.near": "[deepseek-price-guard] 距 DeepSeek 价格高峰还有约 {minutes} 分钟（高峰时段 {periods}，价格翻倍），当前余额 {balance}。请尽快收尾当前任务；未完成的大任务建议暂停，安排到空闲时段继续。",
        "warn.low": "[deepseek-price-guard] DeepSeek 余额仅 {balance}（低于 ¥{threshold}）。请省着用：\n- 精简回复输出，避免冗余内容\n- 合并小工具调用为一次，避免重复读取大文件\n- 能本地完成的操作不调外部 API\n- 必要时提醒用户充值",
        "peak.name": "梁文峰",
        "valley.name": "梁文谷",
        "override": "放行{time}",
        "no_balance": "余额N/A",
        "cache": "缓存{value}",
        "cost": "花费{value}",
    },
}


def t(key, **fmt):
    """取当前语言的文案；缺失时回退英文，再缺失时返回键名本身。"""
    table = STRINGS.get(LANG) or STRINGS["en"]
    txt = table.get(key) or STRINGS["en"].get(key) or key
    try:
        return txt.format(**fmt)
    except Exception:
        return txt
