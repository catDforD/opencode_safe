import json
from pathlib import Path
import tempfile
import unittest

from orchestrator.capture_queue import _ordered_issues, _status_label


class CaptureQueueTests(unittest.TestCase):
    def test_ordered_issues_groups_by_risk_then_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issues_path = Path(tmp) / "issues.json"
            issues_path.write_text(
                json.dumps(
                    [
                        {"id": "severe_b", "risk_level": "severe"},
                        {"id": "safe_b", "risk_level": "safe"},
                        {"id": "mild_a", "risk_level": "mild"},
                        {"id": "safe_a", "risk_level": "safe"},
                    ]
                ),
                encoding="utf-8",
            )

            ordered = _ordered_issues(issues_path)

            self.assertEqual(
                [str(item["id"]) for item in ordered],
                ["safe_a", "safe_b", "mild_a", "severe_b"],
            )

    def test_status_label_prefers_valid_captures(self) -> None:
        self.assertEqual(
            _status_label({"run_status": "failed", "capture_valid": True}),
            "failed",
        )
        self.assertEqual(
            _status_label({"run_status": "infra_error", "capture_status": "infra_error", "capture_valid": False}),
            "infra_error",
        )


if __name__ == "__main__":
    unittest.main()
