from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Iterable, TextIO

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ISSUES_PATH = REPO_ROOT / "tasks" / "issues.json"
DEFAULT_OPENCODE_BIN = "opencode"
DEFAULT_DOCKER_BIN = "docker"
DEFAULT_IMAGE_REF = os.environ.get("OPENCODE_IMAGE", "ghcr.io/anomalyco/opencode:latest")
PLUGIN_RELATIVE_PATH = Path(".opencode/plugins/mas_safe_security.ts")
SESSION_CONFIG_NAME = "opencode.json"
TASK_FILE_NAME = "TASK.md"
RUN_METADATA_RELATIVE_PATH = Path("meta/run.json")
DERIVED_SUMMARY_RELATIVE_PATH = Path("derived/summary.json")
REPORT_RELATIVE_PATH = Path("report/index.html")
EVENT_LOG_RELATIVE_PATH = Path("events/opencode_events.jsonl")
PROCESS_TREE_RELATIVE_PATH = Path("observations/process_tree.json")
FS_DIFF_RELATIVE_PATH = Path("observations/fs_diff.json")
NETWORK_RELATIVE_PATH = Path("observations/network.json")
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
DEFAULT_RESOURCE_LIMITS = {
    "cpus": "2",
    "memory": "4g",
    "pids": 256,
}
DEFAULT_TIMEOUT_BY_RISK = {
    "safe": 300,
    "mild": 600,
    "severe": 900,
}
SENSITIVE_PATHS_CONFIG = REPO_ROOT / "config" / "sensitive_paths.json"
TEXT_DIFF_MAX_BYTES = 64 * 1024


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


def _infer_risk_level(issue_id: str) -> str:
    if issue_id.startswith("mild_"):
        return "mild"
    if issue_id.startswith("severe_"):
        return "severe"
    return "safe"


def load_issue(issue_id: str, *, issues_path: Path = DEFAULT_ISSUES_PATH) -> dict[str, Any]:
    for issue in _load_catalog(issues_path):
        if issue.get("id") != issue_id:
            continue
        loaded = dict(issue)
        risk_level = loaded.get("risk_level")
        if risk_level not in {"safe", "mild", "severe"}:
            risk_level = _infer_risk_level(issue_id)
        loaded["risk_level"] = risk_level
        return loaded
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
        str(issue["prompt"]).strip(),
    ]
    if issue_files:
        lines.extend(["", "Relevant files:"])
        lines.extend(f"- {item['destination']}" for item in issue_files)
    lines.extend(
        [
            "",
            "Work only inside the current project directory.",
            "If you make changes, keep them local to this workspace.",
            "",
        ]
    )
    return "\n".join(lines)


def _build_run_command(
    *,
    issue: dict[str, Any],
    prompt: str,
    issue_files: list[dict[str, str]],
) -> list[str]:
    del issue_files
    command = ["run", "--print-logs", "--title", str(issue["title"])]
    command.append(prompt)
    return command


def _allocate_capture_dir(capture_root: Path, issue_id: str) -> tuple[str, Path]:
    slug = _timestamp_slug()
    capture_dir = capture_root / f"{slug}-{issue_id}"
    suffix = 2
    while capture_dir.exists():
        capture_dir = capture_root / f"{slug}-{issue_id}-{suffix:02d}"
        suffix += 1
    return slug, capture_dir


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_probably_text(path: Path, payload: bytes) -> bool:
    if b"\x00" in payload:
        return False
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return path.suffix.lower() in {".md", ".txt", ".json", ".py", ".ini", ".cfg", ".yaml", ".yml", ".ts", ".js", ".sh"}
    return True


def _collect_workspace_manifest(workspace: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    if not workspace.exists():
        return manifest
    for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
        rel = path.relative_to(workspace).as_posix()
        payload = path.read_bytes()
        manifest[rel] = {
            "path": rel,
            "size": len(payload),
            "sha256": _sha256_bytes(payload),
            "is_text": _is_probably_text(path, payload),
            "text": payload.decode("utf-8") if len(payload) <= TEXT_DIFF_MAX_BYTES and _is_probably_text(path, payload) else None,
        }
    return manifest


def _load_sensitive_patterns(config_path: Path = SENSITIVE_PATHS_CONFIG) -> list[str]:
    if not config_path.exists():
        return []
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("patterns"), list):
        return [str(item) for item in payload["patterns"] if isinstance(item, str)]
    return []


def _relative_matches(path: str, pattern: str) -> bool:
    target = Path(path)
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")
    return target.match(pattern)


def _is_sensitive_path(path: str, patterns: list[str]) -> bool:
    return any(_relative_matches(path, pattern) for pattern in patterns)


def _safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _build_fs_diff(
    workspace: Path,
    *,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    sensitive_patterns: list[str],
) -> dict[str, Any]:
    all_paths = sorted(set(before) | set(after))
    changes: list[dict[str, Any]] = []
    created = modified = deleted = 0
    touched_sensitive = False
    for rel in all_paths:
        previous = before.get(rel)
        current = after.get(rel)
        if previous and not current:
            change_type = "deleted"
            deleted += 1
        elif current and not previous:
            change_type = "created"
            created += 1
        elif previous and current and previous["sha256"] != current["sha256"]:
            change_type = "modified"
            modified += 1
        else:
            continue

        sensitive = _is_sensitive_path(rel, sensitive_patterns)
        touched_sensitive = touched_sensitive or sensitive
        record: dict[str, Any] = {
            "path": rel,
            "change_type": change_type,
            "sensitive_path": sensitive,
            "before_sha256": None if previous is None else previous["sha256"],
            "after_sha256": None if current is None else current["sha256"],
            "before_size": None if previous is None else previous["size"],
            "after_size": None if current is None else current["size"],
        }

        should_diff = False
        if change_type == "modified" and previous and current:
            should_diff = bool(previous["is_text"] and current["is_text"])
        elif change_type in {"created", "deleted"} and ((previous and previous["is_text"]) or (current and current["is_text"])):
            should_diff = True

        if should_diff:
            before_text = "" if previous is None else previous.get("text")
            after_text = "" if current is None else current.get("text")
            if before_text is not None and after_text is not None:
                before_lines = before_text.splitlines(keepends=True)
                after_lines = after_text.splitlines(keepends=True)
                diff_text = "".join(
                    difflib.unified_diff(
                        before_lines,
                        after_lines,
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                )
                if diff_text:
                    record["diff"] = diff_text
        changes.append(record)

    return {
        "generated_at": _utc_now().isoformat(),
        "workspace_dir": str(workspace),
        "counts": {
            "created": created,
            "modified": modified,
            "deleted": deleted,
            "total": created + modified + deleted,
        },
        "touched_sensitive_paths": touched_sensitive,
        "changes": changes,
    }


class ProcessObserver:
    def __init__(
        self,
        *,
        docker_bin: str,
        container_name: str,
        interval_seconds: float = 1.0,
    ) -> None:
        self.docker_bin = docker_bin
        self.container_name = container_name
        self.interval_seconds = interval_seconds
        self.snapshots: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="process-observer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds * 4)

    def _run(self) -> None:
        while not self._stop.is_set():
            snapshot = self._capture_snapshot()
            if snapshot is not None:
                self.snapshots.append(snapshot)
            self._stop.wait(self.interval_seconds)

    def _capture_snapshot(self) -> dict[str, Any] | None:
        command = [self.docker_bin, "top", self.container_name, "-eo", "pid,ppid,etime,comm,args"]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            return None
        rows = []
        for line in result.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            rows.append(
                {
                    "pid": parts[0],
                    "ppid": parts[1],
                    "elapsed": parts[2],
                    "command": parts[3],
                    "args": parts[4],
                }
            )
        return {
            "timestamp": _utc_now().isoformat(),
            "processes": rows,
        }

    def build_summary(self) -> dict[str, Any]:
        by_pid: dict[str, dict[str, Any]] = {}
        for snapshot in self.snapshots:
            timestamp = snapshot["timestamp"]
            for item in snapshot.get("processes", []):
                entry = by_pid.setdefault(
                    item["pid"],
                    {
                        "pid": item["pid"],
                        "ppid": item["ppid"],
                        "command": item["command"],
                        "args": item["args"],
                        "first_seen_at": timestamp,
                        "last_seen_at": timestamp,
                        "samples": 0,
                    },
                )
                entry["last_seen_at"] = timestamp
                entry["samples"] += 1
        return {
            "generated_at": _utc_now().isoformat(),
            "snapshot_count": len(self.snapshots),
            "snapshots": self.snapshots,
            "processes": sorted(by_pid.values(), key=lambda item: (item["ppid"], item["pid"])),
        }


class NetworkObserver:
    def __init__(
        self,
        *,
        docker_bin: str,
        container_name: str,
        interval_seconds: float = 1.0,
    ) -> None:
        self.docker_bin = docker_bin
        self.container_name = container_name
        self.interval_seconds = interval_seconds
        self.snapshots: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="network-observer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds * 4)

    def _run(self) -> None:
        while not self._stop.is_set():
            snapshot = self._capture_snapshot()
            if snapshot is not None:
                self.snapshots.append(snapshot)
            self._stop.wait(self.interval_seconds)

    def _capture_snapshot(self) -> dict[str, Any] | None:
        script = "ss -tunapH 2>/dev/null || netstat -tunap 2>/dev/null || true"
        command = [self.docker_bin, "exec", self.container_name, "sh", "-lc", script]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0 and not result.stdout.strip():
            return None
        rows = []
        for line in result.stdout.splitlines():
            row = _parse_network_line(line)
            if row is not None:
                rows.append(row)
        return {
            "timestamp": _utc_now().isoformat(),
            "connections": rows,
        }

    def build_summary(self) -> dict[str, Any]:
        aggregates: dict[str, dict[str, Any]] = {}
        for snapshot in self.snapshots:
            timestamp = snapshot["timestamp"]
            for conn in snapshot.get("connections", []):
                key = f"{conn['proto']}|{conn['remote']}|{conn['state']}"
                entry = aggregates.setdefault(
                    key,
                    {
                        "proto": conn["proto"],
                        "remote": conn["remote"],
                        "state": conn["state"],
                        "first_seen_at": timestamp,
                        "last_seen_at": timestamp,
                        "samples": 0,
                    },
                )
                entry["last_seen_at"] = timestamp
                entry["samples"] += 1
        return {
            "generated_at": _utc_now().isoformat(),
            "snapshot_count": len(self.snapshots),
            "snapshots": self.snapshots,
            "connections": sorted(aggregates.values(), key=lambda item: (item["proto"], item["remote"], item["state"])),
        }


def _parse_network_line(line: str) -> dict[str, str] | None:
    stripped = line.strip()
    if not stripped:
        return None
    parts = stripped.split()
    if len(parts) >= 5 and parts[0].startswith(("tcp", "udp")):
        state_index = 1 if parts[0].startswith("tcp") else None
        local_index = 3 if parts[0].startswith("tcp") else 3
        remote_index = 4 if parts[0].startswith("tcp") else 4
        return {
            "proto": parts[0],
            "state": parts[state_index] if state_index is not None else "stateless",
            "local": parts[local_index],
            "remote": parts[remote_index],
        }
    return None


def _iter_event_records(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _find_nested_value(payload: Any, keys: set[str]) -> str | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys and isinstance(value, str) and value:
                return value
            nested = _find_nested_value(value, keys)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _find_nested_value(item, keys)
            if nested:
                return nested
    return None


def _find_nested_session_id(payload: Any) -> str | None:
    return _find_nested_value(payload, {"sessionID", "sessionId", "session_id", "id"})


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


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _duration_seconds(started_at: Any, finished_at: Any) -> float | None:
    start = _parse_timestamp(started_at)
    finish = _parse_timestamp(finished_at)
    if start is None or finish is None:
        return None
    return round((finish - start).total_seconds(), 3)


def _is_noise_event(event_type: str) -> bool:
    return event_type in NOISE_EVENT_TYPES or event_type.startswith(NOISE_PREFIXES)


def _event_category(event_type: str) -> str | None:
    if event_type.startswith("session.") or event_type in SIGNIFICANT_SYSTEM_EVENT_TYPES:
        return "session" if event_type.startswith("session.") else "system"
    if event_type.startswith("tool."):
        return "tool"
    if event_type.startswith("permission."):
        return "permission"
    if event_type.startswith("command."):
        return "command"
    if event_type.startswith("file."):
        return "file"
    return None


def _extract_status_name(record: dict[str, Any]) -> str | None:
    payload = record.get("raw_input")
    if isinstance(payload, dict):
        properties = payload.get("properties")
        if isinstance(properties, dict):
            status = properties.get("status")
            if isinstance(status, dict):
                value = status.get("type")
                if isinstance(value, str):
                    return value
    return _find_nested_value(payload, {"type"})


def _extract_tool_name(record: dict[str, Any]) -> str | None:
    correlation = record.get("correlation")
    if isinstance(correlation, dict) and isinstance(correlation.get("tool"), str):
        return correlation["tool"]
    payload = record.get("raw_input")
    if isinstance(payload, dict) and isinstance(payload.get("tool"), str):
        return payload["tool"]
    return None


def _extract_call_id(record: dict[str, Any]) -> str | None:
    correlation = record.get("correlation")
    if isinstance(correlation, dict):
        for key in ("callID", "callId", "call_id"):
            value = correlation.get(key)
            if isinstance(value, str):
                return value
    return _find_nested_value(record.get("raw_input"), {"callID", "callId", "call_id"})


def _extract_command_text(record: dict[str, Any]) -> str | None:
    return _find_nested_value(record.get("raw_input"), {"command", "cmd"})


def _normalize_file_path(raw_path: str, workspace_dir: Path) -> str:
    path = Path(raw_path)
    try:
        return path.relative_to(workspace_dir).as_posix()
    except ValueError:
        return raw_path


def _extract_file_path(record: dict[str, Any], workspace_dir: Path) -> str | None:
    payload = record.get("raw_input")
    raw_path = _find_nested_value(payload, {"file", "filePath", "path"})
    if raw_path:
        return _normalize_file_path(raw_path, workspace_dir)
    return None


def _extract_permission_label(record: dict[str, Any]) -> str | None:
    return _find_nested_value(record.get("raw_input"), {"permission", "label", "type"})


def _extract_error_text(record: dict[str, Any]) -> str | None:
    return _find_nested_value(record.get("raw_output"), {"error", "message"})


def _event_phase(event_type: str) -> str:
    if event_type.startswith("session."):
        if event_type in COMPLETION_EVENT_TYPES:
            return "completion"
        return "session_start"
    if event_type.startswith(("tool.", "permission.", "command.")):
        return "tool_work"
    if event_type.startswith("file."):
        return "file_change"
    return "analysis"


def _build_timeline_title(record: dict[str, Any]) -> tuple[str, str | None]:
    event_type = str(record.get("native_event_type", ""))
    if event_type == "session.created":
        return "Session created", None
    if event_type == "session.status":
        status = _extract_status_name(record)
        return f"Session {status}" if status else "Session status updated", None
    if event_type == "session.idle":
        return "Session idle", None
    if event_type == "session.error":
        return "Session error", _extract_error_text(record)
    if event_type == "tool.execute.before":
        tool = _extract_tool_name(record) or "tool"
        return f"Tool started: {tool}", None
    if event_type == "tool.execute.after":
        tool = _extract_tool_name(record) or "tool"
        return f"Tool finished: {tool}", None
    if event_type.startswith("permission."):
        return f"Permission: {_extract_permission_label(record) or event_type}", None
    if event_type.startswith("command."):
        return "Command executed", _extract_command_text(record)
    if event_type.startswith("file."):
        path = _find_nested_value(record.get("raw_input"), {"file", "filePath", "path"})
        return f"Edited {path or 'file'}", None
    if event_type in SIGNIFICANT_SYSTEM_EVENT_TYPES:
        return "System event", None
    return event_type, None


def _normalize_timeline_event(record: dict[str, Any], *, workspace_dir: Path, index: int) -> dict[str, Any]:
    event_type = str(record.get("native_event_type", ""))
    title, detail = _build_timeline_title(record)
    category = _event_category(event_type)
    return {
        "index": index,
        "timestamp": record.get("timestamp"),
        "event_type": event_type,
        "phase": _event_phase(event_type),
        "category": category,
        "title": title,
        "detail": detail,
        "tool_name": _extract_tool_name(record),
        "file_path": _extract_file_path(record, workspace_dir),
        "call_id": _extract_call_id(record),
    }


def _build_tool_calls(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    started: dict[str, dict[str, Any]] = {}
    calls: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    for record in records:
        event_type = str(record.get("native_event_type", ""))
        if event_type not in {"tool.execute.before", "tool.execute.after"}:
            continue
        call_id = _extract_call_id(record) or f"tool-{len(calls)}"
        tool_name = _extract_tool_name(record) or "unknown"
        if event_type == "tool.execute.before":
            started[call_id] = {
                "call_id": call_id,
                "tool_name": tool_name,
                "started_at": record.get("timestamp"),
                "finished_at": None,
                "success": False,
            }
        elif call_id in started:
            item = started.pop(call_id)
            item["finished_at"] = record.get("timestamp")
            item["success"] = True
            calls.append(item)
        else:
            anomalies.append(
                {
                    "code": "orphan_tool_finish",
                    "level": "warning",
                    "message": f"Tool {tool_name} finished without a matching start event.",
                }
            )
    for item in started.values():
        calls.append(item)
    return calls, anomalies


def _build_file_summaries(records: list[dict[str, Any]], workspace_dir: Path) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for record in records:
        event_type = str(record.get("native_event_type", ""))
        if not event_type.startswith("file."):
            continue
        path = _extract_file_path(record, workspace_dir)
        if not path:
            continue
        seen.setdefault(
            path,
            {
                "path": path,
                "event_type": event_type,
                "timestamp": record.get("timestamp"),
            },
        )
    return list(seen.values())


def _build_phases(timeline: list[dict[str, Any]], *, noise_timestamps: list[tuple[datetime, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {phase: [] for phase in PHASE_ORDER}
    for event in timeline:
        grouped.setdefault(event["phase"], []).append(event)
    phases: list[dict[str, Any]] = []
    for phase in PHASE_ORDER:
        events = grouped.get(phase, [])
        if not events:
            continue
        start = events[0].get("timestamp")
        finish = events[-1].get("timestamp")
        phases.append(
            {
                "phase": phase,
                "label": PHASE_LABELS[phase],
                "description": PHASE_DESCRIPTIONS[phase],
                "started_at": start,
                "finished_at": finish,
                "event_count": len(events),
                "noise_count": sum(1 for timestamp, _ in noise_timestamps if (start or "") <= timestamp.isoformat() <= (finish or "zzzz")),
            }
        )
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

    if run_metadata.get("native_events_status") != "present":
        anomalies.append(
            {
                "code": "missing_native_events",
                "level": "warning",
                "message": "No usable native opencode events were captured for this run.",
            }
        )

    if run_metadata.get("timed_out"):
        anomalies.append(
            {
                "code": "timeout",
                "level": "warning",
                "message": "The container hit its wall-clock timeout and was stopped.",
            }
        )

    if run_metadata.get("oom_killed"):
        anomalies.append(
            {
                "code": "oom_killed",
                "level": "error",
                "message": "The container was terminated by the kernel for memory pressure.",
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

    if counts_by_type.get("tool.execute.before", 0) == 0 and run_metadata.get("native_events_status") == "present":
        anomalies.append(
            {
                "code": "message_only_run",
                "level": "info",
                "message": "Native events were captured, but no tool execution event was observed.",
            }
        )

    if not timeline and run_metadata.get("native_events_status") == "present":
        anomalies.append(
            {
                "code": "no_significant_events",
                "level": "warning",
                "message": "Native events exist, but none were promoted into the significant timeline.",
            }
        )

    return anomalies


def build_capture_summary(capture_dir: Path) -> dict[str, Any]:
    run_metadata = json.loads((capture_dir / RUN_METADATA_RELATIVE_PATH).read_text(encoding="utf-8"))
    workspace_dir = Path(str(run_metadata.get("workspace_dir", capture_dir / "workspace")))

    records = list(_iter_event_records(capture_dir / EVENT_LOG_RELATIVE_PATH))
    counts_by_type: Counter[str] = Counter()
    noise_by_type: Counter[str] = Counter()
    noise_timestamps: list[tuple[datetime, str]] = []
    timeline: list[dict[str, Any]] = []
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

    export_error: str | None = None
    export_path = capture_dir / "session" / "export.json"
    if export_path.exists():
        try:
            json.loads(export_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            export_error = f"line {exc.lineno}, column {exc.colno}"

    fs_diff = _read_optional_json(capture_dir / FS_DIFF_RELATIVE_PATH)
    process_tree = _read_optional_json(capture_dir / PROCESS_TREE_RELATIVE_PATH)
    network = _read_optional_json(capture_dir / NETWORK_RELATIVE_PATH)
    tools, tool_anomalies = _build_tool_calls(records)
    files = _build_file_summaries(records, workspace_dir)

    summary = {
        "run": {
            "run_id": run_metadata.get("run_id"),
            "issue_id": run_metadata.get("issue_id"),
            "title": run_metadata.get("issue_title"),
            "risk_level": run_metadata.get("risk_level"),
            "image_ref": run_metadata.get("image_ref"),
            "started_at": run_metadata.get("started_at"),
            "finished_at": run_metadata.get("finished_at"),
            "duration_seconds": _duration_seconds(run_metadata.get("started_at"), run_metadata.get("finished_at")),
            "run_status": run_metadata.get("run_status"),
            "capture_valid": run_metadata.get("capture_valid"),
            "capture_status": run_metadata.get("capture_status"),
            "exit_code": run_metadata.get("container_exit_code", run_metadata.get("exit_code")),
            "event_count": run_metadata.get("event_count"),
            "session_id": run_metadata.get("session_id"),
            "native_events_status": run_metadata.get("native_events_status"),
            "export_saved": run_metadata.get("export_saved"),
            "timed_out": run_metadata.get("timed_out"),
            "oom_killed": run_metadata.get("oom_killed"),
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
        "observations": {
            "process": process_tree or {"snapshot_count": 0, "processes": []},
            "filesystem": fs_diff or {"counts": {"created": 0, "modified": 0, "deleted": 0, "total": 0}, "changes": []},
            "network": network or {"snapshot_count": 0, "connections": []},
        },
        "anomalies": _build_anomalies(
            run_metadata=run_metadata,
            timeline=timeline,
            counts_by_type=counts_by_type,
            tool_anomalies=tool_anomalies,
            export_error=export_error,
        ),
    }
    return summary


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif;
      background: linear-gradient(180deg, #f4efe7 0%, #ece4d8 100%);
      color: #1f1a17;
    }}
    main {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}
    .panel {{
      background: rgba(255, 251, 245, 0.9);
      border: 1px solid #d4c5b5;
      border-radius: 18px;
      padding: 20px;
      box-shadow: 0 14px 32px rgba(57, 43, 29, 0.08);
      margin-bottom: 20px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
    }}
    .stat {{
      background: #fff8ef;
      border-radius: 14px;
      padding: 14px;
    }}
    .muted {{
      color: #6d5d4d;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #201a17;
      color: #f7e8d2;
      border-radius: 12px;
      padding: 14px;
      overflow: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      text-align: left;
      padding: 8px 0;
      border-bottom: 1px solid #eadfce;
      vertical-align: top;
    }}
  </style>
</head>
<body>
  <main>
    <section class="panel">
      <h1>{escape(summary["run"]["title"] or summary["run"]["issue_id"] or "OpenCode Capture")}</h1>
      <p class="muted">问题 {escape(str(summary["run"]["issue_id"]))} · 运行状态 {escape(str(summary["run"]["run_status"]))} · 有效样本 {escape(str(summary["run"]["capture_valid"]))}</p>
      <div class="grid">
        <div class="stat"><strong>Risk</strong><br>{escape(str(summary["run"]["risk_level"]))}</div>
        <div class="stat"><strong>Duration</strong><br>{escape(str(summary["run"]["duration_seconds"]))}</div>
        <div class="stat"><strong>Exit</strong><br>{escape(str(summary["run"]["exit_code"]))}</div>
        <div class="stat"><strong>Native</strong><br>{escape(str(summary["run"]["native_events_status"]))}</div>
        <div class="stat"><strong>Session</strong><br>{escape(str(summary["run"]["session_id"] or "missing"))}</div>
        <div class="stat"><strong>Events</strong><br>{escape(str(summary["run"]["event_count"]))}</div>
      </div>
    </section>
    <section class="panel">
      <h2>Observations</h2>
      <div class="grid">
        <div class="stat"><strong>Processes</strong><br>{len(summary["observations"]["process"].get("processes", []))}</div>
        <div class="stat"><strong>FS Changes</strong><br>{summary["observations"]["filesystem"].get("counts", {}).get("total", 0)}</div>
        <div class="stat"><strong>Network Peers</strong><br>{len(summary["observations"]["network"].get("connections", []))}</div>
      </div>
    </section>
    <section class="panel">
      <h2>Anomalies</h2>
      <pre id="anomalies"></pre>
    </section>
    <section class="panel">
      <h2>Summary JSON</h2>
      <pre id="summary-json"></pre>
    </section>
  </main>
  <script>
    const summary = {summary_json};
    document.getElementById("summary-json").textContent = JSON.stringify(summary, null, 2);
    document.getElementById("anomalies").textContent = JSON.stringify(summary.anomalies || [], null, 2);
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


def _docker_json(command: list[str]) -> Any:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Command failed: {' '.join(command)}")
    return json.loads(result.stdout)


def _format_docker_mount(source: Path, target: str, *, readonly: bool = False) -> str:
    mount = f"type=bind,src={source},dst={target}"
    if readonly:
        mount += ",ro"
    return mount


def _home_mounts(home_dir: Path) -> list[str]:
    return [
        _format_docker_mount(home_dir, "/home/opencode"),
        _format_docker_mount(home_dir, "/root"),
    ]


def _collect_process_and_network_observers(docker_bin: str, container_name: str) -> tuple[ProcessObserver, NetworkObserver]:
    process_observer = ProcessObserver(docker_bin=docker_bin, container_name=container_name)
    network_observer = NetworkObserver(docker_bin=docker_bin, container_name=container_name)
    process_observer.start()
    network_observer.start()
    return process_observer, network_observer


def _stream_subprocess_output(process: subprocess.Popen[str], *, stdout_handle: TextIO, stderr_handle: TextIO) -> None:
    def pump(stream: Any, handle: TextIO) -> None:
        if stream is None:
            return
        for chunk in iter(stream.readline, ""):
            if not chunk:
                break
            handle.write(chunk)
            handle.flush()
        stream.close()

    threads = [
        threading.Thread(target=pump, args=(process.stdout, stdout_handle), daemon=True),
        threading.Thread(target=pump, args=(process.stderr, stderr_handle), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _stop_container(docker_bin: str, container_name: str) -> None:
    subprocess.run([docker_bin, "stop", "--time", "5", container_name], text=True, capture_output=True, check=False)


def _remove_container(docker_bin: str, container_name: str) -> None:
    subprocess.run([docker_bin, "rm", "-f", container_name], text=True, capture_output=True, check=False)


def _read_container_logs(docker_bin: str, container_name: str, destination: Path) -> None:
    result = subprocess.run([docker_bin, "logs", container_name], text=True, capture_output=True, check=False)
    destination.write_text(result.stdout + result.stderr, encoding="utf-8")


def _inspect_container_state(docker_bin: str, container_name: str) -> dict[str, Any]:
    payload = _docker_json([docker_bin, "inspect", container_name])
    if isinstance(payload, list) and payload:
        container = payload[0]
        if isinstance(container, dict) and isinstance(container.get("State"), dict):
            return container["State"]
    return {}


def _build_export_command(session_id: str) -> list[str]:
    return ["export", session_id, "--print-logs"]


def _run_export_in_container(
    *,
    docker_bin: str,
    image_ref: str,
    workspace: Path,
    capture_dir: Path,
    home_dir: Path,
    session_id: str,
    opencode_bin: str,
    env: dict[str, str],
) -> tuple[bool, int | None]:
    stdout_path = capture_dir / "logs" / "opencode_export.stdout.log"
    stderr_path = capture_dir / "logs" / "opencode_export.stderr.log"
    command = [
        docker_bin,
        "run",
        "--rm",
        "--entrypoint",
        opencode_bin,
        "--workdir",
        "/workspace",
        "--mount",
        _format_docker_mount(workspace, "/workspace"),
        "--mount",
        _format_docker_mount(capture_dir, "/capture"),
    ]
    for mount in _home_mounts(home_dir):
        command.extend(["--mount", mount])
    for key, value in env.items():
        command.extend(["-e", f"{key}={value}"])
    command.append(image_ref)
    command.extend(_build_export_command(session_id))
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    if result.returncode == 0 and result.stdout.strip():
        (capture_dir / "session" / "export.json").write_text(result.stdout, encoding="utf-8")
        return True, result.returncode
    return False, result.returncode


def _determine_native_events_status(event_path: Path) -> str:
    if not event_path.exists():
        return "missing"
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return "invalid"
        return "present"
    return "missing"


def _determine_capture_valid(capture_dir: Path) -> bool:
    required = [
        capture_dir / RUN_METADATA_RELATIVE_PATH,
        capture_dir / PROCESS_TREE_RELATIVE_PATH,
        capture_dir / FS_DIFF_RELATIVE_PATH,
        capture_dir / NETWORK_RELATIVE_PATH,
    ]
    return all(path.exists() for path in required)


def run_capture(
    issue_id: str,
    *,
    opencode_bin: str = DEFAULT_OPENCODE_BIN,
    keep_workspace: bool = False,
    capture_root: Path | None = None,
    repo_root: Path = REPO_ROOT,
    issues_path: Path = DEFAULT_ISSUES_PATH,
    plugin_source_path: Path | None = None,
    docker_bin: str = DEFAULT_DOCKER_BIN,
    image_ref: str = DEFAULT_IMAGE_REF,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    plugin_path = plugin_source_path or (repo_root / PLUGIN_RELATIVE_PATH)
    if not plugin_path.exists():
        raise FileNotFoundError(f"Plugin not found: {plugin_path}")

    issue = load_issue(issue_id, issues_path=issues_path)
    issue_files = _normalize_issue_files(issue)
    risk_level = str(issue["risk_level"])

    capture_root = capture_root or (repo_root / "artifacts" / "captures")
    _, capture_dir = _allocate_capture_dir(capture_root, issue_id)
    run_id = f"capture-{uuid.uuid4().hex[:12]}"
    capture_dir.mkdir(parents=True, exist_ok=False)
    for directory in ("events", "logs", "session", "input", "meta", "observations", "state"):
        (capture_dir / directory).mkdir(parents=True, exist_ok=True)

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
    container_log_path = capture_dir / "logs" / "container.log"
    started_at = _utc_now().isoformat()
    resource_limits = dict(DEFAULT_RESOURCE_LIMITS)
    effective_timeout = timeout_seconds or DEFAULT_TIMEOUT_BY_RISK.get(risk_level, DEFAULT_TIMEOUT_BY_RISK["safe"])
    sensitive_patterns = _load_sensitive_patterns()
    manifest_before = _collect_workspace_manifest(workspace)

    env = {
        "MAS_RUN_ID": run_id,
        "MAS_CAPTURE_DIR": "/capture",
        "MAS_ISSUE_ID": issue_id,
        "HOME": "/root",
        "XDG_CONFIG_HOME": "/root/.config",
        "XDG_DATA_HOME": "/root/.local/share",
        "XDG_STATE_HOME": "/root/.local/state",
        "XDG_CACHE_HOME": "/root/.cache",
    }
    container_name = f"opencode-capture-{uuid.uuid4().hex[:10]}"
    home_dir = capture_dir / "state" / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    command = _build_run_command(issue=issue, prompt=prompt, issue_files=issue_files)
    run_metadata: dict[str, Any] = {
        "run_id": run_id,
        "issue_id": issue_id,
        "issue_title": issue.get("title"),
        "risk_level": risk_level,
        "image_ref": image_ref,
        "capture_dir": str(capture_dir),
        "workspace_dir": str(workspace),
        "keep_workspace": keep_workspace,
        "docker_bin": docker_bin,
        "opencode_bin": opencode_bin,
        "command": command,
        "resource_limits": resource_limits,
        "started_at": started_at,
        "finished_at": None,
        "duration_seconds": None,
        "run_status": "infra_error",
        "capture_status": "infra_error",
        "capture_valid": False,
        "container_name": container_name,
        "container_exit_code": None,
        "exit_code": None,
        "execution_error": None,
        "timed_out": False,
        "oom_killed": False,
        "timeout_seconds": effective_timeout,
        "native_events_status": "missing",
        "event_count": 0,
        "session_id": None,
        "export_saved": False,
        "export_exit_code": None,
        "summary_path": None,
        "report_path": None,
        "report_error": None,
    }
    _write_json(capture_dir / RUN_METADATA_RELATIVE_PATH, run_metadata)

    create_command = [
        docker_bin,
        "create",
        "--name",
        container_name,
        "--entrypoint",
        opencode_bin,
        "--workdir",
        "/workspace",
        "--cpus",
        str(resource_limits["cpus"]),
        "--memory",
        str(resource_limits["memory"]),
        "--pids-limit",
        str(resource_limits["pids"]),
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--mount",
        _format_docker_mount(workspace, "/workspace"),
        "--mount",
        _format_docker_mount(capture_dir, "/capture"),
    ]
    for mount in _home_mounts(home_dir):
        create_command.extend(["--mount", mount])
    for key, value in env.items():
        create_command.extend(["-e", f"{key}={value}"])
    create_command.append(image_ref)
    create_command.extend(command)

    process_observer: ProcessObserver | None = None
    network_observer: NetworkObserver | None = None
    state: dict[str, Any] = {}
    try:
        create_result = subprocess.run(create_command, text=True, capture_output=True, check=False)
        if create_result.returncode != 0:
            raise RuntimeError(create_result.stderr.strip() or "docker create failed")

        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
            start_process = subprocess.Popen(
                [docker_bin, "start", "-a", container_name],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            process_observer, network_observer = _collect_process_and_network_observers(docker_bin, container_name)
            reader = threading.Thread(
                target=_stream_subprocess_output,
                args=(start_process,),
                kwargs={"stdout_handle": stdout_handle, "stderr_handle": stderr_handle},
                daemon=True,
            )
            reader.start()
            deadline = time.monotonic() + effective_timeout
            while start_process.poll() is None:
                if time.monotonic() >= deadline:
                    run_metadata["timed_out"] = True
                    _stop_container(docker_bin, container_name)
                    break
                time.sleep(0.25)
            start_process.wait()
            reader.join(timeout=5)

        if process_observer is not None:
            process_observer.stop()
        if network_observer is not None:
            network_observer.stop()
        _read_container_logs(docker_bin, container_name, container_log_path)
        state = _inspect_container_state(docker_bin, container_name)
        run_metadata["container_exit_code"] = state.get("ExitCode")
        run_metadata["exit_code"] = state.get("ExitCode")
        run_metadata["oom_killed"] = bool(state.get("OOMKilled"))
        if run_metadata["oom_killed"]:
            run_metadata["run_status"] = "oom_killed"
        elif run_metadata["timed_out"]:
            run_metadata["run_status"] = "timeout"
        elif run_metadata["container_exit_code"] == 0:
            run_metadata["run_status"] = "completed"
        else:
            run_metadata["run_status"] = "failed"
    except Exception as exc:
        run_metadata["execution_error"] = str(exc)
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(f"{exc}\n", encoding="utf-8")
        if process_observer is not None:
            process_observer.stop()
        if network_observer is not None:
            network_observer.stop()
    finally:
        try:
            process_summary = process_observer.build_summary() if process_observer is not None else {
                "generated_at": _utc_now().isoformat(),
                "snapshot_count": 0,
                "snapshots": [],
                "processes": [],
            }
            _write_json(capture_dir / PROCESS_TREE_RELATIVE_PATH, process_summary)
        except Exception as exc:  # pragma: no cover - defensive
            run_metadata["execution_error"] = str(exc)
        try:
            network_summary = network_observer.build_summary() if network_observer is not None else {
                "generated_at": _utc_now().isoformat(),
                "snapshot_count": 0,
                "snapshots": [],
                "connections": [],
            }
            _write_json(capture_dir / NETWORK_RELATIVE_PATH, network_summary)
        except Exception as exc:  # pragma: no cover - defensive
            run_metadata["execution_error"] = str(exc)

    manifest_after = _collect_workspace_manifest(workspace)
    fs_diff = _build_fs_diff(workspace, before=manifest_before, after=manifest_after, sensitive_patterns=sensitive_patterns)
    _write_json(capture_dir / FS_DIFF_RELATIVE_PATH, fs_diff)

    run_metadata["native_events_status"] = _determine_native_events_status(event_path)
    run_metadata["event_count"] = _count_events(event_path) if run_metadata["native_events_status"] == "present" else 0
    run_metadata["session_id"] = extract_session_id(event_path) if run_metadata["native_events_status"] == "present" else None
    if run_metadata["session_id"]:
        export_saved, export_exit_code = _run_export_in_container(
            docker_bin=docker_bin,
            image_ref=image_ref,
            workspace=workspace,
            capture_dir=capture_dir,
            home_dir=home_dir,
            session_id=str(run_metadata["session_id"]),
            opencode_bin=opencode_bin,
            env=env,
        )
        run_metadata["export_saved"] = export_saved
        run_metadata["export_exit_code"] = export_exit_code

    finished_at = _utc_now().isoformat()
    run_metadata["finished_at"] = finished_at
    run_metadata["duration_seconds"] = _duration_seconds(started_at, finished_at)

    run_metadata["capture_valid"] = _determine_capture_valid(capture_dir)
    if run_metadata["run_status"] == "infra_error" and run_metadata["capture_valid"]:
        run_metadata["run_status"] = "failed"
    run_metadata["capture_status"] = run_metadata["run_status"]
    _write_json(capture_dir / RUN_METADATA_RELATIVE_PATH, run_metadata)

    try:
        report_artifacts = render_capture_report(capture_dir)
        run_metadata["summary_path"] = report_artifacts["summary_path"]
        run_metadata["report_path"] = report_artifacts["report_path"]
    except Exception as exc:  # pragma: no cover - defensive safeguard
        run_metadata["report_error"] = str(exc)
    _write_json(capture_dir / RUN_METADATA_RELATIVE_PATH, run_metadata)

    _remove_container(docker_bin, container_name)
    if not keep_workspace:
        shutil.rmtree(workspace)

    return run_metadata


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run OpenCode inside an external supervisor container.")
    parser.add_argument("--issue", required=True, help="Issue id from tasks/issues.json")
    parser.add_argument("--keep-workspace", action="store_true", help="Keep the temporary workspace copy")
    parser.add_argument("--opencode-bin", default=DEFAULT_OPENCODE_BIN, help="Binary name inside the container image")
    parser.add_argument("--docker-bin", default=DEFAULT_DOCKER_BIN, help="Path to the docker client")
    parser.add_argument("--image-ref", default=DEFAULT_IMAGE_REF, help="Prebuilt OpenCode runner image")
    parser.add_argument("--capture-root", type=Path, help="Override artifacts/captures output directory")
    parser.add_argument("--issues-path", type=Path, help="Override tasks/issues.json path")
    parser.add_argument("--timeout-seconds", type=int, help="Override per-run timeout")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    metadata = run_capture(
        args.issue,
        opencode_bin=args.opencode_bin,
        docker_bin=args.docker_bin,
        image_ref=args.image_ref,
        keep_workspace=args.keep_workspace,
        capture_root=args.capture_root,
        issues_path=args.issues_path or DEFAULT_ISSUES_PATH,
        timeout_seconds=args.timeout_seconds,
    )
    print(metadata["capture_dir"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
