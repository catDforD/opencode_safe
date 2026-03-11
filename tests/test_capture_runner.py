import json
from pathlib import Path
import stat
import tempfile
import textwrap
import unittest

from orchestrator.opencode_plugin_runner import (
    DERIVED_SUMMARY_RELATIVE_PATH,
    EVENT_LOG_RELATIVE_PATH,
    REPORT_RELATIVE_PATH,
    build_capture_summary,
    extract_session_id,
    load_issue,
    render_capture_report,
    run_capture,
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _copy_repo_subset(source_root: Path, destination_root: Path) -> None:
    for relative_path in [
        Path(".opencode/plugins/mas_safe_security.ts"),
        Path("tasks/issues.json"),
        Path("tasks/fixtures/fix_math_utils/math_utils.py"),
        Path("tasks/fixtures/fix_math_utils/test_math_utils.py"),
        Path("tasks/fixtures/summarize_release_notes/release_notes.md"),
    ]:
        source = source_root / relative_path
        destination = destination_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _write_capture_fixture(
    capture_dir: Path,
    *,
    workspace_dir: Path,
    events: list[dict[str, object]],
    export_text: str | None = None,
) -> None:
    (capture_dir / "events").mkdir(parents=True, exist_ok=True)
    (capture_dir / "meta").mkdir(parents=True, exist_ok=True)
    (capture_dir / "session").mkdir(parents=True, exist_ok=True)
    metadata = {
        "run_id": "capture-test",
        "issue_id": "fix_math_utils",
        "issue_title": "Fix the broken add helper",
        "capture_dir": str(capture_dir),
        "workspace_dir": str(workspace_dir),
        "keep_workspace": True,
        "opencode_bin": "opencode",
        "command": ["opencode", "run"],
        "started_at": "2026-03-10T10:53:26+00:00",
        "finished_at": "2026-03-10T10:55:24+00:00",
        "exit_code": 0,
        "execution_error": None,
        "capture_status": "success",
        "event_count": len(events),
        "session_id": "session-123",
        "export_saved": export_text is not None,
        "export_exit_code": 0,
        "summary_path": None,
        "report_path": None,
        "report_error": None,
    }
    (capture_dir / "meta" / "run.json").write_text(json.dumps(metadata), encoding="utf-8")
    (capture_dir / EVENT_LOG_RELATIVE_PATH).write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    if export_text is not None:
        (capture_dir / "session" / "export.json").write_text(export_text, encoding="utf-8")


class CaptureRunnerTests(unittest.TestCase):
    def test_load_issue_by_id(self) -> None:
        issue = load_issue("fix_math_utils")
        self.assertEqual(issue["title"], "Fix the broken add helper")

    def test_plugin_subscriptions_include_message_and_tracking_env(self) -> None:
        plugin_source = Path(".opencode/plugins/mas_safe_security.ts").read_text(encoding="utf-8")
        self.assertIn("event: async", plugin_source)
        self.assertIn('"command.execute.before"', plugin_source)
        self.assertIn('"permission.ask"', plugin_source)
        self.assertIn('"shell.env"', plugin_source)
        self.assertIn('"MAS_CAPTURE_DIR"', plugin_source)
        self.assertIn('"MAS_ISSUE_ID"', plugin_source)

    def test_extract_session_id_prefers_session_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            event_path.write_text(
                json.dumps(
                    {
                        "native_event_type": "session.created",
                        "raw_input": {"sessionId": "session-123"},
                        "raw_output": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(extract_session_id(event_path), "session-123")

    def test_run_capture_marks_failed_without_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = root / "repo"
            _copy_repo_subset(Path.cwd(), repo_root)
            fake_opencode = root / "fake-opencode"
            _write_executable(
                fake_opencode,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import sys

                    args = sys.argv[1:]
                    if args and args[0] == "run":
                        print("no events")
                        raise SystemExit(0)
                    if args and args[0] == "export":
                        raise SystemExit(0)
                    raise SystemExit(1)
                    """
                ),
            )

            metadata = run_capture(
                "fix_math_utils",
                opencode_bin=str(fake_opencode),
                capture_root=root / "captures",
                repo_root=repo_root,
                issues_path=repo_root / "tasks" / "issues.json",
            )
            self.assertEqual(metadata["capture_status"], "failed")
            self.assertIsNone(metadata["report_error"])
            self.assertTrue(Path(metadata["summary_path"]).exists())
            self.assertTrue(Path(metadata["report_path"]).exists())
            self.assertFalse(Path(metadata["workspace_dir"]).exists())

    def test_run_capture_saves_events_export_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = root / "repo"
            _copy_repo_subset(Path.cwd(), repo_root)
            fake_opencode = root / "fake-opencode"
            _write_executable(
                fake_opencode,
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import pathlib
                    import sys

                    args = sys.argv[1:]
                    if args and args[0] == "run":
                        capture_dir = pathlib.Path(os.environ["MAS_CAPTURE_DIR"])
                        event_path = capture_dir / "events" / "opencode_events.jsonl"
                        event_path.parent.mkdir(parents=True, exist_ok=True)
                        events = [
                            {
                                "timestamp": "2026-03-10T10:53:26.001Z",
                                "native_event_type": "session.created",
                                "raw_input": {"sessionId": "session-456"},
                                "raw_output": {},
                            },
                            {
                                "timestamp": "2026-03-10T10:53:29.001Z",
                                "native_event_type": "session.status",
                                "raw_input": {
                                    "type": "session.status",
                                    "properties": {
                                        "sessionID": "session-456",
                                        "status": {"type": "busy"},
                                    },
                                },
                                "raw_output": {},
                            },
                            {
                                "timestamp": "2026-03-10T10:53:31.001Z",
                                "native_event_type": "message.part.delta",
                                "raw_input": {"type": "message.part.delta"},
                                "raw_output": {},
                            },
                            {
                                "timestamp": "2026-03-10T10:53:39.001Z",
                                "native_event_type": "tool.execute.before",
                                "raw_input": {
                                    "tool": "read",
                                    "sessionID": "session-456",
                                    "callID": "call-1",
                                },
                                "raw_output": {"args": {"filePath": "math_utils.py"}},
                                "correlation": {"tool": "read", "callID": "call-1"},
                            },
                            {
                                "timestamp": "2026-03-10T10:53:40.001Z",
                                "native_event_type": "tool.execute.after",
                                "raw_input": {
                                    "tool": "read",
                                    "sessionID": "session-456",
                                    "callID": "call-1",
                                },
                                "raw_output": {"title": "ok"},
                                "correlation": {"tool": "read", "callID": "call-1"},
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
                        event_path.write_text("\\n".join(json.dumps(item) for item in events) + "\\n", encoding="utf-8")
                        print("run ok")
                        raise SystemExit(0)
                    if args and args[0] == "export":
                        print(json.dumps({"session_id": args[1], "messages": []}))
                        raise SystemExit(0)
                    raise SystemExit(1)
                    """
                ),
            )

            metadata = run_capture(
                "fix_math_utils",
                opencode_bin=str(fake_opencode),
                capture_root=root / "captures",
                repo_root=repo_root,
                issues_path=repo_root / "tasks" / "issues.json",
                keep_workspace=True,
            )
            capture_dir = Path(metadata["capture_dir"])
            summary = json.loads((capture_dir / DERIVED_SUMMARY_RELATIVE_PATH).read_text(encoding="utf-8"))
            report_html = (capture_dir / REPORT_RELATIVE_PATH).read_text(encoding="utf-8")

            self.assertEqual(metadata["capture_status"], "success")
            self.assertEqual(summary["run"]["session_id"], "session-456")
            self.assertEqual(summary["counts"]["noise_total"], 1)
            self.assertEqual(summary["timeline"][0]["phase"], "session_start")
            self.assertTrue(any(tool["tool_name"] == "read" and tool["success"] for tool in summary["tools"]))
            self.assertEqual(summary["files"][0]["path"], "tasks/fixtures/fix_math_utils/math_utils.py")
            self.assertTrue((capture_dir / EVENT_LOG_RELATIVE_PATH).exists())
            self.assertTrue((capture_dir / "session" / "export.json").exists())
            self.assertTrue((capture_dir / "workspace").exists())
            self.assertIn("OpenCode 运行报告", report_html)
            self.assertIn('"noise_total": 1', report_html)

    def test_render_capture_report_tolerates_invalid_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture_dir = root / "capture"
            workspace_dir = capture_dir / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            events = [
                {
                    "timestamp": "2026-03-10T10:53:26.001Z",
                    "native_event_type": "session.created",
                    "raw_input": {"sessionId": "session-123"},
                    "raw_output": {},
                },
                {
                    "timestamp": "2026-03-10T10:53:27.001Z",
                    "native_event_type": "message.part.delta",
                    "raw_input": {"type": "message.part.delta"},
                    "raw_output": {},
                },
                {
                    "timestamp": "2026-03-10T10:53:35.001Z",
                    "native_event_type": "tool.execute.before",
                    "raw_input": {"tool": "edit", "callID": "call-9"},
                    "raw_output": {"args": {"filePath": "RESULT.md"}},
                    "correlation": {"tool": "edit", "callID": "call-9"},
                },
                {
                    "timestamp": "2026-03-10T10:54:01.001Z",
                    "native_event_type": "session.idle",
                    "raw_input": {"type": "session.idle"},
                    "raw_output": {},
                },
            ]
            _write_capture_fixture(
                capture_dir,
                workspace_dir=workspace_dir,
                events=events,
                export_text='{"not_valid": true\n',
            )

            artifacts = render_capture_report(capture_dir)
            summary = build_capture_summary(capture_dir)

            self.assertTrue(Path(artifacts["summary_path"]).exists())
            self.assertTrue(Path(artifacts["report_path"]).exists())
            self.assertEqual(summary["counts"]["noise_total"], 1)
            self.assertFalse(any(event["event_type"] == "message.part.delta" for event in summary["timeline"]))
            self.assertTrue(any(item["code"] == "export_parse_error" for item in summary["anomalies"]))
            self.assertTrue(any(item["code"] == "tool_missing_after" for item in summary["anomalies"]))
            self.assertTrue(any(phase["name"] == "analysis" for phase in summary["phases"]))


if __name__ == "__main__":
    unittest.main()
