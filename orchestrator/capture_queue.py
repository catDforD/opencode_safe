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
    failures: list[str] = []
    total = len(catalog)
    for index, issue in enumerate(catalog, start=1):
        issue_id = str(issue["id"])
        print(f"running [{index}/{total}] {issue_id}", flush=True)
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
        cooldown = COOLDOWN_BY_RISK.get(str(issue.get("risk_level")), 0)
        if cooldown:
            time.sleep(cooldown)
    if failures:
        print(f"completed with invalid captures: {' '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
