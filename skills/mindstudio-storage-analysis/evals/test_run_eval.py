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


if __name__ == "__main__":
    unittest.main()
