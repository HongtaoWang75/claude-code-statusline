[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

# claude-code-statusline · DeepSeek price guard & status line

Guard rails for **Claude Code + DeepSeek**: a hard block during DeepSeek's peak-price hours, a balance/usage status line, and a set of engineering notes on making a status line **fast and stable on Windows**.

> Unofficial plugin, not affiliated with DeepSeek. It only calls the public endpoint `GET https://api.deepseek.com/user/balance` to read your balance.

![status line preview](plugins/claude-code-statusline/demo-shot.png)

(Rendered by `scripts/demo-statusline.py` from the **real script with fake stdin** — two palettes compared.)

---

## Features

### 1. Peak-price hard gate (PreToolUse hook)

DeepSeek doubles its price on weekdays **09:00–12:00 / 14:00–18:00** Beijing time (Chinese public holidays excluded). During those windows this plugin blocks **every tool call**:

- A WinForms dialog asks you: pick an allow duration from the dropdown (default **Allow once**, plus 1 / 3 / 5 / 10 / 20 / 30 / 60 minutes and **Allow for this peak window**), then click Yes; No or a 30-second timeout denies. The dialog icon is the bundled `assets/claude.ico`.
- The denial message is **written for the model**: it carries the current time and a ready-to-paste cron expression, so the model schedules the remaining work with `CronCreate` into the next idle window and stops cleanly.
- Scheduling tools (`CronCreate` / `CronList` / `CronDelete` / `ScheduleWakeup`) are **always allowed** — otherwise the model couldn't even schedule a wake-up.
- **Turns fired by a scheduled task are exempt for the whole turn**: the hook recognizes them from the transcript (`turnOrigin: "scheduled"` / `scheduledTaskId`, with a `promptId` equal to the hook payload's `prompt_id`). Disable with `"exempt_scheduled": false`.
- Chinese public holidays are fetched automatically from [timor.tech](https://timor.tech/api/holiday) (`scripts/fetch-holidays.py`, run it from a weekly scheduled task); a built-in 2026 table is the fallback.
- Set `DSPG_BYPASS=1` when starting a session to bypass the guard entirely.
- Diagnostics: every decision is appended to `~/.claude/.dspg_hook.log`; the latest raw hook payload is in `~/.claude/.dspg_hook_payload.json` (history in `.dspg_hook_payloads.jsonl`, which contains `tool_input` — mind the privacy).

### 2. Status line

```
deepseek-flash[1m] · ¥22.39 · valley 2:08:03:09 · cache 99.6% · 310k toks×8 turns · 451tok/s · cost ¥0.42
```

| Segment | Meaning |
|---|---|
| `deepseek-flash[1m]` | The model currently in use (turns red with `◆` for high-cost tiers such as Opus/Sonnet) |
| `¥22.39` | Account balance (**the render path never touches the network** — see below) |
| `valley 2:08:03:09` / `peak 01:00:00` | Peak/off-peak countdown, **d:h:m:s** (the day field appears only at ≥24 h). `valley` = idle (green), `peak` = peak (orange). The Chinese UI says 梁文谷 / 梁文峰. **Only shown for providers with peak pricing (DeepSeek)** |
| `cache 99.6%` | Session-wide cache hit rate |
| `310k toks×8 turns` | Current context × API calls in this segment (`/compact` resets the call count) |
| `451tok/s` | Average output speed (computed from API time, excluding tool execution and waiting for you) |
| `cost ¥0.42` | Session cost estimated from the price table (shown only when a price for the current model is known) |

**Colors**: values are colored only when they enter a warning zone; otherwise they stay plain, so an anomaly is visible at a glance.

| Metric | Plain | Yellow | Gradient | Red |
|---|---|---|---|---|
| Balance | > ¥1 | ¥1.00 | ¥1 → ¥0 | ¥0 |
| Cache hit | ≥ 95% | < 95% | 95% → 90% | ≤ 90% |
| Output rate | ≥ 150 tok/s | < 150 | 150 → 100 | ≤ 100 tok/s |
| Context | — | 50% | 0 green → 100% red | auto-compact threshold |

- The context gradient's denominator is read from Claude Code's `autoCompactWindow` (`~/.claude/settings.json`), so the color shows "how far from auto-compact". Fallbacks: `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, then 1M.
- **Blinking**: when a metric **just** enters a warning zone, that segment blinks 3 times in reverse video (warning color as background, white text), then stays quietly colored. The decision uses the **displayed** value (balance in cents, cache at 0.1%, rate rounded), so a value that doesn't visibly change never re-blinks.
- **Model theme color**: the model segment is tinted with the provider's primary brand color (solid). Turn it off with `"model_theme": false` in `.dspg_config.json` (also asked by the settings command).
- 24-bit truecolor with a 256-color fallback; force it with `DSPG_TRUECOLOR=0/1`.
- `/balance` command: asks the model to report balance, tier, remaining time and hit rate.

### 3. Supported providers

The status line, balance and guard adapt to whichever provider you are using:

| Provider | Recognized by | Balance | Peak-price guard | Cost estimate |
|---|---|---|---|---|
| **DeepSeek** | `deepseek*` | `/user/balance` (CNY) | ✅ | ✅ built-in prices (peak/off-peak) |
| **Moonshot / Kimi** | `kimi*`, `moonshot*` | `/v1/users/me/balance` (CNY) | — | with a price entry |
| **Anthropic Claude** | `claude*`, `opus/sonnet/haiku` | admin key only → n/a | — | with a price entry |
| **OpenAI** | `gpt*`, `o1/o3/o4` | admin key only → n/a | — | with a price entry |
| **Google Gemini** | `gemini*`, `gemma*` | n/a | — | with a price entry |
| **GLM (Zhipu)** | `glm*` | n/a | — | with a price entry |
| **Xiaomi MiMo** | `mimo*`, `xiaomi*` | n/a | — | with a price entry |

- Detection order: model name → `ANTHROPIC_BASE_URL` → the last assistant model in the transcript. You can force it in `~/.claude/.dspg_config.json` with `"provider": "deepseek"` (or any key from the table); `"auto"` is the default.
- **Peak/off-peak blocking only applies to DeepSeek** — with Kimi, Claude, Gemini, GLM, etc. nothing is blocked, and the countdown segment is hidden.
- Balance is shown in the provider's own currency; providers without a public balance API show `bal N/A` and the rest of the line keeps working.

**Price table.** Built-in prices cover DeepSeek only (fetched from the official pricing page on 2026-10-22, CNY per 1M tokens; peak = 2× off-peak). For anything else, create `~/.claude/.dspg_prices.json`:

```json
{
  "moonshot": {
    "kimi-k3": {"input": 4.0, "cache_read": 1.0, "output": 16.0, "currency": "CNY"},
    "default": "kimi-k3"
  },
  "anthropic": {
    "claude-opus-5-5": {"input": 15.0, "cache_read": 1.5, "output": 75.0, "currency": "USD"}
  }
}
```

Units are per 1M tokens in the provider's currency; the optional `peak_multiplier` applies during peak hours. Cost is estimated from the session's cumulative input / cache-read / output tokens at the **current** price tier.

### 4. Background balance refresh (no network on the render path)

The status line renders a frame every second (or every few seconds). A network request on the render path would make a frame miss its deadline whenever the network hiccups — Claude Code then **cancels it and kills the process tree with `taskkill`**. A few of those and you get a zombie-process avalanche (see the engineering notes below).

So: **the render path only reads a local cache** (`~/.claude/.balance_cache.json`, valid for 30 s). When it expires, a **detached** background process is spawned to refresh it. Rendering cost is therefore constant (measured 180–225 ms → 131 ms through the shim).

### 5. Optional: resident daemon + native shim (3× faster on Windows)

Every refresh starts a whole chain: `Git Bash (103 ms) → python (74 ms) → your script`, each with a console host (`conhost`).

This plugin ships an optional **C shim + Python resident daemon**:

```
CC → bash (forced by CC, unavoidable) → dspg-status.exe (~15 ms) → daemon on 127.0.0.1:18799 (renders in-process)
```

| | Old path (bash+python) | shim + daemon |
|---|---|---|
| Per frame | 421 ms | **131 ms** |
| Processes/frame | bash + python + 2×conhost | one native exe |

Three layers of fallback:

1. If the daemon is down, the shim prints the last line the daemon wrote (`~/.claude/.dspg_line.txt`) — **never blank**.
2. The shim also starts the daemon (`pythonw`, no window); the next frame recovers.
3. A watchdog inside the daemon (every 10 s) **reaps hung `taskkill` processes when more than 60 accumulate** (their `conhost` children exit with them) — this suppresses the side effects of Claude Code's "`taskkill /T` the process tree on cancel". Only if the count stays above 100 for two consecutive checks does it downgrade `refreshInterval` back to 5 s as an emergency brake.

Setup steps are in the header comment of [`scripts/dspg-status.c`](plugins/claude-code-statusline/scripts/dspg-status.c); `dspg.env` (next to the exe, gitignored) can pin `PYTHONW=<absolute path>`.

---

## Install

```bash
# inside Claude Code
/plugin marketplace add <your-github-account>/claude-code-statusline
/plugin install claude-code-statusline@claude-code-statusline
```

Requirements:

- Python 3.8+ (`python` on PATH; on systems with only `python3`, change `python` to `python3` in `hooks/hooks.json`)
- The status line must be configured in `~/.claude/settings.json` (the plugin won't edit your settings):

```json
{
  "statusLine": {
    "type": "command",
    "command": "python \"C:/Users/<you>/.claude/plugins/marketplaces/claude-code-statusline/plugins/claude-code-statusline/scripts/deepseek-status.py\"",
    "refreshInterval": 5
  }
}
```

(`refreshInterval` is in seconds and the minimum is 1. **`0.5` does not clamp — it stops the status line from refreshing at all**.)

- The balance endpoint needs an API key: the `DEEPSEEK_API_KEY` environment variable, or `env.ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json` (usually already there when you use the official DeepSeek endpoint).

---

## Configuration

Claude Code's built-in `/config` does not let plugins add entries, so use the command:

```
/claude-code-statusline:settings
```

It asks you four things and writes the files below:

| Setting | Stored in | Notes |
|---|---|---|
| Refresh seconds | `~/.claude/settings.json` → `statusLine.refreshInterval` | 1 / 2 / 5 / 15 s, **minimum 1 s** |
| Language | `~/.claude/.dspg_config.json` → `language` | `auto` (follow the system display language) / `en` / `zh`. The UI currently ships English and Chinese; other languages fall back to English |
| Model theme | `~/.claude/.dspg_config.json` → `model_theme` | `true` (default): brand colors / `false`: plain |
| Provider override | `~/.claude/.dspg_config.json` → `provider` | `auto` (default) or a provider key (`deepseek`, `moonshot`, `anthropic`, `openai`, `gemini`, `glm`, `xiaomi`) |
| Extra action | `~/.claude/.dspg_override.json` | Grant a 30-minute override / cancel the current override |

## Status line tunables

| What | Where |
|---|---|
| Warning thresholds | Top of `scripts/deepseek-status.py`: `BALANCE_YELLOW_AT` / `CACHE_WARN_PCT` / `RATE_WARN_TPS` / `CTX_ALERT_RATIO` |
| Palette | `PALETTES` at the top (`vivid` saturated / `soft` dark-terminal friendly), or `DSPG_PALETTE=soft` |
| Blink count | `BLINK_FRAMES` (default 6 frames = 3 blinks) |
| Balance cache TTL | `BALANCE_TTL` (default 30 s) |

---

## Engineering notes: Claude Code status lines on Windows

All of these were measured the hard way — for whoever comes next:

1. **CC always starts the status line command through Git Bash** (captured process tree: `bash.exe -c "\"python.exe\" -X utf8 \"script.py\""`). `bash -c true` alone costs **103 ms** — that's the MSYS2 POSIX emulation layer (2 ms on Linux). Switching to .NET/Go doesn't remove it; only "don't start a process every frame" does.
2. **The status line output is shown only after the process exits**, and **a new refresh cancels the in-flight one**. So a resident process printing directly is useless (the content never displays); **slow rendering = cancelled = `taskkill`**.
3. **The 1-second avalanche**: dozens of process creations per minute; as soon as the machine hiccups (compile, AV scan, memory pressure) a frame misses its deadline → cancelled → `taskkill` the tree → hung `taskkill`s pile up (measured 300+, 7.5 GB) → memory pressure → slower still. **The cure is moving network calls and all slow work off the render path**, not lowering the refresh rate.
4. `refreshInterval` is in seconds, **minimum 1**. Writing `0.5` does not clamp — it **stops the status line entirely** (measured: 0 frames in 14 s).
5. **On Windows, `SO_REUSEADDR` allows binding the same port twice** (the opposite of Linux) — using it for a "exit if the port is taken" singleton guard fails and daemons multiply. Use `SO_EXCLUSIVEADDRUSE`.
6. **Under `-mwindows` (GUI subsystem) the CRT's stdout cannot attach to an inherited pipe** — `printf` silently drops output. Either use `ReadFile`/`WriteFile` on the standard handles, or stay a console subsystem app (the shim attaches to bash's existing console, **no extra conhost**).
7. In incremental transcript accounting, one API response is split into several assistant records (one per content block), **each carrying the full identical usage**; summing them multiplies output by 3–5× (measured 115,703 vs. the real 37,302). The records are strictly adjacent — take only the segment head.
8. **PreToolUse "ask" must use `hookSpecificOutput.permissionDecision`** (top-level `decision`/`reason` are deprecated): `"ask"` shows Claude Code's own permission dialog with `permissionDecisionReason`; for the JSON to be parsed the hook must **exit 0** (exit 2 blocks outright, and the JSON is ignored).
9. **A timed-out PreToolUse hook does NOT block by default** — if you want the hook to act as a gate (like this price guard), you must set `"onFailure": "block"` in `hooks.json`; otherwise a hung hook lets the tool call through.
10. **The hook payload has no "turn origin" field**: to tell whether a turn was fired by a scheduled task you must read back the jsonl at `transcript_path` — the injected user message carries `turnOrigin: "scheduled"` / `scheduledTaskId` / `promptSource: "system"`, and its `promptId` equals the hook payload's `prompt_id`. Two traps: the same `promptId` also appears on **every tool-result user record** (which has no `turnOrigin`; scanning backwards hits those first — skip records with `toolUseResult`), and long turns push the trigger record out of the tail window — so the verdict is cached per `prompt_id` in `.dspg_turn_cache.json`. Only the last 4 MB is read; if the record isn't found, treat it as a normal turn (fail closed).

---

## Layout

```
.claude-plugin/marketplace.json          # marketplace manifest
plugins/claude-code-statusline/
├── .claude-plugin/plugin.json
├── commands/balance.md                  # /balance command
├── commands/settings.md                 # /claude-code-statusline:settings command
├── hooks/                               # PreToolUse peak gate
├── scripts/
│   ├── deepseek-status.py               # the status line (colors/blink/countdown/async balance)
│   ├── providers.py                     # provider detection, balance APIs, price table
│   ├── i18n.py                          # UI strings (English/Chinese, follows the system language)
│   ├── demo-statusline.py               # demo renderer (run it in a terminal)
│   ├── statusd.py                       # optional resident daemon
│   ├── dspg-status.c                    # optional native shim source (build.cmd)
│   ├── fetch-holidays.py                # Chinese public holiday fetcher
│   ├── ds-count.py                      # local tokenizer counting
│   └── price-watch.py                   # pricing-page fingerprint self-check
├── demo-shot.png
├── README.md / README.zh-CN.md / README.ja.md / README.ko.md
```

## License

MIT
