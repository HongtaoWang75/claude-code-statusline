#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主流模型 provider 支持：识别、余额接口、价格表。

设计原则
- **识别**：先用模型名（最具体），再看 ANTHROPIC_BASE_URL；本地路由器（127.0.0.1
  等）按模型名区分，认不出来返回 "unknown"。
- **余额**：只有提供公开余额接口的 provider 才查（DeepSeek / Moonshot）。
  Anthropic / OpenAI 需要 Admin key（组织级 cost_report），普通 key 拿不到；
  GLM / 小米没有公开余额接口 —— 一律返回 None，状态栏显示 n/a，不影响其它段。
- **价格**：单位是「每 1M tokens」，货币随 provider。内置只写有官方公开价的
  DeepSeek（2026-10 从 api-docs.deepseek.com 实时抓取，空闲价；高峰 = 2 倍）。
  其它模型可在 ~/.claude/.dspg_prices.json 里补，格式见 README。

新增 provider：在 PROVIDERS 加一项；余额解析函数放 _BALANCE_PARSERS。
"""
import json
import os
import pathlib
import urllib.request

HOME = pathlib.Path(os.path.expanduser("~"))
SETTINGS = HOME / ".claude" / "settings.json"
SETTINGS_LOCAL = HOME / ".claude" / "settings.local.json"
PRICES_OVERRIDE = HOME / ".claude" / ".dspg_prices.json"

# key_envs  : 依次在这些环境变量 / settings.json 的 env 里找 key
# url_hints : ANTHROPIC_BASE_URL 里出现即判定为该 provider
# model_hints: 模型 id 里出现即判定（大小写不敏感）
PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "currency": "CNY",
        "symbol": "¥",
        "peak_pricing": True,
        "balance_url": "https://api.deepseek.com/user/balance",
        "key_envs": ("DEEPSEEK_API_KEY",),
        "url_hints": ("api.deepseek.com",),
        "model_hints": ("deepseek",),
    },
    "moonshot": {
        "name": "Moonshot",
        "currency": "CNY",
        "symbol": "¥",
        "peak_pricing": False,
        "balance_url": "https://api.moonshot.cn/v1/users/me/balance",
        "key_envs": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        "url_hints": ("api.moonshot.cn", "platform.kimi.com", "api.kimi.com"),
        "model_hints": ("kimi", "moonshot"),
    },
    "anthropic": {
        "name": "Anthropic",
        "currency": "USD",
        "symbol": "$",
        "peak_pricing": False,
        "balance_url": None,          # 需要 Admin key + /v1/organizations/cost_report
        "key_envs": ("ANTHROPIC_API_KEY",),
        "url_hints": ("api.anthropic.com",),
        "model_hints": ("claude", "opus", "sonnet", "haiku"),
    },
    "openai": {
        "name": "OpenAI",
        "currency": "USD",
        "symbol": "$",
        "peak_pricing": False,
        "balance_url": None,          # 需要 Admin key + /v1/organization/costs
        "key_envs": ("OPENAI_API_KEY",),
        "url_hints": ("api.openai.com",),
        "model_hints": ("gpt-", "gpt5", "o1-", "o3-", "o4-"),
    },
    "gemini": {
        "name": "Google Gemini",
        "currency": "USD",
        "symbol": "$",
        "peak_pricing": False,
        "balance_url": None,          # AI Studio 无余额接口（账单走 Google Cloud Billing）
        "key_envs": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "url_hints": ("generativelanguage.googleapis.com",),
        "model_hints": ("gemini", "gemma"),
    },
    "xiaomi": {
        "name": "Xiaomi MiMo",
        "currency": "CNY",
        "symbol": "¥",
        "peak_pricing": False,
        "balance_url": None,          # 小米 MiMo 无公开余额接口
        "key_envs": ("XIAOMI_API_KEY", "MIMO_API_KEY", "XIAOMI_MIMO_API_KEY"),
        "url_hints": ("xiaomi.com", "mimo.xiaomi", "api.mimo"),
        "model_hints": ("mimo", "xiaomi"),
    },
    "glm": {
        "name": "GLM",
        "currency": "CNY",
        "symbol": "¥",
        "peak_pricing": False,
        "balance_url": None,          # 智谱无公开余额接口
        "key_envs": ("ZHIPU_API_KEY", "ZHIPUAI_API_KEY", "GLM_API_KEY"),
        "url_hints": ("open.bigmodel.cn", "bigmodel.cn"),
        "model_hints": ("glm", "chatglm"),
    },
}

# 品牌主题色（2026-10-22 检索各家品牌资源；多色 = 在模型名上做渐变）：
#   DeepSeek  #4D6BFE → #3982FF → #2498FF（官方字标渐变）
#   Kimi      #1783FF（品牌蓝）
#   Claude    #D97757（Claude 陶土橙）
#   OpenAI    #10A37F（经典绿）
#   Gemini    #4893FC → #969DFF → #BD99FE（经典蓝紫渐变）
#   GLM/智谱  #3859FF
#   Xiaomi    #FF6900（小米橙；MiMo 官方 logo 为多彩渐变）
# 每家：1 个主色 + 多个**同色系**辅助色（逐级提亮，每通道 +20 / +45）。
# 静态用主色；动效高光在辅助色之间轮换——有层次但不换色相，不会变成彩虹字。
THEMES = {
    "deepseek": ["#4D6BFE", "#617FFF", "#7A98FF"],
    # Kimi：白底字（图标里的白 K）+ 蓝色动效（图标蓝点）
    "moonshot": ["#F5F5F5", "#1080F0", "#2494FF"],
    "anthropic": ["#D07050", "#E48464", "#FD9D7D"],  # 取色自 Claude 星芒橙
    "openai": ["#10A37F", "#24B793", "#3DD0AC"],
    # Gemini：图标渐变四色，顺序按红 → 黄 → 绿 → 蓝（整段常驻渐变，见 GRADIENT_PROVIDERS）
    "gemini": ["#C34E5C", "#C4AD22", "#3AAC5A", "#3781E8"],
    "glm": ["#3859FF", "#4C6DFF", "#6586FF"],
    "xiaomi": ["#FF6900", "#FF7D14", "#FF962D"],
    "unknown": ["#9CA3AF", "#B0B7C3", "#C9D0DC"],
}

# 这些 provider 的模型名**常驻整段渐变**（不是纯色 + 动效高光）
GRADIENT_PROVIDERS = {"gemini"}

# 内置价格：每 1M tokens，单位 = provider 货币。只收官方公开、可核实的数字。
# DeepSeek：2026-10-22 从官方定价页抓取（空闲价；官方说明"空闲价 = 高峰价的一半"）。
# 其它 provider 的价目表是动态渲染的，拿不到静态值 → 留空，用户可在
# ~/.claude/.dspg_prices.json 里补（见 README「价格表」）。
PRICES = {
    "deepseek": {
        "deepseek-flash": {
            "input": 1.0, "cache_read": 0.02, "output": 4.0,
            "peak_multiplier": 2.0, "currency": "CNY",
        },
        "deepseek-v4-pro": {
            "input": 4.5, "cache_read": 0.15, "output": 13.5,
            "peak_multiplier": 2.0, "currency": "CNY",
        },
        "default": "deepseek-flash",
    },
}


def _settings_env():
    """合并 settings.json 与 settings.local.json 的 env（后者优先）。"""
    env = {}
    for p in (SETTINGS, SETTINGS_LOCAL):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            env.update(data.get("env") or {})
        except Exception:
            pass
    return env


def base_url():
    return str(_settings_env().get("ANTHROPIC_BASE_URL") or "")


def detect_provider(model="", url=None):
    """识别 provider：**BASE_URL 优先**，它匹配不上时才按模型名。

    为什么 URL 优先：账单跟着端点走。自建网关/中转在跑 `claude-*` 时，
    余额和价格该按端点算，而不是按模型名算成 Anthropic。
    BASE_URL 是本地路由器（127.0.0.1 等）或自定义域名时匹配不上 → 交给模型名。
    """
    m = str(model or "").lower()
    u = str(base_url() if url is None else url).lower()
    if u:
        for key, p in PROVIDERS.items():
            for hint in p["url_hints"]:
                if hint and hint in u:
                    return key
    for key, p in PROVIDERS.items():
        for hint in p["model_hints"]:
            if hint and hint in m:
                return key
    if not u:
        return "anthropic"       # CC 不设 base URL 时走官方 Anthropic 端点
    if any(h in u for h in ("127.0.0.1", "localhost", "0.0.0.0",
                            "192.168.", "10.", "172.16.", "172.17.", "172.18.",
                            "[::1]")):
        return "unknown"         # 本地路由器 + 模型名也没认出来
    return "unknown"


def provider_name(key):
    return (PROVIDERS.get(key) or {}).get("name") or (key or "unknown")


def symbol(key):
    return (PROVIDERS.get(key) or {}).get("symbol") or "¥"


def is_peak_pricing(key):
    return bool((PROVIDERS.get(key) or {}).get("peak_pricing"))


def get_api_key(provider):
    """按 provider 找 key：环境变量 → settings env → 当前 CC token（仅同源时）。"""
    env = _settings_env()
    p = PROVIDERS.get(provider) or {}
    for name in p.get("key_envs", ()):
        v = os.environ.get(name) or env.get(name)
        if v:
            return v
    # CC 正在用的 token：只有 base URL 确实指向该 provider 时才借用
    u = base_url().lower()
    if any(h in u for h in p.get("url_hints", ())):
        v = os.environ.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_AUTH_TOKEN")
        if v:
            return v
    return None


# ---------- 余额 ----------

def _http_json(url, key, timeout=8):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _balance_deepseek(key):
    data = _http_json("https://api.deepseek.com/user/balance", key)
    infos = data.get("balance_infos") or []
    if not infos:
        return None
    total = sum(float(b.get("total_balance", 0)) for b in infos)
    cur = infos[0].get("currency") or "CNY"
    return {"balance": total, "currency": cur}


def _balance_moonshot(key):
    data = _http_json("https://api.moonshot.cn/v1/users/me/balance", key)
    d = data.get("data") or {}
    val = d.get("available_balance")
    if val is None:
        cash = float(d.get("cash_balance") or 0)
        voucher = float(d.get("voucher_balance") or 0)
        val = cash + voucher if (cash or voucher) else None
    if val is None:
        return None
    return {"balance": float(val), "currency": "CNY"}


_BALANCE_PARSERS = {
    "deepseek": _balance_deepseek,
    "moonshot": _balance_moonshot,
}


def fetch_balance(provider, key=None):
    """返回 {"balance": float, "currency": str} 或 None（不支持/失败）。"""
    parser = _BALANCE_PARSERS.get(provider)
    if not parser:
        return None
    key = key or get_api_key(provider)
    if not key:
        return None
    try:
        return parser(key)
    except Exception:
        return None


# ---------- 价格 ----------

def _merged_prices():
    """内置价格 + ~/.claude/.dspg_prices.json（后者按 provider 合并、覆盖同名字段）。"""
    merged = json.loads(json.dumps(PRICES))   # deep copy
    try:
        user = json.loads(PRICES_OVERRIDE.read_text(encoding="utf-8"))
    except Exception:
        return merged
    if not isinstance(user, dict):
        return merged
    for prov, table in user.items():
        if not isinstance(table, dict):
            continue
        cur = merged.setdefault(prov, {})
        for model, price in table.items():
            if isinstance(price, dict):
                cur[model] = {**(cur.get(model) or {}), **price}
            else:
                cur[model] = price
    return merged


def _match_model(table, model):
    """在价格表里找模型：精确 → 子串 → default。"""
    m = str(model or "").lower()
    if m in table:
        return m
    for key in table:
        if key == "default" or not isinstance(table[key], dict):
            continue
        if key.lower() in m:
            return key
    return table.get("default") if table.get("default") in table else None


def price_for(provider, model, peak=False):
    """返回 {"input","cache_read","output","currency"}（每 1M tokens）；无价格返回 None。"""
    table = _merged_prices().get(provider)
    if not isinstance(table, dict):
        return None
    key = _match_model(table, model)
    if not key or not isinstance(table.get(key), dict):
        return None
    price = dict(table[key])
    mult = 1.0
    if peak:
        try:
            mult = float(price.get("peak_multiplier") or 1.0)
        except Exception:
            mult = 1.0
    out = {}
    for k in ("input", "cache_read", "output"):
        if k in price:
            try:
                out[k] = float(price[k]) * mult
            except Exception:
                pass
    if len(out) < 3:
        return None
    out["currency"] = price.get("currency") or (PROVIDERS.get(provider) or {}).get("currency") or "CNY"
    return out


def estimate_cost(provider, model, usage, peak=False):
    """按价格表估算会话花费；返回 (金额, 货币) 或 (None, currency)。

    usage: {"in": 非缓存输入(含 cache_creation), "cr": 缓存读取, "out": 输出}
    """
    if not isinstance(usage, dict):
        return None
    price = price_for(provider, model, peak)
    if not price:
        return None
    try:
        cost = (
            float(usage.get("in") or 0) * price["input"]
            + float(usage.get("cr") or 0) * price["cache_read"]
            + float(usage.get("out") or 0) * price["output"]
        ) / 1_000_000.0
    except Exception:
        return None
    return cost
