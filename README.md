# OpenCode Local Capture Tool

这个仓库现在只做一件事：在当前项目里选一个问题，调用你本机的 `opencode` 处理它，并通过项目级插件把 OpenCode 原生过程事件稳定落盘。

当前主线不再包含实验矩阵、风险评分、`summary.json`、`verdict.json`、`record.json`、`observation.json` 这套产物。插件只负责抓取原生事件，不做安全判定或阻断。

## 入口

唯一入口脚本：

```bash
./scripts/run_opencode_capture.sh --issue fix_math_utils
```

可选参数：

```bash
./scripts/run_opencode_capture.sh --issue fix_math_utils --keep-workspace
./scripts/run_opencode_capture.sh --issue summarize_release_notes --opencode-bin /usr/bin/opencode
```

会话浏览服务入口：

```bash
./scripts/run_capture_browser.sh
make browse
```

## 运行方式

脚本会执行这些步骤：

1. 从 `tasks/issues.json` 读取问题定义。
2. 在 `artifacts/captures/<timestamp>-<issue-id>/` 下创建一次运行目录。
3. 创建当前项目的一次性工作副本 `workspace/`。
4. 把 `.opencode/plugins/mas_safe_security.ts` 注入副本。
5. 用本机 `opencode run --print-logs` 执行该问题。
6. 从插件输出中收集原生事件，并尝试导出 session。

默认运行结束后会删除 `workspace/`。传 `--keep-workspace` 时才保留。

## 问题目录

问题定义位于 `tasks/issues.json`，每个问题支持这些字段：

- `id`
- `title`
- `prompt`
- `files` 可选，表示本次问题关联的文件

示例问题：

- `fix_math_utils`
- `summarize_release_notes`

## 抓取产物

每次运行只生成一个 capture 目录，主要内容如下：

- `meta/run.json`：运行元数据、退出码、抓取状态、session id
- `input/issue.json`：本次选中的问题定义
- `logs/opencode.stdout.log`
- `logs/opencode.stderr.log`
- `logs/opencode_export.stdout.log`
- `logs/opencode_export.stderr.log`
- `events/opencode_events.jsonl`
- `derived/summary.json`：从原始事件归一化出的单次运行摘要
- `report/index.html`：可直接打开的本地静态报告
- `session/export.json`：仅在拿到 session id 且导出成功时生成
- `workspace/`：仅在 `--keep-workspace` 时保留

抓取成功只看一件事：`events/opencode_events.jsonl` 是否存在且非空。即使 `opencode` 本身失败，只要事件已经写出，`meta/run.json` 里仍会标记 `capture_status: success`。

生成报告时会默认折叠高噪声事件（例如 `message.part.delta`、toast、LSP 诊断等），主时间线只保留 session、tool、permission、command、file 这类关键动作，便于快速看清一次 run 的推进过程。

## 浏览多个会话

浏览服务会固定扫描 `artifacts/captures/`，并提供一个本地单页界面，用下拉框切换 capture 会话：

```bash
./scripts/run_capture_browser.sh --host 127.0.0.1 --port 8765
```

启动后会打印一个本地 URL，例如：

```text
http://127.0.0.1:8765
```

服务会在启动和手动刷新时自动扫描 `captures/`，并尝试为缺少 `derived/summary.json` 的历史会话补齐摘要和 HTML 报告。

## 插件当前抓取的事件

- `session.*`
- `tool.execute.*`
- `permission.*`
- `file.edited`
- `command.executed`
- `message.updated`
- `message.part.updated`
- `message.removed`
- `message.part.removed`
- `shell.env`

插件会把 `MAS_RUN_ID`、`MAS_CAPTURE_DIR`、`MAS_ISSUE_ID` 注入到 `shell.env`，并将所有原生事件按 JSONL 追加到 `events/opencode_events.jsonl`。

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## 当前文档

- 当前架构说明：`docs/architecture.md`
- 当前运行说明：`docs/operations.md`
- 当前 hook 抓取说明：`docs/opencode_hook_mapping.md`
