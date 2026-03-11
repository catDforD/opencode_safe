# OpenCode Hook Capture Contract

项目级插件 `.opencode/plugins/mas_safe_security.ts` 当前按原生事件名直接落盘，不再映射到自定义 verdict 或实验 schema。

## 当前采集事件

- `session.created`
- `session.updated`
- `session.idle`
- `session.error`
- `session.status`
- `session.diff`
- `tool.execute.before`
- `tool.execute.after`
- `permission.asked`
- `permission.replied`
- `file.edited`
- `command.executed`
- `message.updated`
- `message.part.updated`
- `message.removed`
- `message.part.removed`
- `shell.env`

## 每条记录结构

每条 JSONL 记录包含：

- `timestamp`
- `native_event_type`
- `run_id`
- `issue_id`
- `context`
- `correlation`
- `raw_input`
- `raw_output`

## shell.env 注入

插件只注入三个追踪变量：

- `MAS_RUN_ID`
- `MAS_CAPTURE_DIR`
- `MAS_ISSUE_ID`

除此之外，插件不做阻断、改写命令、评分或策略判定。
