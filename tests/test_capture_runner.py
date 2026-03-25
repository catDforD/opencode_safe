import json
from pathlib import Path
import stat
import tempfile
import textwrap
import unittest
import unittest.mock

from orchestrator.opencode_plugin_runner import (
    DERIVED_SUMMARY_RELATIVE_PATH,
    EVENT_LOG_RELATIVE_PATH,
    FS_DIFF_RELATIVE_PATH,
    NETWORK_RELATIVE_PATH,
    PROCESS_TREE_RELATIVE_PATH,
    REPORT_RELATIVE_PATH,
    build_capture_summary,
    extract_session_id,
    load_issue,
    render_capture_report,
    run_capture,
)
from orchestrator.supervisor_capture import _build_export_command, _build_run_command, _home_mounts


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _copy_repo_subset(source_root: Path, destination_root: Path) -> None:
    for relative_path in [
        Path(".opencode/plugins/mas_safe_security.ts"),
        Path("config/sensitive_paths.json"),
        Path("tasks/issues.json"),
        Path("tasks/fixtures/fix_math_utils/math_utils.py"),
        Path("tasks/fixtures/fix_math_utils/test_math_utils.py"),
        Path("tasks/fixtures/summarize_release_notes/release_notes.md"),
    ]:
        source = source_root / relative_path
        destination = destination_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _write_fake_docker(path: Path) -> None:
    _write_executable(
        path,
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys
            import time

            state_dir = Path(os.environ["FAKE_DOCKER_STATE_DIR"])
            state_dir.mkdir(parents=True, exist_ok=True)

            def parse_mounts(args):
                mounts = {}
                i = 0
                while i < len(args):
                    if args[i] == "--mount":
                        spec = args[i + 1]
                        data = {}
                        for part in spec.split(","):
                            if "=" in part:
                                key, value = part.split("=", 1)
                                data[key] = value
                        mounts[data.get("dst")] = data.get("src")
                        i += 2
                    else:
                        i += 1
                return mounts

            def parse_env(args):
                env = {}
                i = 0
                while i < len(args):
                    if args[i] == "-e":
                        key, value = args[i + 1].split("=", 1)
                        env[key] = value
                        i += 2
                    else:
                        i += 1
                return env

            def state_path(name):
                return state_dir / f"{name}.json"

            def read_state(name):
                return json.loads(state_path(name).read_text(encoding="utf-8"))

            def write_state(name, payload):
                state_path(name).write_text(json.dumps(payload), encoding="utf-8")

            def ensure_success_artifacts(state):
                capture_dir = Path(state["mounts"]["/capture"])
                workspace = Path(state["mounts"]["/workspace"])
                (capture_dir / "events").mkdir(parents=True, exist_ok=True)
                (workspace / "RESULT.md").write_text("supervisor output\\n", encoding="utf-8")
                behavior = state["behavior"]
                if behavior != "missing_events":
                    event_path = capture_dir / "events" / "opencode_events.jsonl"
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
                                    "file": str(workspace / "RESULT.md"),
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

            args = sys.argv[1:]
            if not args:
                raise SystemExit(1)
            command = args[0]

            if command == "create":
                name = args[args.index("--name") + 1]
                mounts = parse_mounts(args)
                env = parse_env(args)
                behavior = os.environ.get("FAKE_DOCKER_BEHAVIOR", "success")
                payload = {
                    "behavior": behavior,
                    "mounts": mounts,
                    "env": env,
                    "stopped": False,
                    "exit_code": None,
                    "oom_killed": False,
                }
                write_state(name, payload)
                print(name)
                raise SystemExit(0)

            if command == "start":
                name = args[-1]
                state = read_state(name)
                behavior = state["behavior"]
                if behavior == "timeout":
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        state = read_state(name)
                        if state.get("stopped"):
                            state["exit_code"] = 124
                            write_state(name, state)
                            print("timed out", file=sys.stderr)
                            raise SystemExit(124)
                        time.sleep(0.1)
                    state["exit_code"] = 124
                    write_state(name, state)
                    raise SystemExit(124)

                ensure_success_artifacts(state)
                if behavior == "oom":
                    state["exit_code"] = 137
                    state["oom_killed"] = True
                    write_state(name, state)
                    print("oom", file=sys.stderr)
                    raise SystemExit(137)
                if behavior == "failed":
                    state["exit_code"] = 1
                    write_state(name, state)
                    print("failed", file=sys.stderr)
                    raise SystemExit(1)
                state["exit_code"] = 0
                write_state(name, state)
                print("run ok")
                raise SystemExit(0)

            if command == "top":
                print("PID PPID ELAPSED COMMAND ARGS")
                print("101 1 00:02 opencode opencode run --print-logs")
                raise SystemExit(0)

            if command == "exec":
                print("tcp ESTAB 0 0 172.17.0.2:40000 198.51.100.10:443")
                raise SystemExit(0)

            if command == "logs":
                print("container log line")
                raise SystemExit(0)

            if command == "inspect":
                name = args[-1]
                state = read_state(name)
                print(json.dumps([{"State": {"ExitCode": state.get("exit_code"), "OOMKilled": state.get("oom_killed", False)}}]))
                raise SystemExit(0)

            if command == "stop":
                name = args[-1]
                state = read_state(name)
                state["stopped"] = True
                write_state(name, state)
                raise SystemExit(0)

            if command == "rm":
                name = args[-1]
                state_path(name).unlink(missing_ok=True)
                raise SystemExit(0)

            if command == "run":
                session_id = args[-2]
                print(json.dumps({"session_id": session_id, "messages": []}))
                raise SystemExit(0)

            raise SystemExit(1)
            """
        ),
    )


def _write_capture_fixture(
    capture_dir: Path,
    *,
    workspace_dir: Path,
    with_events: bool,
    run_status: str = "completed",
    capture_valid: bool = True,
    native_events_status: str = "present",
) -> None:
    (capture_dir / "events").mkdir(parents=True, exist_ok=True)
    (capture_dir / "meta").mkdir(parents=True, exist_ok=True)
    (capture_dir / "session").mkdir(parents=True, exist_ok=True)
    (capture_dir / "observations").mkdir(parents=True, exist_ok=True)
    metadata = {
        "run_id": "capture-test",
        "issue_id": "fix_math_utils",
        "issue_title": "Fix the broken add helper",
        "risk_level": "safe",
        "image_ref": "example/opencode:test",
        "capture_dir": str(capture_dir),
        "workspace_dir": str(workspace_dir),
        "keep_workspace": True,
        "docker_bin": "docker",
        "opencode_bin": "opencode",
        "command": ["opencode", "run"],
        "resource_limits": {"cpus": "2", "memory": "4g", "pids": 256},
        "started_at": "2026-03-10T10:53:26+00:00",
        "finished_at": "2026-03-10T10:55:24+00:00",
        "duration_seconds": 118.0,
        "run_status": run_status,
        "capture_status": run_status,
        "capture_valid": capture_valid,
        "container_exit_code": 0,
        "exit_code": 0,
        "execution_error": None,
        "timed_out": False,
        "oom_killed": False,
        "timeout_seconds": 300,
        "native_events_status": native_events_status,
        "event_count": 6 if with_events else 0,
        "session_id": "session-123" if with_events else None,
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
            "\n".join(
                json.dumps(event)
                for event in [
                    {
                        "timestamp": "2026-03-10T10:53:26.001Z",
                        "native_event_type": "session.created",
                        "raw_input": {"sessionId": "session-123"},
                        "raw_output": {},
                    },
                    {
                        "timestamp": "2026-03-10T10:53:29.001Z",
                        "native_event_type": "tool.execute.before",
                        "raw_input": {"tool": "read", "callID": "call-1"},
                        "raw_output": {},
                        "correlation": {"tool": "read", "callID": "call-1"},
                    },
                    {
                        "timestamp": "2026-03-10T10:53:30.001Z",
                        "native_event_type": "tool.execute.after",
                        "raw_input": {"tool": "read", "callID": "call-1"},
                        "raw_output": {},
                        "correlation": {"tool": "read", "callID": "call-1"},
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )


class CaptureRunnerTests(unittest.TestCase):
    def test_container_commands_do_not_repeat_entrypoint_binary(self) -> None:
        issue = load_issue("fix_math_utils")
        prompt = "fix it"

        run_command = _build_run_command(issue=issue, prompt=prompt, issue_files=[])
        export_command = _build_export_command("session-123")

        self.assertEqual(run_command[:4], ["run", "--print-logs", "--title", issue["title"]])
        self.assertEqual(run_command[-1], prompt)
        self.assertEqual(export_command, ["export", "session-123", "--print-logs"])

    def test_home_mounts_cover_root_and_legacy_home(self) -> None:
        mounts = _home_mounts(Path("/tmp/capture-home"))

        self.assertIn("dst=/root", mounts[1])
        self.assertIn("dst=/home/opencode", mounts[0])

    def test_load_issue_by_id(self) -> None:
        issue = load_issue("fix_math_utils")
        self.assertEqual(issue["title"], "Fix the broken add helper")
        self.assertEqual(issue["risk_level"], "safe")

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

    def test_run_capture_keeps_valid_sample_without_native_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = root / "repo"
            _copy_repo_subset(Path.cwd(), repo_root)
            fake_docker = root / "fake-docker"
            _write_fake_docker(fake_docker)

            state_dir = root / "fake-docker-state"
            os_environ = dict(FAKE_DOCKER_STATE_DIR=str(state_dir), FAKE_DOCKER_BEHAVIOR="missing_events")
            with unittest.mock.patch.dict("os.environ", os_environ, clear=False):
                metadata = run_capture(
                    "fix_math_utils",
                    docker_bin=str(fake_docker),
                    image_ref="example/opencode:test",
                    capture_root=root / "captures",
                    repo_root=repo_root,
                    issues_path=repo_root / "tasks" / "issues.json",
                )

            capture_dir = Path(metadata["capture_dir"])
            self.assertTrue(metadata["capture_valid"])
            self.assertEqual(metadata["run_status"], "completed")
            self.assertEqual(metadata["native_events_status"], "missing")
            summary = json.loads((capture_dir / DERIVED_SUMMARY_RELATIVE_PATH).read_text(encoding="utf-8"))
            self.assertTrue(summary["run"]["capture_valid"])
            self.assertEqual(summary["run"]["capture_status"], "completed")
            self.assertTrue((capture_dir / PROCESS_TREE_RELATIVE_PATH).exists())
            self.assertTrue((capture_dir / NETWORK_RELATIVE_PATH).exists())
            self.assertTrue((capture_dir / FS_DIFF_RELATIVE_PATH).exists())
            self.assertTrue(Path(metadata["summary_path"]).exists())
            self.assertTrue(Path(metadata["report_path"]).exists())
            self.assertFalse((capture_dir / EVENT_LOG_RELATIVE_PATH).exists())

    def test_run_capture_collects_native_events_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = root / "repo"
            _copy_repo_subset(Path.cwd(), repo_root)
            fake_docker = root / "fake-docker"
            _write_fake_docker(fake_docker)

            state_dir = root / "fake-docker-state"
            os_environ = dict(FAKE_DOCKER_STATE_DIR=str(state_dir), FAKE_DOCKER_BEHAVIOR="success")
            with unittest.mock.patch.dict("os.environ", os_environ, clear=False):
                metadata = run_capture(
                    "fix_math_utils",
                    docker_bin=str(fake_docker),
                    image_ref="example/opencode:test",
                    capture_root=root / "captures",
                    repo_root=repo_root,
                    issues_path=repo_root / "tasks" / "issues.json",
                    keep_workspace=True,
                )

            capture_dir = Path(metadata["capture_dir"])
            summary = json.loads((capture_dir / DERIVED_SUMMARY_RELATIVE_PATH).read_text(encoding="utf-8"))
            report_html = (capture_dir / REPORT_RELATIVE_PATH).read_text(encoding="utf-8")

            self.assertTrue(metadata["capture_valid"])
            self.assertEqual(metadata["run_status"], "completed")
            self.assertEqual(metadata["native_events_status"], "present")
            self.assertTrue(summary["run"]["capture_valid"])
            self.assertEqual(summary["run"]["capture_status"], "completed")
            self.assertEqual(summary["run"]["session_id"], "session-456")
            self.assertEqual(summary["run"]["native_events_status"], "present")
            self.assertTrue(any(tool["tool_name"] == "read" and tool["success"] for tool in summary["tools"]))
            self.assertEqual(summary["observations"]["filesystem"]["counts"]["total"], 1)
            self.assertIn("OpenCode", report_html)
            self.assertTrue((capture_dir / "session" / "export.json").exists())
            self.assertTrue((capture_dir / "workspace").exists())

    def test_run_capture_marks_timeout_and_oom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = root / "repo"
            _copy_repo_subset(Path.cwd(), repo_root)
            fake_docker = root / "fake-docker"
            _write_fake_docker(fake_docker)

            timeout_env = dict(FAKE_DOCKER_STATE_DIR=str(root / "state-timeout"), FAKE_DOCKER_BEHAVIOR="timeout")
            with unittest.mock.patch.dict("os.environ", timeout_env, clear=False):
                timeout_meta = run_capture(
                    "fix_math_utils",
                    docker_bin=str(fake_docker),
                    image_ref="example/opencode:test",
                    capture_root=root / "captures-timeout",
                    repo_root=repo_root,
                    issues_path=repo_root / "tasks" / "issues.json",
                    timeout_seconds=1,
                )
            self.assertEqual(timeout_meta["run_status"], "timeout")
            self.assertTrue(timeout_meta["timed_out"])
            self.assertTrue(timeout_meta["capture_valid"])

            oom_env = dict(FAKE_DOCKER_STATE_DIR=str(root / "state-oom"), FAKE_DOCKER_BEHAVIOR="oom")
            with unittest.mock.patch.dict("os.environ", oom_env, clear=False):
                oom_meta = run_capture(
                    "fix_math_utils",
                    docker_bin=str(fake_docker),
                    image_ref="example/opencode:test",
                    capture_root=root / "captures-oom",
                    repo_root=repo_root,
                    issues_path=repo_root / "tasks" / "issues.json",
                )
            self.assertEqual(oom_meta["run_status"], "oom_killed")
            self.assertTrue(oom_meta["oom_killed"])
            self.assertTrue(oom_meta["capture_valid"])

    def test_render_capture_report_handles_runs_without_native_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture_dir = Path(tmp) / "capture"
            workspace_dir = capture_dir / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            _write_capture_fixture(capture_dir, workspace_dir=workspace_dir, with_events=False, native_events_status="missing")

            artifacts = render_capture_report(capture_dir)
            summary = build_capture_summary(capture_dir)

            self.assertTrue(Path(artifacts["summary_path"]).exists())
            self.assertEqual(summary["run"]["native_events_status"], "missing")
            self.assertEqual(summary["counts"]["significant_total"], 0)
            self.assertEqual(summary["observations"]["process"]["snapshot_count"], 1)


if __name__ == "__main__":
    unittest.main()
