#!/usr/bin/env python3
"""Unit tests for the bounded AscendCL runtime evaluator."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_npu_runtime_eval as npu_eval


class TestNpuRuntimeEvalHelpers(unittest.TestCase):
    def test_find_toolkit_root_from_custom_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            required = [
                root / "include" / "acl" / "acl.h",
                root / "include" / "aclnnop" / "aclnn_add.h",
                root / "lib64" / "libascendcl.so",
                root / "lib64" / "libopapi.so",
            ]
            for path in required:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")
            with patch.dict(
                npu_eval.os.environ,
                {"ASCEND_HOME_PATH": str(root), "ASCEND_TOOLKIT_HOME": ""},
            ):
                self.assertEqual(npu_eval._find_toolkit_root(), root.resolve())

    def test_parse_result_requires_pass_object(self):
        self.assertEqual(
            npu_eval._parse_result('noise\n{"status":"PASS","device":0}\n')["device"],
            0,
        )
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            npu_eval._parse_result("not-json")
        with self.assertRaisesRegex(RuntimeError, "did not return PASS"):
            npu_eval._parse_result('{"status":"FAIL"}')

    def test_atomic_report_write_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "report.json"
            npu_eval._atomic_write_json(report, {"ok": True})
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8")), {"ok": True}
            )
            self.assertEqual(list(Path(temp_dir).glob(".*.tmp")), [])

    def test_compile_timeout_is_reported_as_runtime_error(self):
        with (
            patch.object(npu_eval.shutil, "which", return_value="/usr/bin/c++"),
            patch.object(
                npu_eval,
                "_run",
                side_effect=subprocess.TimeoutExpired(["c++"], timeout=120),
            ),
            self.assertRaisesRegex(RuntimeError, "compilation timed out"),
        ):
            npu_eval._compile(Path("/cann"), Path("/tmp/npu-smoke"))


if __name__ == "__main__":
    unittest.main()
