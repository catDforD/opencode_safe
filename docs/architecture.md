# Local OpenCode Capture Architecture

当前架构以外置 supervisor 为主链路：

1. `scripts/run_opencode_capture.sh` 调用 `python3 -m orchestrator.opencode_plugin_runner`。
2. supervisor 从 `tasks/issues.json` 读取问题和 `risk_level`。
3. supervisor 在 `artifacts/captures/<timestamp>-<issue-id>/workspace/` 下创建一次性工作副本。
4. supervisor 将项目级插件 `.opencode/plugins/mas_safe_security.ts` 放进工作副本。
5. supervisor 用预构建镜像启动一次性容器，限制 CPU / 内存 / PID / 超时。
6. 运行期间外部轮询采集进程树和网络连接快照，结束后计算工作区前后文件 diff。
7. 若插件写出了 `events/opencode_events.jsonl`，则再解析 `session_id` 并尝试 `opencode export <session_id>`。

这个仓库不再负责：

- 风险评分
- verdict 生成
- 实验矩阵
- baseline / trust_guided 对比
- 多并发 agent 调度

## 目录职责

- `.opencode/plugins/mas_safe_security.ts`：原生事件抓取插件
- `orchestrator/supervisor_capture.py`：supervisor 主实现、摘要和报告生成
- `orchestrator/opencode_plugin_runner.py`：兼容入口
- `orchestrator/capture_queue.py`：固定并发 1 的批量队列执行器
- `scripts/run_opencode_capture.sh`：用户入口脚本
- `scripts/run_all_captures.sh`：批量队列入口
- `tasks/issues.json`：问题目录
- `artifacts/captures/`：每次运行的抓取结果

## 成功标准

一次样本是否有效，不再取决于 `events/opencode_events.jsonl`。`meta/run.json` 会记录：

- `run_status`
- `capture_valid`
- `container_exit_code`
- `timed_out`
- `oom_killed`
- `native_events_status`
- `event_count`
- `session_id`
- `export_saved`
