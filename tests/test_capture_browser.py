import json
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest

from orchestrator.capture_browser import CaptureSessionStore, dispatch_capture_browser_request
from orchestrator.opencode_plugin_runner import DERIVED_SUMMARY_RELATIVE_PATH, EVENT_LOG_RELATIVE_PATH, REPORT_RELATIVE_PATH, render_capture_report


def _sample_events(capture_dir: Path) -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-03-10T10:53:26.001Z",
            "native_event_type": "session.created",
            "raw_input": {"sessionId": f"session-{capture_dir.name}"},
            "raw_output": {},
        },
        {
            "timestamp": "2026-03-10T10:53:29.001Z",
            "native_event_type": "session.status",
            "raw_input": {
                "type": "session.status",
                "properties": {
                    "sessionID": f"session-{capture_dir.name}",
                    "status": {"type": "busy"},
                },
            },
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
        {
            "timestamp": "2026-03-10T10:53:58.001Z",
            "native_event_type": "file.edited",
            "raw_input": {
                "type": "file.edited",
                "properties": {
                    "file": str(capture_dir / "workspace" / "tasks/fixtures/fix_math_utils/math_utils.py"),
                },
            },
            "raw_output": {},
        },
        {
            "timestamp": "2026-03-10T10:55:24.001Z",
            "native_event_type": "session.idle",
            "raw_input": {"type": "session.idle"},
            "raw_output": {},
        },
    ]


def _write_capture(
    captures_root: Path,
    capture_id: str,
    *,
    with_summary: bool,
    issue_id: str = "fix_math_utils",
    started_at: str = "2026-03-10T10:53:26+00:00",
) -> Path:
    capture_dir = captures_root / capture_id
    (capture_dir / "events").mkdir(parents=True, exist_ok=True)
    (capture_dir / "meta").mkdir(parents=True, exist_ok=True)
    (capture_dir / "session").mkdir(parents=True, exist_ok=True)
    (capture_dir / "workspace").mkdir(parents=True, exist_ok=True)

    metadata = {
        "run_id": f"capture-{capture_id}",
        "issue_id": issue_id,
        "issue_title": f"title-{issue_id}",
        "capture_dir": str(capture_dir),
        "workspace_dir": str(capture_dir / "workspace"),
        "keep_workspace": True,
        "opencode_bin": "opencode",
        "command": ["opencode", "run"],
        "started_at": started_at,
        "finished_at": "2026-03-10T10:55:24+00:00",
        "exit_code": 0,
        "execution_error": None,
        "capture_status": "success",
        "event_count": 6,
        "session_id": f"session-{capture_id}",
        "export_saved": False,
        "export_exit_code": None,
        "summary_path": None,
        "report_path": None,
        "report_error": None,
    }
    (capture_dir / "meta" / "run.json").write_text(json.dumps(metadata), encoding="utf-8")
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

            self.assertEqual([session["capture_id"] for session in sessions], [
                "20260310T105326Z-fix_math_utils",
                "20260310T104758Z-fix_math_utils",
            ])
            self.assertTrue((newer / DERIVED_SUMMARY_RELATIVE_PATH).exists())
            self.assertTrue((newer / REPORT_RELATIVE_PATH).exists())
            self.assertTrue(sessions[0]["has_summary"])
            self.assertTrue(sessions[0]["report_ready"])

    def test_session_detail_returns_summary_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captures_root = Path(tmp) / "captures"
            _write_capture(captures_root, "20260310T105326Z-fix_math_utils", with_summary=True)

            store = CaptureSessionStore(captures_root=captures_root)
            store.refresh()
            detail = store.get_session_detail("20260310T105326Z-fix_math_utils")

            assert detail is not None
            self.assertIsNotNone(detail["summary"])
            self.assertNotIn("capture_dir", detail["run"])
            self.assertNotIn("workspace_dir", detail["run"])
            self.assertEqual(detail["paths"]["summary"], "20260310T105326Z-fix_math_utils/derived/summary.json")
            self.assertEqual(detail["paths"]["report"], "20260310T105326Z-fix_math_utils/report/index.html")

    def test_http_api_routes_index_sessions_details_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captures_root = Path(tmp) / "captures"
            _write_capture(captures_root, "20260310T105326Z-fix_math_utils", with_summary=False)

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
            capture_id = sessions_payload["sessions"][0]["capture_id"]

            status, _, detail_payload = dispatch_capture_browser_request(store, "GET", f"/api/sessions/{capture_id}")
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(detail_payload["session"]["capture_id"], capture_id)
            self.assertIsNotNone(detail_payload["summary"])

            _write_capture(captures_root, "20260311T105326Z-summarize_release_notes", with_summary=False, issue_id="summarize_release_notes")
            status, _, refresh_payload = dispatch_capture_browser_request(store, "POST", "/api/refresh")
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(
                [item["capture_id"] for item in refresh_payload["sessions"]],
                [
                    "20260311T105326Z-summarize_release_notes",
                    "20260310T105326Z-fix_math_utils",
                ],
            )


if __name__ == "__main__":
    unittest.main()
