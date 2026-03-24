# Local OpenCode Capture Operations

## 基本运行

```bash
./scripts/run_opencode_capture.sh --issue fix_math_utils
```

## 保留工作副本

```bash
./scripts/run_opencode_capture.sh --issue fix_math_utils --keep-workspace
```

## 指定容器内 opencode 二进制名或自定义镜像

```bash
./scripts/run_opencode_capture.sh --issue summarize_release_notes --opencode-bin opencode
./scripts/run_opencode_capture.sh --issue summarize_release_notes --image-ref ghcr.io/anomalyco/opencode:latest
```

## 查看结果

脚本结束时会打印本次 capture 目录路径。进入该目录后，优先看这几个文件：

- `meta/run.json`
- `observations/process_tree.json`
- `observations/fs_diff.json`
- `observations/network.json`
- `events/opencode_events.jsonl` 可选
- `logs/opencode.stderr.log`
- `session/export.json`

## 常见判断

- `capture_valid: true` 且 `native_events_status: missing`
  说明外置采集链路是通的，但 OpenCode 原生事件没有拿到。这种 run 仍然是有效失败样本。
- `run_status: timeout`
  说明容器触发了 wall clock timeout，被 supervisor 主动停止。
- `run_status: oom_killed`
  说明容器被内核 OOM 终止。
- `run_status: failed` 且 `capture_valid: true`
  说明执行失败，但外置观测已经完整落盘。

## 维护说明

当前仓库不再依赖 audit endpoint 作为主采集通路；`infra/audit` 只保留为历史兼容组件。批量运行统一走 `./scripts/run_all_captures.sh`，默认固定并发 1，并按 `safe -> mild -> severe` 排序。
