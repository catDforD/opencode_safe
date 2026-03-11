# Local OpenCode Capture Operations

## 基本运行

```bash
./scripts/run_opencode_capture.sh --issue fix_math_utils
```

## 保留工作副本

```bash
./scripts/run_opencode_capture.sh --issue fix_math_utils --keep-workspace
```

## 指定本机 opencode 路径

```bash
./scripts/run_opencode_capture.sh --issue summarize_release_notes --opencode-bin /usr/bin/opencode
```

## 查看结果

脚本结束时会打印本次 capture 目录路径。进入该目录后，优先看这几个文件：

- `meta/run.json`
- `events/opencode_events.jsonl`
- `logs/opencode.stderr.log`
- `session/export.json`

## 常见判断

- `capture_status: success`
  说明插件已经抓到事件，不代表模型一定完成了问题。
- `capture_status: failed`
  说明没有拿到事件文件或事件文件为空，应先排查插件加载、OpenCode 启动和本机认证。
- `exit_code != 0` 且 `capture_status: success`
  说明执行失败，但抓取链路本身是通的。

## 维护说明

当前仓库不再提供实验矩阵、隔离检查、runner 容器启动脚本等运维入口；这些内容只作为历史资料保留在 legacy 文档中。
