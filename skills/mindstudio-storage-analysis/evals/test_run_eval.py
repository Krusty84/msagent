#!/usr/bin/env python3
"""Tests for the cases.yaml-driven deterministic eval runner."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_eval


class TestEvalRunner(unittest.TestCase):
    def test_expected_confidence_rejects_overclaim(self):
        case = {
            "id": "overclaim",
            "fixture_file": "fixtures/overclaim.json",
            "expected_rule": "R100",
            "expected_confidence": "medium",
            "expected_severity": "high",
        }
        result = {
            "findings": [
                {
                    "rule_id": "R100",
                    "confidence": "high",
                    "severity": "high",
                    "summary": "",
                }
            ]
        }
        with (
            patch.object(
                run_eval, "_resolve_fixture_path", return_value="overclaim.json"
            ),
            patch.object(run_eval.os.path, "exists", return_value=True),
            patch.object(run_eval, "_load_fixture", return_value={}),
            patch.object(run_eval.a, "analyze_all", return_value=result),
        ):
            passed, detail = run_eval.evaluate_case(case)
        self.assertFalse(passed)
        self.assertIn("期望=medium", detail)

    def test_expected_severity_rejects_informational_regression(self):
        case = {
            "id": "severity-regression",
            "fixture_file": "fixtures/input.json",
            "expected_rule": "R100",
            "expected_confidence": "high",
            "expected_severity": "high",
        }
        result = {
            "findings": [
                {
                    "rule_id": "R100",
                    "confidence": "high",
                    "severity": "info",
                    "summary": "",
                }
            ]
        }
        with (
            patch.object(run_eval, "_resolve_fixture_path", return_value="input.json"),
            patch.object(run_eval.os.path, "exists", return_value=True),
            patch.object(run_eval, "_load_fixture", return_value={}),
            patch.object(run_eval.a, "analyze_all", return_value=result),
        ):
            passed, detail = run_eval.evaluate_case(case)

        self.assertFalse(passed)
        self.assertIn("severity=info", detail)

    def test_unexpected_validation_errors_fail_instead_of_passing_vacuously(self):
        case = {
            "id": "unexpected-validation",
            "fixture_file": "fixtures/input.json",
            "expected_rule": "none",
        }
        result = {
            "findings": [],
            "validation_errors": ["process_io_map: malformed evidence"],
        }
        with (
            patch.object(run_eval, "_resolve_fixture_path", return_value="input.json"),
            patch.object(run_eval.os.path, "exists", return_value=True),
            patch.object(run_eval, "_load_fixture", return_value={}),
            patch.object(run_eval.a, "analyze_all", return_value=result),
        ):
            passed, detail = run_eval.evaluate_case(case)
            allowed, _ = run_eval.evaluate_case(
                {
                    **case,
                    "allowed_validation_errors": ["process_io_map"],
                }
            )

        self.assertFalse(passed)
        self.assertIn("unexpected validation_error", detail)
        self.assertTrue(allowed)

    def test_all_machine_cases_pass(self):
        self.assertEqual(run_eval.run(verbose=False), 0)

    def test_explicit_report_is_written(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "eval.json"
            self.assertEqual(run_eval.run(str(report), verbose=False), 0)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["failed"], 0)
            self.assertEqual(payload["passed"], payload["total"])
            self.assertEqual(list(Path(temp_dir).glob("*.lock")), [])
            self.assertEqual(list(Path(temp_dir).glob(".*.tmp")), [])

    def test_profile_environment_cases_use_paired_snapshot(self):
        cases = run_eval._load_cases()
        for case in cases.get("environment_cases", []):
            command = str(case.get("command", ""))
            if "run_live_eval.py" in command and "--profile" in command:
                self.assertIn("--snapshot", command, case.get("id"))
                if "--require-r500-high" in command:
                    self.assertIn(
                        "conduction_evidence",
                        str(case.get("prerequisite", "")),
                        case.get("id"),
                    )

    def test_op_summary_case_is_explicitly_non_certifying(self):
        cases = run_eval._load_cases()
        case = next(
            item
            for item in cases.get("environment_cases", [])
            if item.get("id") == "ascend-msprof-op-summary-diagnostics"
        )
        self.assertIn("op_summary_diagnostics.json", case["command"])
        self.assertIn("不参与 R500 认证", case["validation"])
        self.assertIn("不能作为 --profile 输入", case["safety"])


if __name__ == "__main__":
    unittest.main()
