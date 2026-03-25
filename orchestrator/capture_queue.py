from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from orchestrator.supervisor_capture import DEFAULT_IMAGE_REF, DEFAULT_ISSUES_PATH, _load_catalog, run_capture

COOLDOWN_BY_RISK = {
    "safe": 0,
    "mild": 0,
    "severe": 20,
}
ORDER = {
    "safe": 0,
    "mild": 1,
    "severe": 2,
}


def _status_label(metadata: dict[str, object]) -> str:
    if bool(metadata.get("capture_valid")):
        return str(metadata.get("run_status") or "completed")
    return str(metadata.get("run_status") or metadata.get("capture_status") or "failed")


def _print_queue_header(catalog: list[dict[str, object]]) -> None:
    safe = sum(1 for issue in catalog if str(issue.get("risk_level")) == "safe")
    mild = sum(1 for issue in catalog if str(issue.get("risk_level")) == "mild")
    severe = sum(1 for issue in catalog if str(issue.get("risk_level")) == "severe")
    print(
        f"queue start: total={len(catalog)} safe={safe} mild={mild} severe={severe}",
        flush=True,
    )


def _print_run_result(
    *,
    index: int,
    total: int,
    issue_id: str,
    risk_level: str,
    metadata: dict[str, object],
    started_at: float,
    succeeded: int,
    failed: int,
) -> None:
    duration = time.monotonic() - started_at
    status = _status_label(metadata)
    capture_dir = str(metadata.get("capture_dir") or "-")
    event_count = metadata.get("event_count")
    session_id = metadata.get("session_id") or "-"
    print(
        f"finished [{index}/{total}] {issue_id} risk={risk_level} status={status} "
        f"valid={metadata.get('capture_valid')} events={event_count} session={session_id} "
        f"duration={duration:.1f}s ok={succeeded} failed={failed}",
        flush=True,
    )
    print(f"capture_dir: {capture_dir}", flush=True)


def _ordered_issues(path: Path) -> list[dict[str, object]]:
    issues = []
    for item in _load_catalog(path):
        issue = dict(item)
        issue_id = str(issue.get("id", ""))
        risk_level = str(issue.get("risk_level") or ("mild" if issue_id.startswith("mild_") else "severe" if issue_id.startswith("severe_") else "safe"))
        issue["risk_level"] = risk_level
        issues.append(issue)
    return sorted(issues, key=lambda issue: (ORDER.get(str(issue["risk_level"]), 9), str(issue.get("id", ""))))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run OpenCode captures sequentially with queue semantics.")
    parser.add_argument("--issues-path", type=Path, default=DEFAULT_ISSUES_PATH, help="Issue catalog path")
    parser.add_argument("--issue", action="append", dest="issues", help="Optional issue ids to run")
    parser.add_argument("--keep-workspace", action="store_true", help="Keep workspaces for all runs")
    parser.add_argument("--image-ref", help="Override runner image reference")
    parser.add_argument("--docker-bin", default="docker", help="Docker client path")
    parser.add_argument("--opencode-bin", default="opencode", help="Binary name inside the container image")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    catalog = _ordered_issues(args.issues_path)
    selected = set(args.issues or [])
    if selected:
        catalog = [issue for issue in catalog if str(issue.get("id")) in selected]
    if not catalog:
        print("queue start: total=0", flush=True)
        return 0

    _print_queue_header(catalog)

    failures: list[str] = []
    succeeded = 0
    total = len(catalog)
    queue_started_at = time.monotonic()
    for index, issue in enumerate(catalog, start=1):
        issue_id = str(issue["id"])
        risk_level = str(issue.get("risk_level") or "unknown")
        run_started_at = time.monotonic()
        print(f"running [{index}/{total}] {issue_id} risk={risk_level}", flush=True)
        metadata = run_capture(
            issue_id,
            keep_workspace=args.keep_workspace,
            issues_path=args.issues_path,
            image_ref=args.image_ref or DEFAULT_IMAGE_REF,
            docker_bin=args.docker_bin,
            opencode_bin=args.opencode_bin,
        )
        if metadata.get("run_status") != "completed" and not metadata.get("capture_valid"):
            failures.append(issue_id)
        else:
            succeeded += 1
        _print_run_result(
            index=index,
            total=total,
            issue_id=issue_id,
            risk_level=risk_level,
            metadata=metadata,
            started_at=run_started_at,
            succeeded=succeeded,
            failed=len(failures),
        )
        cooldown = COOLDOWN_BY_RISK.get(str(issue.get("risk_level")), 0)
        if cooldown:
            print(f"cooldown: {cooldown}s before next issue", flush=True)
            time.sleep(cooldown)
    queue_duration = time.monotonic() - queue_started_at
    print(
        f"queue done: total={total} succeeded={succeeded} failed={len(failures)} duration={queue_duration:.1f}s",
        flush=True,
    )
    if failures:
        print(f"completed with invalid captures: {' '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
