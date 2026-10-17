---
description: 查看 DeepSeek 余额和当前价格档位
argument-hint: [--refresh]
---

查询 DeepSeek 账户余额和当前价格档位（高峰/空闲 5 折）。

1. 运行脚本（若用户要求强制刷新，加 `--refresh` 参数跳过缓存）：
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/deepseek-status.py" --json
   ```
2. 向用户报告：当前余额（元）、是否处于价格高峰（周一至周五 09:00–12:00 / 14:00–18:00 为高峰价，法定节假日及周末全天、工作日其余时段为 5 折价；`is_holiday` 表示当天是否法定节假日）、当前时段剩余时长（`period_remaining_minutes`，换算成几小时几分，高峰则提示"约 xx 后恢复 5 折"）、缓存命中率（`cache_hit_ratio`，百分比）、本会话累计 tokens（`session_total_tokens`）与平均生成速度（`session_output_tokens_per_sec`，tok/s）。
   - `session_total_tokens` 是 input + cache_creation + cache_read + output 之和，与 Claude Code 自身统计同口径；它会远大于输出量——每轮对话都要重读整个上下文，命中缓存的部分记在 cache_read 里
   - 速度为 null 表示本会话还没产生输出
3. 若余额低于 ¥5，提醒用户余额不足并建议充值；若临近高峰（15 分钟内），提醒错峰使用。

输出格式示例：余额 ¥0.26，当前空闲时段（5 折价）。
