# Local OpenCode Capture Architecture

当前架构只有一条主链路：

1. `scripts/run_opencode_capture.sh` 调用 `python3 -m orchestrator.opencode_plugin_runner`。
2. runner 从 `tasks/issues.json` 选择问题。
3. runner 在 `artifacts/captures/<timestamp>-<issue-id>/workspace/` 下创建一次性工作副本。
4. runner 将项目级插件 `.opencode/plugins/mas_safe_security.ts` 放进工作副本。
5. runner 使用本机 `opencode run --print-logs` 在该副本中执行问题。
6. 插件把原生事件写入 `events/opencode_events.jsonl`。
7. runner 从事件中提取 `session_id`，若成功则执行 `opencode export <session_id>`。

这个仓库不再负责：

- 风险评分
- verdict 生成
- 实验矩阵
- baseline / trust_guided 对比
- 本地沙箱或容器隔离编排

## 目录职责

- `.opencode/plugins/mas_safe_security.ts`：原生事件抓取插件
- `orchestrator/opencode_plugin_runner.py`：最小 runner 和 CLI 入口
- `scripts/run_opencode_capture.sh`：用户入口脚本
- `tasks/issues.json`：问题目录
- `artifacts/captures/`：每次运行的抓取结果

## 成功标准

一次运行是否成功，只取决于 `events/opencode_events.jsonl` 是否存在且非空。`meta/run.json` 会记录：

- `capture_status`
- `exit_code`
- `event_count`
- `session_id`
- `export_saved`
