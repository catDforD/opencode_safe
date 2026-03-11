from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ISSUES_PATH = REPO_ROOT / "tasks" / "issues.json"
DEFAULT_OPENCODE_BIN = "opencode"
PLUGIN_RELATIVE_PATH = Path(".opencode/plugins/mas_safe_security.ts")
SESSION_CONFIG_NAME = "opencode.json"
TASK_FILE_NAME = "TASK.md"
EVENT_LOG_RELATIVE_PATH = Path("events/opencode_events.jsonl")
RUN_METADATA_RELATIVE_PATH = Path("meta/run.json")
DERIVED_SUMMARY_RELATIVE_PATH = Path("derived/summary.json")
REPORT_RELATIVE_PATH = Path("report/index.html")
IGNORE_PATTERNS = (
    ".git",
    "artifacts",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    "*.pyc",
    "*.pyo",
)
NOISE_EVENT_TYPES = {
    "todo.updated",
    "session.updated",
    "session.diff",
}
NOISE_PREFIXES = (
    "message.",
    "lsp.",
    "tui.",
    "file.watcher.",
)
COMPLETION_EVENT_TYPES = {
    "session.error",
    "session.idle",
}
SIGNIFICANT_SYSTEM_EVENT_TYPES = {
    "server.instance.disposed",
}
SIGNIFICANT_GAP_THRESHOLD_SECONDS = 30.0
PHASE_ORDER = [
    "session_start",
    "analysis",
    "tool_work",
    "file_change",
    "completion",
]
PHASE_LABELS = {
    "session_start": "Session Start",
    "analysis": "Analysis Gap",
    "tool_work": "Tool Work",
    "file_change": "File Change",
    "completion": "Completion",
}
PHASE_DESCRIPTIONS = {
    "session_start": "Session creation and status changes before the first concrete action.",
    "analysis": "Suppressed chatter between session start and the first actionable event.",
    "tool_work": "Tool, permission, and command activity that moved the task forward.",
    "file_change": "Tracked file edits during the run.",
    "completion": "Terminal session events and runner shutdown.",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp_slug() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _load_catalog(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
        return [item for item in payload["issues"] if isinstance(item, dict)]
    raise ValueError(f"Unsupported issue catalog format: {path}")


def load_issue(issue_id: str, *, issues_path: Path = DEFAULT_ISSUES_PATH) -> dict[str, Any]:
    for issue in _load_catalog(issues_path):
        if issue.get("id") == issue_id:
            return issue
    raise KeyError(f"Issue not found: {issue_id}")


def _normalize_issue_files(issue: dict[str, Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for entry in issue.get("files", []):
        if isinstance(entry, str):
            normalized.append({"source": entry, "destination": entry})
            continue
        if isinstance(entry, dict):
            source = entry.get("source")
            destination = entry.get("destination", source)
            if isinstance(source, str) and isinstance(destination, str):
                normalized.append({"source": source, "destination": destination})
    return normalized


def _copy_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _default_opencode_config() -> dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
    }


def _stage_workspace(
    *,
    repo_root: Path,
    capture_dir: Path,
    plugin_source_path: Path,
    issue_files: list[dict[str, str]],
) -> Path:
    workspace = capture_dir / "workspace"
    shutil.copytree(repo_root, workspace, ignore=shutil.ignore_patterns(*IGNORE_PATTERNS))

    plugin_destination = workspace / PLUGIN_RELATIVE_PATH
    plugin_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plugin_source_path, plugin_destination)

    config_path = workspace / SESSION_CONFIG_NAME
    config_path.write_text(
        json.dumps(_default_opencode_config(), ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )

    for item in issue_files:
        source = repo_root / item["source"]
        if not source.exists():
            raise FileNotFoundError(f"Issue file not found: {source}")
        _copy_path(source, workspace / item["destination"])

    return workspace


def _build_prompt(issue: dict[str, Any], issue_files: list[dict[str, str]]) -> str:
    lines = [
        f"Issue ID: {issue['id']}",
        f"Title: {issue['title']}",
        "",
        str(issue.get("prompt", "")).strip(),
    ]
    if issue_files:
        lines.extend(["", "Relevant files:"])
        lines.extend(f"- {item['destination']}" for item in issue_files)
    lines.extend(
        [
            "",
            "Work only inside the current project directory.",
            "If you make changes, keep them local to this workspace.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _build_run_command(
    *,
    opencode_bin: str,
    issue: dict[str, Any],
    prompt: str,
    issue_files: list[dict[str, str]],
) -> list[str]:
    del issue_files
    command = [opencode_bin, "run", "--print-logs", "--title", str(issue["title"])]
    command.append(prompt)
    return command


def _iter_event_records(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return ()
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _find_nested_session_id(payload: Any) -> str | None:
    return _find_nested_value(payload, {"sessionID", "sessionId", "session_id", "id"})


def _find_nested_value(payload: Any, candidate_keys: set[str]) -> str | None:
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in candidate_keys and isinstance(value, (str, int, float)):
                    return str(value)
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return None


def extract_session_id(path: Path) -> str | None:
    for record in _iter_event_records(path):
        if not str(record.get("native_event_type", "")).startswith("session."):
            continue
        session_id = _find_nested_session_id(record.get("raw_output"))
        if session_id:
            return session_id
        session_id = _find_nested_session_id(record.get("raw_input"))
        if session_id:
            return session_id
    return None


def _count_events(path: Path) -> int:
    return sum(1 for _ in _iter_event_records(path))


def _run_command(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)


def _allocate_capture_dir(capture_root: Path, issue_id: str) -> tuple[str, Path]:
    slug = _timestamp_slug()
    capture_dir = capture_root / f"{slug}-{issue_id}"
    suffix = 2
    while capture_dir.exists():
        capture_dir = capture_root / f"{slug}-{issue_id}-{suffix:02d}"
        suffix += 1
    return slug, capture_dir


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    started = _parse_timestamp(started_at)
    finished = _parse_timestamp(finished_at)
    if started is None or finished is None:
        return None
    return round(max((finished - started).total_seconds(), 0.0), 3)


def _trim_text(value: Any, *, limit: int = 120) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _is_noise_event(event_type: str) -> bool:
    if event_type in NOISE_EVENT_TYPES:
        return True
    return event_type.startswith(NOISE_PREFIXES)


def _event_category(event_type: str) -> str | None:
    if event_type.startswith("session."):
        return "session"
    if event_type.startswith("tool.execute."):
        return "tool"
    if event_type.startswith("permission."):
        return "permission"
    if event_type.startswith("command.execute."):
        return "command"
    if event_type == "file.edited":
        return "file"
    if event_type in SIGNIFICANT_SYSTEM_EVENT_TYPES:
        return "system"
    return None


def _extract_status_name(record: dict[str, Any]) -> str | None:
    raw_input = record.get("raw_input")
    if not isinstance(raw_input, dict):
        return None
    properties = raw_input.get("properties")
    if not isinstance(properties, dict):
        return None
    status = properties.get("status")
    if isinstance(status, dict):
        return _trim_text(status.get("type"))
    return _trim_text(status)


def _extract_tool_name(record: dict[str, Any]) -> str | None:
    for source in (record.get("raw_input"), record.get("raw_output"), record.get("correlation")):
        if isinstance(source, dict):
            tool = source.get("tool")
            if isinstance(tool, str) and tool:
                return tool
    return None


def _extract_call_id(record: dict[str, Any]) -> str | None:
    return _find_nested_value(
        [record.get("raw_input"), record.get("raw_output"), record.get("correlation")],
        {"callID", "callId", "call_id"},
    )


def _extract_command_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("command", "cmd"):
            value = payload.get(key)
            if isinstance(value, str):
                return _trim_text(value, limit=160)
            if isinstance(value, list):
                joined = " ".join(str(item) for item in value)
                return _trim_text(joined, limit=160)
        args = payload.get("args")
        if isinstance(args, dict):
            return _extract_command_text(args)
    return None


def _normalize_file_path(file_path: str, workspace_dir: Path) -> str:
    candidate = Path(file_path)
    try:
        return candidate.relative_to(workspace_dir).as_posix()
    except ValueError:
        return candidate.as_posix()


def _extract_file_path(record: dict[str, Any], workspace_dir: Path) -> str | None:
    raw_input = record.get("raw_input")
    if not isinstance(raw_input, dict):
        return None
    properties = raw_input.get("properties")
    if not isinstance(properties, dict):
        return None
    file_path = properties.get("file")
    if not isinstance(file_path, str):
        return None
    return _normalize_file_path(file_path, workspace_dir)


def _extract_permission_label(record: dict[str, Any]) -> str | None:
    raw_input = record.get("raw_input")
    if not isinstance(raw_input, dict):
        return None
    for key in ("tool", "command", "name"):
        value = raw_input.get(key)
        text = _trim_text(value)
        if text:
            return text
    return _trim_text(raw_input.get("title"))


def _extract_error_text(payload: Any) -> str | None:
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                lower_key = key.lower()
                if lower_key in {"error", "stderr", "exception", "failure"} and isinstance(value, str) and value.strip():
                    return _trim_text(value, limit=160)
                if lower_key in {"ok", "success"} and value is False:
                    return f"{key}=false"
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return None


def _event_phase(record: dict[str, Any], event_type: str) -> str:
    category = _event_category(event_type)
    if category in {"tool", "permission", "command"}:
        return "tool_work"
    if category == "file":
        return "file_change"
    if event_type in COMPLETION_EVENT_TYPES:
        return "completion"
    if event_type == "session.status" and _extract_status_name(record) in {"idle", "error", "completed"}:
        return "completion"
    if category == "system":
        return "completion"
    return "session_start"


def _build_timeline_title(record: dict[str, Any], workspace_dir: Path) -> tuple[str, str | None]:
    event_type = str(record.get("native_event_type", ""))
    if event_type == "session.created":
        return "Session created", None
    if event_type == "session.status":
        status = _extract_status_name(record)
        return (
            f"Session {status}" if status else "Session status updated",
            None,
        )
    if event_type == "session.idle":
        return "Session idle", None
    if event_type == "session.error":
        error = _extract_error_text(record.get("raw_input")) or _extract_error_text(record.get("raw_output"))
        return "Session error", error
    if event_type == "tool.execute.before":
        tool_name = _extract_tool_name(record) or "unknown"
        return f"Tool started: {tool_name}", None
    if event_type == "tool.execute.after":
        tool_name = _extract_tool_name(record) or "unknown"
        error = _extract_error_text(record.get("raw_output"))
        return (
            f"Tool finished: {tool_name}",
            error or _trim_text(record.get("raw_output")),
        )
    if event_type == "file.edited":
        file_path = _extract_file_path(record, workspace_dir) or "unknown file"
        return f"Edited {file_path}", None
    if event_type.startswith("permission."):
        label = _extract_permission_label(record)
        event_label = "Permission requested" if event_type.endswith("ask") else "Permission updated"
        return event_label, label
    if event_type.startswith("command.execute."):
        command = _extract_command_text(record.get("raw_input")) or _extract_command_text(record.get("raw_output"))
        return "Command executed", command
    if event_type in SIGNIFICANT_SYSTEM_EVENT_TYPES:
        return "System event", event_type
    return event_type, None


def _normalize_timeline_event(
    record: dict[str, Any],
    *,
    workspace_dir: Path,
    index: int,
) -> dict[str, Any]:
    event_type = str(record.get("native_event_type", ""))
    title, detail = _build_timeline_title(record, workspace_dir)
    return {
        "index": index,
        "timestamp": record.get("timestamp"),
        "event_type": event_type,
        "category": _event_category(event_type),
        "phase": _event_phase(record, event_type),
        "title": title,
        "detail": detail,
        "tool_name": _extract_tool_name(record),
        "call_id": _extract_call_id(record),
        "file_path": _extract_file_path(record, workspace_dir),
        "status": _extract_status_name(record),
    }


def _build_tool_calls(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tools: list[dict[str, Any]] = []
    pending_by_key: dict[str, int] = {}
    pending_by_tool: dict[str, list[int]] = {}

    for record in records:
        event_type = str(record.get("native_event_type", ""))
        if not event_type.startswith("tool.execute."):
            continue

        tool_name = _extract_tool_name(record) or "unknown"
        call_id = _extract_call_id(record)
        timestamp = record.get("timestamp")

        if event_type == "tool.execute.before":
            summary = {
                "tool_name": tool_name,
                "call_id": call_id,
                "started_at": timestamp,
                "finished_at": None,
                "duration_seconds": None,
                "success": None,
                "error": None,
            }
            tools.append(summary)
            index = len(tools) - 1
            if call_id:
                pending_by_key[call_id] = index
            pending_by_tool.setdefault(tool_name, []).append(index)
            continue

        if event_type != "tool.execute.after":
            continue

        index: int | None = None
        if call_id and call_id in pending_by_key:
            index = pending_by_key.pop(call_id)
        else:
            candidates = pending_by_tool.get(tool_name, [])
            while candidates:
                candidate = candidates.pop(0)
                if tools[candidate]["finished_at"] is None:
                    index = candidate
                    break
        if index is None:
            tools.append(
                {
                    "tool_name": tool_name,
                    "call_id": call_id,
                    "started_at": None,
                    "finished_at": timestamp,
                    "duration_seconds": None,
                    "success": False,
                    "error": "after event without matching before event",
                }
            )
            continue

        current = tools[index]
        current["finished_at"] = timestamp
        current["duration_seconds"] = _duration_seconds(current["started_at"], timestamp)
        current["error"] = _extract_error_text(record.get("raw_output"))
        current["success"] = current["error"] is None

    anomalies: list[dict[str, Any]] = []
    for tool in tools:
        if tool["finished_at"] is None:
            tool["success"] = False
            tool["error"] = tool["error"] or "missing tool.execute.after event"
            anomalies.append(
                {
                    "code": "tool_missing_after",
                    "level": "warning",
                    "message": f"Tool '{tool['tool_name']}' did not emit a matching tool.execute.after event.",
                }
            )

    return tools, anomalies


def _build_file_summaries(records: list[dict[str, Any]], workspace_dir: Path) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    for record in records:
        if str(record.get("native_event_type", "")) != "file.edited":
            continue
        file_path = _extract_file_path(record, workspace_dir)
        if not file_path:
            continue
        summary = by_path.setdefault(
            file_path,
            {
                "path": file_path,
                "edit_count": 0,
                "first_edited_at": record.get("timestamp"),
                "last_edited_at": record.get("timestamp"),
            },
        )
        summary["edit_count"] += 1
        summary["last_edited_at"] = record.get("timestamp")
    return sorted(by_path.values(), key=lambda item: (-item["edit_count"], item["path"]))


def _count_events_between(
    timestamps: list[tuple[datetime, str]],
    started_at: datetime | None,
    finished_at: datetime | None,
) -> int:
    if started_at is None or finished_at is None:
        return 0
    return sum(1 for timestamp, _ in timestamps if started_at <= timestamp <= finished_at)


def _build_phases(
    timeline: list[dict[str, Any]],
    *,
    noise_timestamps: list[tuple[datetime, str]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for event in timeline:
        phase_name = str(event["phase"])
        phase = grouped.setdefault(
            phase_name,
            {
                "name": phase_name,
                "label": PHASE_LABELS[phase_name],
                "description": PHASE_DESCRIPTIONS[phase_name],
                "started_at": event["timestamp"],
                "finished_at": event["timestamp"],
                "duration_seconds": None,
                "event_count": 0,
                "noise_count": 0,
                "categories": [],
            },
        )
        phase["finished_at"] = event["timestamp"]
        phase["event_count"] += 1
        category = event.get("category")
        if isinstance(category, str) and category not in phase["categories"]:
            phase["categories"].append(category)

    for phase in grouped.values():
        phase["duration_seconds"] = _duration_seconds(phase["started_at"], phase["finished_at"])
        phase["noise_count"] = _count_events_between(
            noise_timestamps,
            _parse_timestamp(phase["started_at"]),
            _parse_timestamp(phase["finished_at"]),
        )

    session_phase = grouped.get("session_start")
    first_action = min(
        (
            _parse_timestamp(event["timestamp"])
            for event in timeline
            if event["phase"] in {"tool_work", "file_change"}
        ),
        default=None,
    )
    if session_phase and first_action:
        session_finished = _parse_timestamp(session_phase["finished_at"])
        if session_finished and first_action > session_finished:
            analysis_noise = _count_events_between(noise_timestamps, session_finished, first_action)
            grouped["analysis"] = {
                "name": "analysis",
                "label": PHASE_LABELS["analysis"],
                "description": PHASE_DESCRIPTIONS["analysis"],
                "started_at": _isoformat(session_finished),
                "finished_at": _isoformat(first_action),
                "duration_seconds": round((first_action - session_finished).total_seconds(), 3),
                "event_count": 0,
                "noise_count": analysis_noise,
                "categories": ["noise"] if analysis_noise else [],
            }

    phases = [grouped[name] for name in PHASE_ORDER if name in grouped]
    return phases


def _build_anomalies(
    *,
    run_metadata: dict[str, Any],
    timeline: list[dict[str, Any]],
    counts_by_type: Counter[str],
    tool_anomalies: list[dict[str, Any]],
    export_error: str | None,
) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    anomalies.extend(tool_anomalies)

    if run_metadata.get("execution_error"):
        anomalies.append(
            {
                "code": "execution_error",
                "level": "error",
                "message": str(run_metadata["execution_error"]),
            }
        )

    if not run_metadata.get("session_id"):
        anomalies.append(
            {
                "code": "missing_session_id",
                "level": "warning",
                "message": "No session_id could be extracted from the captured events.",
            }
        )

    tool_count = counts_by_type.get("tool.execute.before", 0)
    message_count = sum(count for event_type, count in counts_by_type.items() if event_type.startswith("message."))
    if tool_count == 0 and message_count > 0:
        anomalies.append(
            {
                "code": "message_only_run",
                "level": "warning",
                "message": "Captured message traffic but no tool execution events.",
            }
        )

    if run_metadata.get("event_count", 0) > 0 and not timeline:
        anomalies.append(
            {
                "code": "no_significant_events",
                "level": "warning",
                "message": "The capture contains events, but none were promoted into the significant timeline.",
            }
        )

    if export_error:
        anomalies.append(
            {
                "code": "export_parse_error",
                "level": "warning",
                "message": f"session/export.json could not be parsed: {export_error}",
            }
        )

    significant_times = [
        _parse_timestamp(event["timestamp"])
        for event in timeline
        if _parse_timestamp(event["timestamp"]) is not None
    ]
    for previous, current in zip(significant_times, significant_times[1:]):
        gap = (current - previous).total_seconds()
        if gap > SIGNIFICANT_GAP_THRESHOLD_SECONDS:
            anomalies.append(
                {
                    "code": "significant_gap",
                    "level": "info",
                    "message": f"Observed a {round(gap, 1)}s gap between significant events.",
                }
            )
            break

    if run_metadata.get("capture_status") == "success" and not any(
        event["phase"] == "completion" for event in timeline
    ):
        anomalies.append(
            {
                "code": "missing_completion_event",
                "level": "info",
                "message": "No explicit completion event was captured before the runner finished.",
            }
        )

    return anomalies


def build_capture_summary(capture_dir: Path) -> dict[str, Any]:
    run_metadata = json.loads((capture_dir / RUN_METADATA_RELATIVE_PATH).read_text(encoding="utf-8"))
    event_path = capture_dir / EVENT_LOG_RELATIVE_PATH
    records = list(_iter_event_records(event_path))
    counts_by_type: Counter[str] = Counter()
    noise_by_type: Counter[str] = Counter()
    noise_timestamps: list[tuple[datetime, str]] = []
    timeline: list[dict[str, Any]] = []

    workspace_dir = Path(str(run_metadata.get("workspace_dir", capture_dir / "workspace")))
    for record in records:
        event_type = str(record.get("native_event_type", ""))
        counts_by_type[event_type] += 1
        timestamp = _parse_timestamp(record.get("timestamp"))
        if _is_noise_event(event_type):
            noise_by_type[event_type] += 1
            if timestamp is not None:
                noise_timestamps.append((timestamp, event_type))
            continue
        category = _event_category(event_type)
        if category is None:
            continue
        timeline_event = _normalize_timeline_event(record, workspace_dir=workspace_dir, index=len(timeline) + 1)
        if (
            timeline
            and timeline_event["event_type"] == "session.status"
            and timeline[-1]["event_type"] == "session.status"
            and timeline[-1]["title"] == timeline_event["title"]
        ):
            continue
        timeline.append(timeline_event)

    tools, tool_anomalies = _build_tool_calls(records)
    files = _build_file_summaries(records, workspace_dir)

    export_error: str | None = None
    export_path = capture_dir / "session" / "export.json"
    if export_path.exists():
        try:
            json.loads(export_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            export_error = f"line {exc.lineno}, column {exc.colno}"

    anomalies = _build_anomalies(
        run_metadata=run_metadata,
        timeline=timeline,
        counts_by_type=counts_by_type,
        tool_anomalies=tool_anomalies,
        export_error=export_error,
    )

    summary = {
        "run": {
            "run_id": run_metadata.get("run_id"),
            "issue_id": run_metadata.get("issue_id"),
            "title": run_metadata.get("issue_title"),
            "started_at": run_metadata.get("started_at"),
            "finished_at": run_metadata.get("finished_at"),
            "duration_seconds": _duration_seconds(
                run_metadata.get("started_at"), run_metadata.get("finished_at")
            ),
            "exit_code": run_metadata.get("exit_code"),
            "capture_status": run_metadata.get("capture_status"),
            "event_count": run_metadata.get("event_count"),
            "session_id": run_metadata.get("session_id"),
            "export_saved": run_metadata.get("export_saved"),
        },
        "counts": {
            "by_type": dict(sorted(counts_by_type.items())),
            "significant_total": len(timeline),
            "noise_total": sum(noise_by_type.values()),
            "noise_by_type": dict(sorted(noise_by_type.items())),
        },
        "timeline": timeline,
        "phases": _build_phases(timeline, noise_timestamps=noise_timestamps),
        "tools": tools,
        "files": files,
        "anomalies": anomalies,
    }
    return summary


def _summary_json_script(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=True).replace("</", "<\\/")


def _render_capture_report_html(summary: dict[str, Any]) -> str:
    title = escape(f"OpenCode 运行报告 · {summary['run']['issue_id']}")
    summary_json = _summary_json_script(summary)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f6f1e8;
      --bg-strong: #efe5d6;
      --card: rgba(255, 252, 246, 0.92);
      --ink: #1f1d1a;
      --muted: #645e56;
      --accent: #005f73;
      --accent-soft: rgba(0, 95, 115, 0.12);
      --success: #2d6a4f;
      --warn: #bc6c25;
      --error: #9b2226;
      --border: rgba(31, 29, 26, 0.12);
      --shadow: 0 18px 50px rgba(67, 56, 42, 0.1);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(233, 196, 106, 0.22), transparent 35%),
        radial-gradient(circle at top right, rgba(0, 95, 115, 0.16), transparent 32%),
        linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
      line-height: 1.5;
    }}
    main {{
      width: min(1680px, calc(100vw - 64px));
      margin: 0 auto;
      padding: 36px 0 64px;
    }}
    .hero {{
      display: grid;
      gap: 18px;
      padding: 32px;
      background: linear-gradient(135deg, rgba(255,255,255,0.74), rgba(255,248,236,0.92));
      border: 1px solid var(--border);
      border-radius: 28px;
      box-shadow: var(--shadow);
    }}
    .eyebrow {{
      margin: 0;
      font-size: 12px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--accent);
    }}
    h1, h2, h3, p {{ margin: 0; }}
    h1 {{
      font-size: clamp(28px, 4vw, 46px);
      line-height: 1.05;
      max-width: 20ch;
    }}
    .subhead {{
      color: var(--muted);
      max-width: 68ch;
    }}
    .status-row, .card-grid, .panel-grid {{
      display: grid;
      gap: 16px;
    }}
    .status-row {{
      grid-template-columns: repeat(auto-fit, minmax(156px, 1fr));
    }}
    .stat, .panel, .timeline-item, details.phase {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 20px;
      box-shadow: var(--shadow);
    }}
    .stat {{
      padding: 16px 18px;
    }}
    .stat-label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .stat-value {{
      font-size: 24px;
      font-weight: 700;
      margin-top: 6px;
    }}
    section {{
      margin-top: 24px;
    }}
    .section-head {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .section-head p {{
      color: var(--muted);
      max-width: 72ch;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .filter-pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 9px 12px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 999px;
      font-size: 13px;
    }}
    .filter-pill input {{
      accent-color: var(--accent);
    }}
    .card-grid {{
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    }}
    details.phase {{
      overflow: hidden;
    }}
    details.phase summary {{
      cursor: pointer;
      list-style: none;
      padding: 18px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }}
    details.phase summary::-webkit-details-marker {{ display: none; }}
    .phase-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-size: 13px;
    }}
    .phase-body {{
      padding: 0 20px 20px;
      border-top: 1px solid var(--border);
      background: rgba(255,255,255,0.55);
    }}
    .timeline {{
      display: grid;
      gap: 12px;
    }}
    .timeline-item {{
      padding: 16px 18px;
      display: grid;
      gap: 8px;
    }}
    .timeline-top {{
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 12px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 600;
      background: var(--accent-soft);
      color: var(--accent);
    }}
    .badge.success {{ background: rgba(45, 106, 79, 0.12); color: var(--success); }}
    .badge.warn {{ background: rgba(188, 108, 37, 0.14); color: var(--warn); }}
    .badge.error {{ background: rgba(155, 34, 38, 0.14); color: var(--error); }}
    .muted {{ color: var(--muted); }}
    .panel-grid {{
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    }}
    .panel {{
      padding: 20px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      text-align: left;
      padding: 10px 0;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    ul.clean {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 10px;
    }}
    li.notice {{
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(255,255,255,0.68);
      border: 1px solid var(--border);
    }}
    code {{
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 0.95em;
    }}
    @media (min-width: 1500px) {{
      .hero {{
        grid-template-columns: minmax(0, 1.25fr) minmax(780px, 1fr);
        align-items: start;
      }}
      .hero > div:first-of-type {{
        padding-right: 12px;
      }}
      .status-row {{
        grid-template-columns: repeat(6, minmax(0, 1fr));
        align-self: end;
      }}
      .card-grid {{
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }}
      .panel-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}
    @media (max-width: 720px) {{
      main {{ width: min(100vw - 20px, 1680px); }}
      .hero {{ padding: 22px; border-radius: 22px; }}
      details.phase summary, .panel, .timeline-item {{ padding-left: 16px; padding-right: 16px; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <p class="eyebrow">OpenCode 运行报告</p>
      <div>
        <h1 id="hero-title"></h1>
        <p class="subhead" id="hero-subhead"></p>
      </div>
      <div class="status-row" id="overview-stats"></div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>阶段视图</h2>
          <p>按最能解释这次运行的阶段分组，高噪声消息流只折叠进统计，不直接展开。</p>
        </div>
      </div>
      <div class="card-grid" id="phases"></div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>关键时间线</h2>
          <p>这里只展示显著动作。可以用右侧分类筛选快速收窄视图。</p>
        </div>
        <div class="controls" id="filters"></div>
      </div>
      <div class="timeline" id="timeline"></div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>工具与文件</h2>
          <p>汇总这次运行调用了什么工具、改动了哪些文件。</p>
        </div>
      </div>
      <div class="panel-grid">
        <div class="panel">
          <h3>工具调用</h3>
          <div id="tools"></div>
        </div>
        <div class="panel">
          <h3>修改文件</h3>
          <div id="files"></div>
        </div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <div>
          <h2>噪声与异常</h2>
          <p>这里展示被时间线主动折叠的事件，以及数据质量相关的提醒。</p>
        </div>
      </div>
      <div class="panel-grid">
        <div class="panel">
          <h3>折叠事件统计</h3>
          <div id="noise"></div>
        </div>
        <div class="panel">
          <h3>异常提示</h3>
          <div id="anomalies"></div>
        </div>
      </div>
    </section>
  </main>

  <script id="summary-data" type="application/json">{summary_json}</script>
  <script>
    const summary = JSON.parse(document.getElementById("summary-data").textContent);
    const filters = new Set(summary.timeline.map((event) => event.category).filter(Boolean));
    const phaseLabels = {{
      session_start: "会话启动",
      analysis: "分析空档",
      tool_work: "工具执行",
      file_change: "文件修改",
      completion: "完成收尾",
    }};
    const phaseDescriptions = {{
      session_start: "首次动作发生前的会话建立与状态变化。",
      analysis: "会话启动后到首个关键动作之间，被折叠的消息噪声区间。",
      tool_work: "推动任务前进的工具、权限与命令动作。",
      file_change: "本次运行中发生的文件编辑。",
      completion: "运行结束前后的终态事件与收尾动作。",
    }};
    const categoryLabels = {{
      session: "会话",
      tool: "工具",
      permission: "权限",
      command: "命令",
      file: "文件",
      system: "系统",
    }};
    const statusLabels = {{
      success: "成功",
      failed: "失败",
    }};
    const levelLabels = {{
      info: "提示",
      warning: "警告",
      error: "错误",
    }};

    function formatTimestamp(value) {{
      if (!value) return "无";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString("zh-CN", {{ hour12: false }});
    }}

    function formatDuration(seconds) {{
      if (seconds === null || seconds === undefined) return "无";
      if (seconds < 1) return `${{seconds.toFixed(3)}}s`;
      if (seconds < 60) return `${{seconds.toFixed(1)}}s`;
      const minutes = Math.floor(seconds / 60);
      const remainder = seconds - minutes * 60;
      return `${{minutes}}m ${{remainder.toFixed(1)}}s`;
    }}

    function badgeClass(level) {{
      if (level === "success") return "badge success";
      if (level === "error") return "badge error";
      if (level === "warning") return "badge warn";
      return "badge";
    }}

    function labelForCategory(category) {{
      return categoryLabels[category] || category || "其他";
    }}

    function labelForPhase(phase) {{
      return phaseLabels[phase] || phase;
    }}

    function labelForStatus(status) {{
      return statusLabels[status] || status || "未知";
    }}

    function labelForLevel(level) {{
      return levelLabels[level] || level || "提示";
    }}

    function translateEventTitle(event) {{
      if (!event || !event.title) return "";
      if (event.title === "Session created") return "会话已创建";
      if (event.title === "Session idle") return "会话空闲";
      if (event.title === "Session error") return "会话错误";
      if (event.title.startsWith("Session ")) {{
        const status = event.title.slice("Session ".length);
        const mapped = {{
          busy: "忙碌",
          idle: "空闲",
          error: "错误",
          completed: "完成",
        }};
        return `会话状态：${{mapped[status] || status}}`;
      }}
      if (event.title.startsWith("Tool started: ")) {{
        return `工具开始：${{event.title.slice("Tool started: ".length)}}`;
      }}
      if (event.title.startsWith("Tool finished: ")) {{
        return `工具完成：${{event.title.slice("Tool finished: ".length)}}`;
      }}
      if (event.title.startsWith("Edited ")) {{
        return `已编辑 ${{event.title.slice("Edited ".length)}}`;
      }}
      if (event.title === "Permission requested") return "权限请求";
      if (event.title === "Permission updated") return "权限结果";
      if (event.title === "Command executed") return "命令执行";
      if (event.title === "System event") return "系统事件";
      return event.title;
    }}

    function renderOverview() {{
      document.getElementById("hero-title").textContent = summary.run.title || summary.run.issue_id;
      document.getElementById("hero-subhead").textContent =
        `问题 ${{summary.run.issue_id}} · 抓取状态 ${{labelForStatus(summary.run.capture_status)}} · 会话 ${{summary.run.session_id || "缺失"}}`;
      const items = [
        ["状态", labelForStatus(summary.run.capture_status)],
        ["时长", formatDuration(summary.run.duration_seconds)],
        ["退出码", summary.run.exit_code ?? "无"],
        ["事件总数", summary.run.event_count ?? 0],
        ["关键时间线", summary.counts.significant_total],
        ["折叠噪声", summary.counts.noise_total],
      ];
      document.getElementById("overview-stats").innerHTML = items.map(([label, value]) => `
        <div class="stat">
          <div class="stat-label">${{label}}</div>
          <div class="stat-value">${{value}}</div>
        </div>
      `).join("");
    }}

    function renderFilters() {{
      const ordered = Array.from(filters).sort();
      document.getElementById("filters").innerHTML = ordered.map((category) => `
        <label class="filter-pill">
          <input type="checkbox" data-filter="${{category}}" checked>
          <span>${{labelForCategory(category)}}</span>
        </label>
      `).join("");
      document.getElementById("filters").addEventListener("change", renderTimeline);
    }}

    function activeCategories() {{
      const checked = document.querySelectorAll("#filters input:checked");
      return new Set(Array.from(checked).map((input) => input.getAttribute("data-filter")));
    }}

    function renderPhases() {{
      const phaseEvents = new Map();
      summary.timeline.forEach((event) => {{
        const existing = phaseEvents.get(event.phase) || [];
        existing.push(event);
        phaseEvents.set(event.phase, existing);
      }});
      document.getElementById("phases").innerHTML = summary.phases.map((phase) => {{
        const events = phaseEvents.get(phase.name) || [];
        const items = events.slice(0, 6).map((event) => `
          <li>${{formatTimestamp(event.timestamp)}} · <strong>${{translateEventTitle(event)}}</strong>${{event.detail ? ` <span class="muted">· ${{event.detail}}</span>` : ""}}</li>
        `).join("");
        return `
          <details class="phase" open>
            <summary>
              <div>
                <h3>${{labelForPhase(phase.name)}}</h3>
                <p class="muted">${{phaseDescriptions[phase.name] || phase.description}}</p>
              </div>
              <div class="phase-meta">
                <span>${{formatDuration(phase.duration_seconds)}}</span>
                <span>${{phase.event_count}} 个关键事件</span>
                <span>${{phase.noise_count}} 个已折叠</span>
              </div>
            </summary>
            <div class="phase-body">
              <p class="muted">${{formatTimestamp(phase.started_at)}} 至 ${{formatTimestamp(phase.finished_at)}}</p>
              ${{items ? `<ul class="clean">${{items}}</ul>` : '<p class="muted">这个阶段没有关键事件，时间范围来自被折叠的噪声活动。</p>'}}
            </div>
          </details>
        `;
      }}).join("");
    }}

    function renderTimeline() {{
      const active = activeCategories();
      const events = summary.timeline.filter((event) => !event.category || active.has(event.category));
      document.getElementById("timeline").innerHTML = events.map((event) => `
        <article class="timeline-item">
          <div class="timeline-top">
            <div>
              <div class="${{badgeClass(event.phase === "completion" ? "success" : event.category === "permission" ? "warning" : "info")}}">${{labelForCategory(event.category)}} · ${{labelForPhase(event.phase)}}</div>
              <h3>${{translateEventTitle(event)}}</h3>
            </div>
            <div class="muted">${{formatTimestamp(event.timestamp)}}</div>
          </div>
          <div class="muted"><code>${{event.event_type}}</code></div>
          ${{event.detail ? `<div>${{event.detail}}</div>` : ""}}
          ${{event.file_path ? `<div class="muted">文件：<code>${{event.file_path}}</code></div>` : ""}}
          ${{event.tool_name ? `<div class="muted">工具：<code>${{event.tool_name}}</code></div>` : ""}}
        </article>
      `).join("") || '<div class="timeline-item"><p class="muted">当前筛选条件下没有可展示的时间线事件。</p></div>';
    }}

    function renderTools() {{
      if (!summary.tools.length) {{
        document.getElementById("tools").innerHTML = '<p class="muted">这次运行没有捕获到工具调用。</p>';
        return;
      }}
      document.getElementById("tools").innerHTML = `
        <table>
          <thead>
            <tr><th>工具</th><th>状态</th><th>时长</th><th>时间窗口</th></tr>
          </thead>
          <tbody>
            ${{summary.tools.map((tool) => `
              <tr>
                <td><code>${{tool.tool_name}}</code></td>
                <td><span class="${{badgeClass(tool.success ? "success" : "warning")}}">${{tool.success ? "完成" : "不完整"}}</span></td>
                <td>${{formatDuration(tool.duration_seconds)}}</td>
                <td class="muted">${{formatTimestamp(tool.started_at)}} → ${{formatTimestamp(tool.finished_at)}}</td>
              </tr>
            `).join("")}}
          </tbody>
        </table>
      `;
    }}

    function renderFiles() {{
      if (!summary.files.length) {{
        document.getElementById("files").innerHTML = '<p class="muted">这次运行没有捕获到文件编辑。</p>';
        return;
      }}
      document.getElementById("files").innerHTML = `
        <table>
          <thead>
            <tr><th>路径</th><th>编辑次数</th><th>时间窗口</th></tr>
          </thead>
          <tbody>
            ${{summary.files.map((file) => `
              <tr>
                <td><code>${{file.path}}</code></td>
                <td>${{file.edit_count}}</td>
                <td class="muted">${{formatTimestamp(file.first_edited_at)}} → ${{formatTimestamp(file.last_edited_at)}}</td>
              </tr>
            `).join("")}}
          </tbody>
        </table>
      `;
    }}

    function renderNoise() {{
      const entries = Object.entries(summary.counts.noise_by_type);
      if (!entries.length) {{
        document.getElementById("noise").innerHTML = '<p class="muted">没有需要折叠的噪声事件。</p>';
        return;
      }}
      document.getElementById("noise").innerHTML = `
        <ul class="clean">
          ${{entries.map(([eventType, count]) => `<li class="notice"><strong>${{count}}</strong> <code>${{eventType}}</code></li>`).join("")}}
        </ul>
      `;
    }}

    function renderAnomalies() {{
      if (!summary.anomalies.length) {{
        document.getElementById("anomalies").innerHTML = '<p class="muted">没有检测到异常。</p>';
        return;
      }}
      document.getElementById("anomalies").innerHTML = `
        <ul class="clean">
          ${{summary.anomalies.map((item) => `
            <li class="notice">
              <div class="${{badgeClass(item.level)}}">${{labelForLevel(item.level)}}</div>
              <p>${{item.message}}</p>
            </li>
          `).join("")}}
        </ul>
      `;
    }}

    renderOverview();
    renderFilters();
    renderPhases();
    renderTimeline();
    renderTools();
    renderFiles();
    renderNoise();
    renderAnomalies();
  </script>
</body>
</html>
"""


def render_capture_report(capture_dir: Path) -> dict[str, Any]:
    summary = build_capture_summary(capture_dir)
    summary_path = capture_dir / DERIVED_SUMMARY_RELATIVE_PATH
    report_path = capture_dir / REPORT_RELATIVE_PATH
    _write_json(summary_path, summary)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_capture_report_html(summary), encoding="utf-8")
    return {
        "summary_path": str(summary_path),
        "report_path": str(report_path),
    }


def run_capture(
    issue_id: str,
    *,
    opencode_bin: str = DEFAULT_OPENCODE_BIN,
    keep_workspace: bool = False,
    capture_root: Path | None = None,
    repo_root: Path = REPO_ROOT,
    issues_path: Path = DEFAULT_ISSUES_PATH,
    plugin_source_path: Path | None = None,
) -> dict[str, Any]:
    plugin_path = plugin_source_path or (repo_root / PLUGIN_RELATIVE_PATH)
    if not plugin_path.exists():
        raise FileNotFoundError(f"Plugin not found: {plugin_path}")

    issue = load_issue(issue_id, issues_path=issues_path)
    issue_files = _normalize_issue_files(issue)

    capture_root = capture_root or (repo_root / "artifacts" / "captures")
    _, capture_dir = _allocate_capture_dir(capture_root, issue_id)
    run_id = f"capture-{uuid.uuid4().hex[:12]}"

    capture_dir.mkdir(parents=True, exist_ok=False)
    (capture_dir / "events").mkdir(parents=True, exist_ok=True)
    (capture_dir / "logs").mkdir(parents=True, exist_ok=True)
    (capture_dir / "session").mkdir(parents=True, exist_ok=True)

    workspace = _stage_workspace(
        repo_root=repo_root,
        capture_dir=capture_dir,
        plugin_source_path=plugin_path,
        issue_files=issue_files,
    )
    prompt = _build_prompt(issue, issue_files)
    (workspace / TASK_FILE_NAME).write_text(prompt, encoding="utf-8")

    issue_snapshot = dict(issue)
    issue_snapshot["files"] = issue_files
    _write_json(capture_dir / "input" / "issue.json", issue_snapshot)

    event_path = capture_dir / EVENT_LOG_RELATIVE_PATH
    stdout_path = capture_dir / "logs" / "opencode.stdout.log"
    stderr_path = capture_dir / "logs" / "opencode.stderr.log"
    started_at = _utc_now().isoformat()

    env = os.environ.copy()
    env["MAS_RUN_ID"] = run_id
    env["MAS_CAPTURE_DIR"] = str(capture_dir)
    env["MAS_ISSUE_ID"] = issue_id

    command = _build_run_command(opencode_bin=opencode_bin, issue=issue, prompt=prompt, issue_files=issue_files)

    opencode_result: subprocess.CompletedProcess[str] | None = None
    execution_error: str | None = None
    try:
        opencode_result = _run_command(command, cwd=workspace, env=env)
        stdout_path.write_text(opencode_result.stdout, encoding="utf-8")
        stderr_path.write_text(opencode_result.stderr, encoding="utf-8")
    except FileNotFoundError as exc:
        execution_error = str(exc)
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(f"{execution_error}\n", encoding="utf-8")

    session_id = extract_session_id(event_path)
    event_count = _count_events(event_path)
    capture_status = "success" if event_count > 0 else "failed"

    export_saved = False
    export_result: subprocess.CompletedProcess[str] | None = None
    export_stdout_path = capture_dir / "logs" / "opencode_export.stdout.log"
    export_stderr_path = capture_dir / "logs" / "opencode_export.stderr.log"
    if session_id:
        export_command = [opencode_bin, "export", session_id, "--print-logs"]
        try:
            export_result = _run_command(export_command, cwd=workspace, env=env)
            export_stdout_path.write_text(export_result.stdout, encoding="utf-8")
            export_stderr_path.write_text(export_result.stderr, encoding="utf-8")
            if export_result.returncode == 0 and export_result.stdout.strip():
                (capture_dir / "session" / "export.json").write_text(export_result.stdout, encoding="utf-8")
                export_saved = True
        except FileNotFoundError as exc:
            export_stderr_path.write_text(f"{exc}\n", encoding="utf-8")

    finished_at = _utc_now().isoformat()
    run_metadata = {
        "run_id": run_id,
        "issue_id": issue_id,
        "issue_title": issue.get("title"),
        "capture_dir": str(capture_dir),
        "workspace_dir": str(workspace),
        "keep_workspace": keep_workspace,
        "opencode_bin": opencode_bin,
        "command": command,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": None if opencode_result is None else opencode_result.returncode,
        "execution_error": execution_error,
        "capture_status": capture_status,
        "event_count": event_count,
        "session_id": session_id,
        "export_saved": export_saved,
        "export_exit_code": None if export_result is None else export_result.returncode,
        "summary_path": None,
        "report_path": None,
        "report_error": None,
    }
    _write_json(capture_dir / RUN_METADATA_RELATIVE_PATH, run_metadata)

    try:
        report_artifacts = render_capture_report(capture_dir)
        run_metadata["summary_path"] = report_artifacts["summary_path"]
        run_metadata["report_path"] = report_artifacts["report_path"]
    except Exception as exc:  # pragma: no cover - defensive safeguard
        run_metadata["report_error"] = str(exc)
    _write_json(capture_dir / RUN_METADATA_RELATIVE_PATH, run_metadata)

    if not keep_workspace:
        shutil.rmtree(workspace)

    return run_metadata


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local OpenCode with capture plugin enabled.")
    parser.add_argument("--issue", required=True, help="Issue id from tasks/issues.json")
    parser.add_argument("--keep-workspace", action="store_true", help="Keep the temporary workspace copy")
    parser.add_argument("--opencode-bin", default=DEFAULT_OPENCODE_BIN, help="Path to the opencode binary")
    parser.add_argument("--capture-root", type=Path, help="Override artifacts/captures output directory")
    parser.add_argument("--issues-path", type=Path, help="Override tasks/issues.json path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    metadata = run_capture(
        args.issue,
        opencode_bin=args.opencode_bin,
        keep_workspace=args.keep_workspace,
        capture_root=args.capture_root,
        issues_path=args.issues_path or DEFAULT_ISSUES_PATH,
    )
    print(metadata["capture_dir"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
