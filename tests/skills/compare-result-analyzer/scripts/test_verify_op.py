#!/usr/bin/env python3
"""
verify_op.py 单元测试
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "skills", "compare-result-analyzer", "scripts"))

import verify_op
from verify_op import main


def _write_csv(path, rows):
    """Write a minimal msProbe compare CSV file."""
    header = (
        "NPU Name,NPU Tensor Shape,NPU Dtype,NPU Requires_grad,"
        "NPU max,NPU min,NPU mean,NPU l2norm"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(",".join(r) + "\n")


class TestMainNoArgs(unittest.TestCase):
    def test_no_csv_arg_exits_1(self):
        old_argv = sys.argv
        sys.argv = ["verify_op.py"]
        try:
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.argv = old_argv


class TestMainEmptyCsv(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmpdir, "empty.csv")
        _write_csv(self.csv_path, [])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_csv_exits_0(self):
        old_argv = sys.argv
        sys.argv = ["verify_op.py", self.csv_path]
        try:
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)
        finally:
            sys.argv = old_argv

    def test_empty_csv_with_output(self):
        out_path = os.path.join(self.tmpdir, "out.json")
        old_argv = sys.argv
        sys.argv = ["verify_op.py", self.csv_path, "-o", out_path]
        try:
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)
        finally:
            sys.argv = old_argv
        self.assertTrue(os.path.exists(out_path))
        with open(out_path) as f:
            data = json.load(f)
        self.assertEqual(data["summary"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
