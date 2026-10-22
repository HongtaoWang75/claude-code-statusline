---
description: 设置价格守护的弹窗方式（TUI / 独立窗口）与状态栏刷新秒数
---

配置 claude-code-statusline 插件。**先读现状，再用 AskUserQuestion 一次问完，最后写文件。**

> 说明：Claude Code 内置的 `/config` 不允许插件往里加设置项，所以各项开关由这个命令承担。

## 1. 读现状

- `~/.claude/.dspg_config.json` —— 不存在时视为 `{"popup_mode": "window", "refresh_seconds": 1}`
- `~/.claude/settings.json` 的 `statusLine.refreshInterval` —— 不存在时视为未设置
- `~/.claude/.dspg_override.json` —— 存在且 `expires` 大于当前 epoch 秒 = 当前处于「临时放行」

## 2. 询问（AskUserQuestion，四个问题一起问）

**问题 1：弹窗方式**（header：`弹窗方式`）
- `TUI 原生对话框` —— 用 Claude Code 自己的权限询问框（不抢焦点、可键盘操作）。高峰里每次被拦都会问一次，不写 30 分钟放行。
- `独立窗口` —— WScript 模态弹窗。点「是」临时放行 30 分钟，这段时间内高峰不再问；点「否」或 30 秒不点 = 拒绝。

**问题 2：状态栏刷新秒数**（header：`刷新秒数`）
- `1 秒` —— 最灵敏，进程开销最大
- `2 秒`
- `5 秒` —— 省资源，推荐
- `15 秒` —— 最省

说明写进 description：最小 1 秒；写 0.5 不是半秒，而是整个状态栏停止刷新（实测）。改完需新会话或等 CC 重新读取设置才生效。

**问题 3：语言**（header：`语言`）
- `自动（跟随系统）` —— 系统显示语言是中文 → 中文文案，其它 → 英文
- `English` —— 固定英文
- `中文` —— 固定中文

**问题 4：附加操作**（header：`附加操作`）
- `不执行`
- `临时放行 30 分钟` —— 立即解除高峰拦截 30 分钟（写放行文件），适合 TUI 模式下想安静干活
- `取消当前放行` —— 删除放行文件，恢复拦截

**第二轮（单独一次 AskUserQuestion，一个问题）：模型主题色**（header：`主题色`）
- `开启` —— 模型名按各家品牌色上色（DeepSeek / Gemini 是渐变）
- `关闭` —— 纯色显示

## 3. 写文件（用 Python，`encoding="utf-8"`）

```python
import json, pathlib, time

home = pathlib.Path.home()

# 3.1 配置
p = home / ".claude" / ".dspg_config.json"
cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
cfg.update({"popup_mode": MODE, "refresh_seconds": SEC, "language": LANG_VALUE,
            "model_theme": THEME})   # model_theme: true 开 / false 关
# popup_mode: "tui" | "window"；language: "auto"（跟随系统）| "en" | "zh"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

# 3.2 状态栏刷新秒数（保留 settings.json 其他键）
s = home / ".claude" / "settings.json"
data = json.loads(s.read_text(encoding="utf-8"))
data.setdefault("statusLine", {})["refreshInterval"] = SEC
s.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# 3.3 附加操作
ov = home / ".claude" / ".dspg_override.json"
if ACTION == "override":
    ov.write_text(json.dumps({"session_id": "manual", "expires": time.time() + 30 * 60}),
                  encoding="utf-8")
elif ACTION == "no-override":
    ov.unlink(missing_ok=True)
```

注意：
- 若 `settings.json` 解析失败（含注释/尾逗号），不要覆盖它——改用 Read + Edit 只改 `refreshInterval` 那一行。
- `refresh_seconds` 必须是整数且 ≥ 1。
- 只改 `statusLine.refreshInterval`，**不要动** `statusLine.command` 等其他键。

## 4. 汇报

一行说明新配置（弹窗方式 / 刷新秒数 / 附加操作结果），并提醒：设置对之后的高峰拦截立即生效；刷新秒数需新会话或 CC 重新读取设置后生效。
