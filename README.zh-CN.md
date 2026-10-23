[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

# claude-code-statusline · DeepSeek 价格守护与状态栏

给 **Claude Code + DeepSeek** 用户的一组守护工具：峰谷价时段硬拦截、余额/用量状态栏，以及一套让状态栏在 Windows 上**又快又稳**的工程实现。

> 非官方插件，与 DeepSeek 无隶属关系。只调用官方公开接口 `GET https://api.deepseek.com/user/balance` 读余额。

![状态栏预览](plugins/claude-code-statusline/demo-shot.png)

（上图由 `scripts/demo-statusline.py` 用**真实脚本 + 伪造 stdin** 渲染，两种调色板对比）

---

## 安装

```bash
# 在 Claude Code 里
/plugin marketplace add HongtaoWang75/claude-code-statusline
/plugin install claude-code-statusline@claude-code-statusline
```

前置条件：

- Python 3.8+（`python` 在 PATH 里；只有 `python3` 的系统请把 `hooks/hooks.json` 里的 `python` 改成 `python3`）
- 状态栏需要在 `~/.claude/settings.json` 里配置（插件不会自动改你的 settings）：

```json
{
  "statusLine": {
    "type": "command",
    "command": "python \"C:/Users/<你>/.claude/plugins/marketplaces/claude-code-statusline/plugins/claude-code-statusline/scripts/deepseek-status.py\"",
    "refreshInterval": 5
  }
}
```

（`refreshInterval` 最小 1 秒。**填 0.5 不会生效，状态栏会直接停止刷新**。）

- 余额接口需要 API key：环境变量 `DEEPSEEK_API_KEY`，或读 `~/.claude/settings.json` 的 `env.ANTHROPIC_AUTH_TOKEN`（用 DeepSeek 官方端点时通常已经在里面）

---

## 功能

### 1. 峰谷价硬拦截（PreToolUse hook）

DeepSeek 的工作日 **09:00–12:00 / 14:00–18:00**（北京时间，法定节假日全天除外）价格翻倍。高峰时段里，本插件会拦下**所有工具调用**：

- 弹 WPF 对话框（跟随系统深/浅色）：提示文字下方是放行时长下拉菜单（默认 **放行一次**，另有 1 / 3 / 5 / 10 / 20 / 30 / 60 分钟和 **放行本次高峰期**），选好后点「是」；点「否」或 30 秒无响应 = 拒绝。窗口图标用插件内打包的 `assets/claude-light.ico`（黑，浅色模式）/ `claude-dark.ico`（白，深色模式）
- 拒绝时的提示词是**给模型看的**，带完整时间信息与可直接抄用的 cron 表达式，模型会用 `CronCreate` 把任务排到下一个空闲时段，然后干净地收尾
- 定时/唤醒类工具（`CronCreate` / `CronList` / `CronDelete` / `ScheduleWakeup`）**永远放行**，否则模型连「预约到空闲时段」都做不到
- **定时任务触发的整个回合自动放行**：hook 从 transcript 认出该轮是定时任务注入的（`turnOrigin: "scheduled"` / `scheduledTaskId`，且 `promptId` 与 hook 的 `prompt_id` 相同）→ 高峰也不拦。不想这样可在 `.dspg_config.json` 里设 `"exempt_scheduled": false`
- 法定节假日自动从 [timor.tech](https://timor.tech/api/holiday) 抓取（`scripts/fetch-holidays.py`，配一个每周计划任务即可），抓不到就用内置 2026 年表兜底
- 启动会话时设 `DSPG_BYPASS=1` 可完全绕过拦截
- 排查用：每次判定的结果写在 `~/.claude/.dspg_hook.log`（工具名/会话/放行或拒绝/原因），最近一次 hook 原始载荷在 `~/.claude/.dspg_hook_payload.json`（追加历史在 `.dspg_hook_payloads.jsonl`，含 `tool_input`，注意隐私）

### 2. 状态栏

```
deepseek-flash[1m] · ¥22.39 · 梁文谷 2:08:03:09 · 缓存99.6% · 310k toks×8 turns · 451tok/s · 花费¥0.42
```

| 段 | 含义 |
|---|---|
| `deepseek-flash[1m]` | 当前模型名（Opus/Sonnet 等高价位档会变红并加 `◆`） |
| `¥22.39` | 账户余额（**渲染路径永不联网**，见下文） |
| `梁文谷 2:08:03:09` / `梁文峰 01:00:00` | 峰谷倒计时，**天:时:分:秒**（满 24h 才出「天」）；梁文谷=空闲（绿）/ 梁文峰=高峰（橙）。英文界面显示 `valley` / `peak`。**只有有峰谷定价的 provider（目前只有 DeepSeek）才显示** |
| `缓存99.6%` | 全会话缓存命中率 |
| `310k toks×8 turns` | 当前上下文 × 本段轮数（`/compact` 会重置轮数计数） |
| `451tok/s` | 平均输出速度（用 API 耗时算，不含工具执行与等人时间） |
| `花费¥0.42` | 按价格表估算的本次会话花费（价格表里没有当前模型时不显示） |

**配色**：数字只在「进入警戒」时染色，平时保持素色，一眼区分正常/异常。

| 指标 | 不染色 | 黄 | 渐变 | 红 |
|---|---|---|---|---|
| 余额 | > ¥1 | ¥1.00 | ¥1 → ¥0 | ¥0 |
| 缓存命中 | ≥ 95% | < 95% | 95% → 90% | ≤ 90% |
| 输出速率 | ≥ 150 tok/s | < 150 | 150 → 100 | ≤ 100 tok/s |
| 上下文 | — | 50% | 0 绿 → 100% 红 | 到自动压缩阈值 |

- **上下文色阶的分母自动读** Claude Code 配置里的 `autoCompactWindow`（`~/.claude/settings.json`），所以颜色直观表示「离自动压缩还有多远」；备选回退 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`、再退 1M
- **闪烁**：任一指标**刚进入警戒**时，那一段会反色闪 3 次（反色 = 警戒色作底 + 白字），之后安静地保持染色。判定按**显示值**（余额按分、缓存 0.1%、速率取整），所以不会出现「数字没变却在闪」
- **模型主题色**：模型名用该 provider 的主品牌色**纯色**显示。`.dspg_config.json` 里 `"model_theme": false` 可关（settings 命令也会问）
- 24 位真彩；终端不支持时自动退 256 色；`DSPG_TRUECOLOR=0/1` 可强制
- `/balance` 命令：让模型汇报余额/档位/剩余时长/命中率

### 3. 主流模型支持（provider）

状态栏、余额和拦截都会按你当前使用的 provider 自动适配：

| Provider | 识别依据 | 余额 | 峰谷拦截 | 花费估算 |
|---|---|---|---|---|
| **DeepSeek** | `deepseek*` | `/user/balance`（CNY） | ✅ | ✅ 内置价表（区分高峰/空闲） |
| **Moonshot / Kimi** | `kimi*`、`moonshot*` | `/v1/users/me/balance`（CNY） | — | 需在价格表里补 |
| **Anthropic Claude** | `claude*`、`opus/sonnet/haiku` | 需 Admin key → n/a | — | 需在价格表里补 |
| **OpenAI** | `gpt*`、`o1/o3/o4` | 需 Admin key → n/a | — | 需在价格表里补 |
| **Google Gemini** | `gemini*`、`gemma*` | n/a | — | 需在价格表里补 |
| **GLM（智谱）** | `glm*` | n/a | — | 需在价格表里补 |
| **小米 MiMo** | `mimo*`、`xiaomi*` | n/a | — | 需在价格表里补 |

- 识别顺序：模型名 → `ANTHROPIC_BASE_URL` → transcript 里最后一条 assistant 的模型名。可在 `~/.claude/.dspg_config.json` 里用 `"provider": "deepseek"`（或上表任意 key）强制指定，默认 `auto`
- **峰谷拦截只对 DeepSeek 生效**：用 Kimi / Claude / Gemini / GLM 等时不会拦，峰谷倒计时也会隐藏
- 余额按 provider 的货币显示；没有公开余额接口的 provider 显示 `余额N/A`，其余各段照常工作

**价格表**：内置只含 DeepSeek（2026-10-22 从官方定价页实时抓取，CNY / 每 1M tokens；高峰 = 空闲的 2 倍）。其它模型可在 `~/.claude/.dspg_prices.json` 里补：

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

单位是「每 1M tokens」、货币随 provider；可选 `peak_multiplier` 在高峰时段生效。花费按会话累计的 输入 / 缓存读取 / 输出 tokens 乘**当前时段**单价估算。

### 4. 后台刷余额（渲染不联网）

状态栏每秒/每几秒就渲染一帧，如果在渲染路径上发网络请求，网络一抖这一帧就会超时——在 Claude Code 里这会被**取消并 taskkill 清进程树**，多来几次就是一场僵尸进程雪崩（见下文「工程笔记」）。

所以：**渲染只读本地缓存**（`~/.claude/.balance_cache.json`，30 秒有效），过期时 `Popen` 一个**分离**的后台进程去联网更新。渲染耗时因此恒定（实测 180–225 ms → 走 shim 后 131 ms）。

### 5. 可选：常驻 daemon + 原生 shim（Windows 提速 3 倍）

Claude Code 每次刷新状态栏都会起一整条链：`Git Bash（103 ms）→ python（74 ms）→ 你的脚本`，还各带一个控制台宿主 `conhost`。

本插件带一个可选的 **C 语言 shim + Python 常驻 daemon**：

```
CC → bash（CC 强制，省不掉）→ dspg-status.exe（~15 ms）→ 127.0.0.1:18799 上的 daemon（进程内渲染）
```

| | 旧路径（bash+python） | shim + daemon |
|---|---|---|
| 单帧耗时 | 421 ms | **131 ms** |
| 进程数/帧 | bash + python + 2×conhost | 1 个原生 exe |

三层保险：

1. daemon 不在时，shim 打印 daemon 上一帧写的整行（`~/.claude/.dspg_line.txt`）——**不是空白**
2. shim 顺手把 daemon 拉起来（`pythonw`，无窗口），下一帧自动恢复
3. daemon 内置看门狗（每 10 秒）：**挂住的 `taskkill` 超过 60 个就自动回收**（它们独占的 `conhost` 会随之退出）——这是对 Claude Code「取消时 `taskkill /T` 清进程树」副作用的自动抑制；回收后仍连续两次超过 100 个，才把 `refreshInterval` 降回 5 秒兜底（防雪崩自动止血）

启用步骤见 [`scripts/dspg-status.c`](plugins/claude-code-statusline/scripts/dspg-status.c) 头部注释；`dspg.env`（与本 exe 同目录，已被 gitignore）可指定 `PYTHONW=<绝对路径>`。

---

## 配置

Claude Code 内置的 `/config` 不允许插件往里加设置项，所以用命令：

```
/claude-code-statusline:settings
```

它会问你四件事，并写入对应文件：

| 设置 | 存哪里 | 说明 |
|---|---|---|
| 刷新秒数 | `~/.claude/settings.json` → `statusLine.refreshInterval` | 1 / 2 / 5 / 15 秒可选，**最小 1 秒** |
| 语言 | `~/.claude/.dspg_config.json` → `language` | `auto`（跟随系统显示语言）/ `en` / `zh`；界面文案目前支持中英，其它语言回退英文 |
| 模型主题色 | `~/.claude/.dspg_config.json` → `model_theme` | `true`（默认）：品牌色 / `false`：纯色 |
| provider 覆盖 | `~/.claude/.dspg_config.json` → `provider` | `auto`（默认）或任意 provider key（`deepseek`、`moonshot`、`anthropic`、`openai`、`gemini`、`glm`、`xiaomi`） |
| 附加操作 | `~/.claude/.dspg_override.json` | 临时放行 30 分钟 / 取消当前放行 |

## 状态栏行为可调项

| 想改什么 | 改哪里 |
|---|---|
| 各警戒阈值 | `scripts/deepseek-status.py` 顶部 `BALANCE_YELLOW_AT` / `CACHE_WARN_PCT` / `RATE_WARN_TPS` / `CTX_ALERT_RATIO` |
| 调色板 | 顶部 `PALETTES`（`vivid` 纯色 / `soft` 深色终端友好），或环境变量 `DSPG_PALETTE=soft` |
| 闪几次 | `BLINK_FRAMES`（默认 6 帧 = 3 次） |
| 余额缓存多久刷一次 | `BALANCE_TTL`（默认 30 秒） |

---

## 工程笔记：Claude Code 状态栏在 Windows 上的坑

这些都是实测踩出来的，写给后来者：

1. **CC 强制经 Git Bash 起状态栏命令**（现场抓到的进程树：`bash.exe -c "\"python.exe\" -X utf8 \"script.py\""`），`bash -c true` 单跑就要 **103 ms**——这是 MSYS2 的 POSIX 模拟层成本（Linux 上 2 ms）。换 .NET/Go 也省不掉这一层，只有「别每次起进程」能省。
2. **状态栏命令的输出必须等进程退出才显示**，而且**新的刷新触发会取消在跑的那次**。所以：常驻进程直接打印是没用的（内容永远不会显示）；**渲染慢 = 被取消 = 触发 taskkill**。
3. **1 秒刷新的雪崩**：每分钟几十次进程创建，只要机器一卡（编译/杀软扫描/内存紧张），单帧就会超过刷新间隔 → 被取消 → `taskkill` 清进程树 → 挂住的 taskkill 越堆越多（实测 300+ 个、吃掉 7.5 GB）→ 内存告急 → 更卡。**根治办法是把网络请求和一切慢操作挪出渲染路径**，而不是调低刷新率。
4. `refreshInterval` 单位是秒、**最小 1**；写 `0.5` 不会夹紧，而是**整个状态栏停止刷新**（实测 14 秒 0 帧）。
5. **`SO_REUSEADDR` 在 Windows 上允许重复绑定同一端口**（和 Linux 相反）——用它写「端口被占就退出」的单例保护会失效，daemon 越起越多。要用 `SO_EXCLUSIVEADDRUSE`。
6. **`-mwindows`（GUI 子系统）下 CRT 的 stdout 接不上继承来的管道**，`printf` 静默丢输出。要么用 `ReadFile`/`WriteFile` 直连标准句柄，要么就用控制台子系统（shim 会挂到 bash 已有的控制台上，**不会新增 conhost**）。
7. transcript 增量统计里，同一次 API 响应会被拆成多条 assistant 记录（每个 content block 一条），**每条都带完整相同的 usage**，逐条累加会把 output 重复计 3~5 倍——只取「段首」那条。
8. **PreToolUse 的「询问」必须走 `hookSpecificOutput.permissionDecision`**（顶层 `decision`/`reason` 已废弃）：`"ask"` 会弹 CC 自己的权限对话框并在框里显示 `permissionDecisionReason`；要解析这个 JSON，hook 必须 **exit 0**（exit 2 = 直接拦截，JSON 一律无效）。
9. **PreToolUse hook 超时默认不阻断**——如果你想让 hook 当闸门（比如价格拦截），`hooks.json` 里必须写 `"onFailure": "block"`，否则 hook 卡住/超时的时候工具调用会照常放行。
10. **hook 载荷里没有「本回合来源」字段**：要判断一轮是不是定时任务触发的，只能回读 `transcript_path` 指向的 jsonl——定时任务注入的 user 消息带 `turnOrigin: "scheduled"` / `scheduledTaskId` / `promptSource: "system"`，且它的 `promptId` 等于 hook 载荷的 `prompt_id`。两个坑：同一个 `promptId` 还会出现在**每条工具结果**的 user 记录上（没有 `turnOrigin`，倒序扫描会先撞上它们 → 必须跳过带 `toolUseResult` 的记录）；回合变长后触发行会被挤出尾部窗口 → 判定结果按 `prompt_id` 缓存进 `.dspg_turn_cache.json`。只读文件末尾 4 MB，找不到就按普通回合处理（宁可拦，不误放）。

---

## 文件结构

```
.claude-plugin/marketplace.json          # marketplace 清单
plugins/claude-code-statusline/
├── .claude-plugin/plugin.json
├── commands/balance.md                  # /balance 命令
├── commands/settings.md                 # /claude-code-statusline:settings 命令
├── hooks/                               # PreToolUse 峰谷拦截
├── scripts/
│   ├── deepseek-status.py               # 状态栏（配色/闪烁/倒计时/后台刷余额）
│   ├── providers.py                     # provider 识别 / 余额接口 / 价格表
│   ├── i18n.py                          # 界面文案（中/英，跟随系统语言）
│   ├── demo-statusline.py               # 演示脚本（终端里直接跑）
│   ├── statusd.py                       # 可选：常驻 daemon
│   ├── dspg-status.c                    # 可选：原生 shim 源码（build.cmd 编译）
│   ├── fetch-holidays.py                # 法定节假日抓取
│   ├── ds-count.py                      # 本地 tokenizer 计数
│   └── price-watch.py                   # 定价页指纹自检
├── demo-shot.png
├── README.md / README.zh-CN.md / README.ja.md / README.ko.md
```

## License

MIT
