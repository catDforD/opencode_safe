# OpenCode Local Capture Tool

这个仓库现在做的是外置 supervisor 驱动的 OpenCode 采集：每次 run 都在独立容器里执行，主采集链路优先记录容器级元数据、进程快照、文件 diff、网络摘要，再把 OpenCode 原生事件作为可选附加数据源。

当前主线不再把 `opencode_events.jsonl` 当成唯一真相源。即使 session 建立失败、插件没加载、或者原生事件为空，只要 supervisor 产物齐全，这次样本仍然算有效。

## 入口

唯一入口脚本：

```bash
./scripts/run_opencode_capture.sh --issue fix_math_utils
```

可选参数：

```bash
./scripts/run_opencode_capture.sh --issue fix_math_utils --keep-workspace
./scripts/run_opencode_capture.sh --issue summarize_release_notes --opencode-bin opencode
./scripts/run_opencode_capture.sh --issue summarize_release_notes --image-ref ghcr.io/anomalyco/opencode:latest
```

按当前队列逻辑串行跑完整个 `tasks/issues.json`：

```bash
./scripts/run_all_captures.sh
make capture-all
```

保留每次运行的工作区副本：

```bash
./scripts/run_all_captures.sh --keep-workspace
make capture-all-keep
```

执行过程中会持续打印总进度、当前 issue、风险级别、单次耗时、累计成功/失败数，以及每次 run 对应的 `capture_dir`。不会并行执行。

会话浏览服务入口：

```bash
./scripts/run_capture_browser.sh
make browse
```

指定监听地址和端口：

```bash
./scripts/run_capture_browser.sh --host 127.0.0.1 --port 8765
make browse BROWSER_HOST=127.0.0.1 BROWSER_PORT=8765
```

## 运行方式

脚本会执行这些步骤：

1. 从 `tasks/issues.json` 读取问题定义和 `risk_level`。
2. 在 `artifacts/captures/<timestamp>-<issue-id>/` 下创建一次运行目录。
3. 创建当前项目的一次性工作副本 `workspace/`。
4. 把 `.opencode/plugins/mas_safe_security.ts` 注入副本。
5. 用预构建镜像在独立容器中执行 `opencode run --print-logs`。
6. supervisor 轮询采集容器退出状态、进程树、网络连接快照，并在结束后计算工作区前后 diff。
7. 如果插件产出了原生事件，再解析 `events/opencode_events.jsonl` 并尝试导出 session。

默认运行结束后会删除 `workspace/`。传 `--keep-workspace` 时才保留。

## 问题目录

问题定义位于 `tasks/issues.json`，每个问题支持这些字段：

- `id`
- `risk_level`：`safe` / `mild` / `severe`
- `title`
- `prompt`
- `files` 可选，表示本次问题关联的文件

示例问题：

- `fix_math_utils`
- `summarize_release_notes`

## 抓取产物

每次运行只生成一个 capture 目录，主要内容如下：

- `meta/run.json`：运行元数据、`run_status`、`capture_valid`、退出码、timeout/OOM、原生事件状态
- `input/issue.json`：本次选中的问题定义
- `logs/container.log`
- `logs/opencode.stdout.log`
- `logs/opencode.stderr.log`
- `logs/opencode_export.stdout.log`
- `logs/opencode_export.stderr.log`
- `observations/process_tree.json`
- `observations/fs_diff.json`
- `observations/network.json`
- `events/opencode_events.jsonl`：可选
- `derived/summary.json`：从原始事件归一化出的单次运行摘要
- `report/index.html`：可直接打开的本地静态报告
- `session/export.json`：仅在拿到 session id 且导出成功时生成
- `workspace/`：仅在 `--keep-workspace` 时保留

样本有效性不再取决于 `events/opencode_events.jsonl`。只要 `meta/run.json`、`observations/process_tree.json`、`observations/fs_diff.json`、`observations/network.json` 落盘成功，这次 run 就会标记 `capture_valid: true`。

生成报告时会优先展示 supervisor 摘要，再附加原生事件时间线。高噪声事件（例如 `message.part.delta`、toast、LSP 诊断等）仍会被折叠。

## 浏览多个会话

浏览服务会固定扫描 `artifacts/captures/`，并提供一个本地单页界面，用下拉框切换 capture 会话：

```bash
./scripts/run_capture_browser.sh --host 127.0.0.1 --port 8765
```

启动后会打印一个本地 URL，例如：

```text
http://127.0.0.1:8765
```

服务会在启动和手动刷新时自动扫描 `captures/`，并尝试为缺少 `derived/summary.json` 的历史会话补齐摘要和 HTML 报告。没有原生事件的 run 也能正常展示。

推荐的完整流程：

```bash
make capture-all
make browse
```

如果你希望在查看时保留每次 run 的临时工作区，改用：

```bash
make capture-all-keep
make browse
```

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

插件会把 `MAS_RUN_ID`、`MAS_CAPTURE_DIR`、`MAS_ISSUE_ID` 注入到 `shell.env`，并将所有原生事件按 JSONL 追加到 `events/opencode_events.jsonl`。这里的 `MAS_CAPTURE_DIR` 在容器内固定指向 `/capture`。

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## 当前文档

- 当前架构说明：`docs/architecture.md`
- 当前运行说明：`docs/operations.md`
- 当前 hook 抓取说明：`docs/opencode_hook_mapping.md`
