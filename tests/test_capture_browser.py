import json
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest

from orchestrator.capture_browser import CaptureSessionStore, dispatch_capture_browser_request
from orchestrator.opencode_plugin_runner import (
    DERIVED_SUMMARY_RELATIVE_PATH,
    EVENT_LOG_RELATIVE_PATH,
    FS_DIFF_RELATIVE_PATH,
    NETWORK_RELATIVE_PATH,
    PROCESS_TREE_RELATIVE_PATH,
    REPORT_RELATIVE_PATH,
    render_capture_report,
)


def _sample_events(capture_dir: Path) -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-03-10T10:53:26.001Z",
            "native_event_type": "session.created",
            "raw_input": {"sessionId": f"session-{capture_dir.name}"},
            "raw_output": {},
        },
        {
            "timestamp": "2026-03-10T10:53:39.001Z",
            "native_event_type": "tool.execute.before",
            "raw_input": {
                "tool": "read",
                "sessionID": f"session-{capture_dir.name}",
                "callID": f"call-{capture_dir.name}",
            },
            "raw_output": {"args": {"filePath": "math_utils.py"}},
            "correlation": {"tool": "read", "callID": f"call-{capture_dir.name}"},
        },
        {
            "timestamp": "2026-03-10T10:53:40.001Z",
            "native_event_type": "tool.execute.after",
            "raw_input": {
                "tool": "read",
                "sessionID": f"session-{capture_dir.name}",
                "callID": f"call-{capture_dir.name}",
            },
            "raw_output": {"title": "ok"},
            "correlation": {"tool": "read", "callID": f"call-{capture_dir.name}"},
        },
    ]


def _write_capture(
    captures_root: Path,
    capture_id: str,
    *,
    with_summary: bool,
    with_events: bool = True,
    issue_id: str = "fix_math_utils",
    run_status: str = "completed",
    native_events_status: str = "present",
    started_at: str = "2026-03-10T10:53:26+00:00",
) -> Path:
    capture_dir = captures_root / capture_id
    (capture_dir / "events").mkdir(parents=True, exist_ok=True)
    (capture_dir / "meta").mkdir(parents=True, exist_ok=True)
    (capture_dir / "session").mkdir(parents=True, exist_ok=True)
    (capture_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (capture_dir / "observations").mkdir(parents=True, exist_ok=True)

    metadata = {
        "run_id": f"capture-{capture_id}",
        "issue_id": issue_id,
        "issue_title": f"title-{issue_id}",
        "risk_level": "safe" if not issue_id.startswith("severe_") else "severe",
        "image_ref": "example/opencode:test",
        "capture_dir": str(capture_dir),
        "workspace_dir": str(capture_dir / "workspace"),
        "keep_workspace": True,
        "docker_bin": "docker",
        "opencode_bin": "opencode",
        "command": ["opencode", "run"],
        "resource_limits": {"cpus": "2", "memory": "4g", "pids": 256},
        "started_at": started_at,
        "finished_at": "2026-03-10T10:55:24+00:00",
        "duration_seconds": 118.0,
        "run_status": run_status,
        "capture_status": run_status,
        "capture_valid": True,
        "container_exit_code": 0,
        "exit_code": 0,
        "execution_error": None,
        "timed_out": run_status == "timeout",
        "oom_killed": run_status == "oom_killed",
        "timeout_seconds": 300,
        "native_events_status": native_events_status,
        "event_count": 3 if with_events else 0,
        "session_id": f"session-{capture_id}" if with_events else None,
        "export_saved": False,
        "export_exit_code": None,
        "summary_path": None,
        "report_path": None,
        "report_error": None,
    }
    (capture_dir / "meta" / "run.json").write_text(json.dumps(metadata), encoding="utf-8")
    (capture_dir / PROCESS_TREE_RELATIVE_PATH).write_text(
        json.dumps({"snapshot_count": 1, "snapshots": [], "processes": [{"pid": "101", "command": "opencode"}]}),
        encoding="utf-8",
    )
    (capture_dir / NETWORK_RELATIVE_PATH).write_text(
        json.dumps({"snapshot_count": 1, "snapshots": [], "connections": [{"proto": "tcp", "remote": "198.51.100.10:443", "state": "ESTAB"}]}),
        encoding="utf-8",
    )
    (capture_dir / FS_DIFF_RELATIVE_PATH).write_text(
        json.dumps({"counts": {"created": 1, "modified": 0, "deleted": 0, "total": 1}, "changes": [{"path": "RESULT.md", "change_type": "created", "sensitive_path": False}]}),
        encoding="utf-8",
    )
    if with_events:
        (capture_dir / EVENT_LOG_RELATIVE_PATH).write_text(
            "\n".join(json.dumps(event) for event in _sample_events(capture_dir)) + "\n",
            encoding="utf-8",
        )
    if with_summary:
        render_capture_report(capture_dir)
    return capture_dir


class CaptureBrowserTests(unittest.TestCase):
    def test_store_refresh_backfills_missing_summary_and_sorts_desc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captures_root = Path(tmp) / "captures"
            newer = _write_capture(
                captures_root,
                "20260310T105326Z-fix_math_utils",
                with_summary=False,
                with_events=False,
                native_events_status="missing",
                started_at="2026-03-10T10:53:26+00:00",
            )
            _write_capture(
                captures_root,
                "20260310T104758Z-fix_math_utils",
                with_summary=True,
                started_at="2026-03-10T10:47:58+00:00",
            )
            broken = captures_root / "20260310T000000Z-broken"
            (broken / "meta").mkdir(parents=True, exist_ok=True)
            (broken / "meta" / "run.json").write_text("{not-valid", encoding="utf-8")

            store = CaptureSessionStore(captures_root=captures_root)
            sessions = store.refresh()

            self.assertEqual(
                [session["capture_id"] for session in sessions],
                [
                    "20260310T105326Z-fix_math_utils",
                    "20260310T104758Z-fix_math_utils",
                ],
            )
            self.assertTrue((newer / DERIVED_SUMMARY_RELATIVE_PATH).exists())
            self.assertTrue((newer / REPORT_RELATIVE_PATH).exists())
            self.assertTrue(sessions[0]["has_summary"])
            self.assertTrue(sessions[0]["report_ready"])
            self.assertEqual(sessions[0]["native_events_status"], "missing")
            self.assertTrue(sessions[0]["capture_valid"])

    def test_session_detail_returns_summary_and_new_run_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captures_root = Path(tmp) / "captures"
            _write_capture(captures_root, "20260310T105326Z-fix_math_utils", with_summary=True, run_status="timeout")

            store = CaptureSessionStore(captures_root=captures_root)
            store.refresh()
            detail = store.get_session_detail("20260310T105326Z-fix_math_utils")

            assert detail is not None
            self.assertIsNotNone(detail["summary"])
            self.assertNotIn("capture_dir", detail["run"])
            self.assertNotIn("workspace_dir", detail["run"])
            self.assertEqual(detail["run"]["run_status"], "timeout")
            self.assertIn("capture_valid", detail["session"])
            self.assertEqual(detail["paths"]["summary"], "20260310T105326Z-fix_math_utils/derived/summary.json")
            self.assertEqual(detail["paths"]["report"], "20260310T105326Z-fix_math_utils/report/index.html")

    def test_http_api_routes_index_sessions_details_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captures_root = Path(tmp) / "captures"
            _write_capture(captures_root, "20260310T105326Z-fix_math_utils", with_summary=False, with_events=False, native_events_status="missing")

            store = CaptureSessionStore(captures_root=captures_root)
            store.refresh()

            status, content_type, index_html = dispatch_capture_browser_request(store, "GET", "/")
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(content_type, "text/html; charset=utf-8")
            self.assertIn("OpenCode 会话浏览", index_html)
            self.assertIn('id="session-select"', index_html)
            self.assertIn("/api/sessions", index_html)

            status, _, sessions_payload = dispatch_capture_browser_request(store, "GET", "/api/sessions")
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(len(sessions_payload["sessions"]), 1)
            self.assertEqual(sessions_payload["sessions"][0]["run_status"], "completed")
            self.assertEqual(sessions_payload["sessions"][0]["native_events_status"], "missing")
            capture_id = sessions_payload["sessions"][0]["capture_id"]

            status, _, detail_payload = dispatch_capture_browser_request(store, "GET", f"/api/sessions/{capture_id}")
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(detail_payload["session"]["capture_id"], capture_id)
            self.assertIsNotNone(detail_payload["summary"])
            self.assertEqual(detail_payload["summary"]["run"]["native_events_status"], "missing")

            _write_capture(
                captures_root,
                "20260311T105326Z-severe_md_python_setup",
                with_summary=False,
                issue_id="severe_md_python_setup",
                run_status="oom_killed",
                native_events_status="present",
            )
            status, _, refresh_payload = dispatch_capture_browser_request(store, "POST", "/api/refresh")
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(
                [item["capture_id"] for item in refresh_payload["sessions"]],
                [
                    "20260311T105326Z-severe_md_python_setup",
                    "20260310T105326Z-fix_math_utils",
                ],
            )
            self.assertEqual(refresh_payload["sessions"][0]["run_status"], "oom_killed")


if __name__ == "__main__":
    unittest.main()
