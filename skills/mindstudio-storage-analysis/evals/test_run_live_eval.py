#!/usr/bin/env python3
"""Unit tests for the read-only live environment runner."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_live_eval as live


class TestLiveEvalHelpers(unittest.TestCase):
    def test_find_toolkit_root_from_custom_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            toolkit = Path(temp_dir) / "cann"
            (toolkit / "lib64").mkdir(parents=True)
            (toolkit / "set_env.sh").write_text("", encoding="utf-8")
            (toolkit / "lib64" / "libascendcl.so").write_bytes(b"")
            with (
                patch.dict(
                    live.os.environ,
                    {
                        "ASCEND_HOME_PATH": str(toolkit),
                        "ASCEND_TOOLKIT_HOME": "",
                    },
                ),
                patch.object(live.shutil, "which", return_value=None),
            ):
                self.assertEqual(live._find_toolkit_root(), toolkit.resolve())

    def test_acl_runtime_probe_requires_real_success(self):
        completed = subprocess.CompletedProcess(
            ["python3", "-c", "probe"],
            0,
            stdout='{"device_count": 2}\n',
            stderr="",
        )
        with patch.object(live, "_run", return_value=completed):
            passed, detail = live._acl_runtime_probe()
        self.assertTrue(passed)
        self.assertIn("logical_devices=2", detail)

        failed = subprocess.CompletedProcess(
            ["python3", "-c", "probe"],
            1,
            stdout="",
            stderr="acl.init returned 507899",
        )
        with patch.object(live, "_run", return_value=failed):
            passed, detail = live._acl_runtime_probe()
        self.assertFalse(passed)
        self.assertIn("507899", detail)

    def test_required_runtime_fails_when_acl_probe_fails(self):
        def find_spec(name):
            return object() if name == "acl" else None

        with (
            patch.object(live, "_find_toolkit_root", return_value=Path("/cann")),
            patch.object(live.importlib.util, "find_spec", side_effect=find_spec),
            patch.object(live, "_acl_runtime_probe", return_value=(False, "broken")),
            patch.object(live.shutil, "which", return_value="/cann/bin/msprof"),
        ):
            required = live._npu_runtime_check(required=True)
            optional = live._npu_runtime_check(required=False)
        self.assertEqual(required.status, "FAIL")
        self.assertEqual(optional.status, "SKIP")

    def test_profile_requires_object_and_recognized_metric(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "profile.json"
            profile.write_text("[]", encoding="utf-8")
            payload, error = live._load_profile(profile)
            self.assertIsNone(payload)
            self.assertIn("JSON object", error)

            profile.write_text('{"other": 1}', encoding="utf-8")
            payload, error = live._load_profile(profile)
            self.assertIsNone(payload)
            self.assertIn("device_free_percent", error)

            profile.write_text('{"device_free_percent": 20}', encoding="utf-8")
            payload, error = live._load_profile(profile)
            self.assertIsNone(payload)
            self.assertIn("profile_window", error)

            profile.write_text(
                '{"device_free_percent": 20, "profile_window": '
                '{"start": "2026-07-20T00:00:00+00:00", '
                '"end": "2026-07-20T00:00:01+00:00"}}',
                encoding="utf-8",
            )
            payload, error = live._load_profile(profile)
            self.assertIsNotNone(payload)
            self.assertIsNone(error)

    def test_profile_rejects_malformed_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "profile.json"
            profile.write_text("{", encoding="utf-8")
            payload, error = live._load_profile(profile)
            self.assertIsNone(payload)
            self.assertIsNotNone(error)

    def test_npu_health_parser_accepts_case_insensitive_ok(self):
        output = """+---+
| 0 310P3 | ok | other |
| 1 310P3 | OK | other |
"""
        completed = subprocess.CompletedProcess(
            ["npu-smi", "info"], 0, stdout=output, stderr=""
        )
        with (
            patch.object(live.shutil, "which", return_value="/usr/bin/npu-smi"),
            patch.object(live.Path, "glob", return_value=[Path("/dev/davinci0")]),
            patch.object(live, "_run", return_value=completed),
        ):
            check, count = live._npu_hardware_check()
        self.assertEqual(check.status, "PASS")
        self.assertEqual(count, 2)

    def test_npu_health_without_device_node_is_not_a_pass(self):
        completed = subprocess.CompletedProcess(
            ["npu-smi", "info"],
            0,
            stdout="| 0 310P3 | OK | other |\n",
            stderr="",
        )
        with (
            patch.object(live.shutil, "which", return_value="/usr/bin/npu-smi"),
            patch.object(live.Path, "glob", return_value=[]),
            patch.object(live, "_run", return_value=completed),
        ):
            check, count = live._npu_hardware_check()
        self.assertEqual(check.status, "SKIP")
        self.assertEqual(count, 1)

    def test_r500_certification_rejects_informational_high(self):
        handoff = {
            "rule_id": "R500",
            "confidence": "high",
            "severity": "info",
            "handoff": "ascend-computation-analysis",
            "evidence_fields": ["profile.mte2_ratio"],
        }
        self.assertFalse(live._r500_is_certified(handoff))

        confirmed = {
            "rule_id": "R500",
            "confidence": "high",
            "severity": "high",
            "evidence_fields": [
                "profile.device_free_percent",
                "profile.conduction_evidence",
            ],
        }
        self.assertTrue(live._r500_is_certified(confirmed))

    def test_atomic_report_write_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "report.json"
            live._atomic_write_json(report, {"ok": True})
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8")), {"ok": True}
            )
            self.assertEqual(list(Path(temp_dir).glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
