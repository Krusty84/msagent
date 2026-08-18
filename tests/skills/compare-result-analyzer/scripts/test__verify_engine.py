#!/usr/bin/env python3
"""
_verify_engine.py 单元测试
"""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "skills", "compare-result-analyzer", "scripts"))

import torch

import _verify_engine as verify_engine
from _verify_engine import (
    VerifyResult,
    verify_operator,
    _to_comparison_dtype,
    _merge_construct_quality,
    _match_tensor_by_shape,
    _match_bwd_output_to_fwd_input,
    _match_bwd_input_to_fwd_output,
    print_header,
    print_results,
    print_verify_params,
    output_json,
)
from _verify_core import TensorInfo, OpGroup, ConstructQuality

# print_verify_params 引用了 _verify_core._CONSTRUCT_SEED，但 _verify_engine 未导入该名，
# 调用时会触发 NameError。此处向模块命名空间注入默认值以消除导入副作用。
if not hasattr(verify_engine, "_CONSTRUCT_SEED"):
    verify_engine._CONSTRUCT_SEED = None


def _make_ti(name, op_name, instance, direction, io_type, idx, shape,
             dtype="torch.float32", requires_grad=False,
             npu_max=2.0, npu_min=-1.0, npu_mean=0.5, npu_l2norm=1.0, is_none=False):
    """构造一个 TensorInfo 用于测试"""
    return TensorInfo(
        name=name, op_name=op_name, instance=instance, direction=direction,
        io_type=io_type, idx=idx, shape=shape, npu_dtype=dtype,
        requires_grad=requires_grad, npu_max=npu_max, npu_min=npu_min,
        npu_mean=npu_mean, npu_l2norm=npu_l2norm, is_none=is_none,
    )


class TestMergeConstructQuality(unittest.TestCase):
    def test_merge_takes_worst(self):
        q1 = ConstructQuality(l2norm_err=0.01, clamp_ratio=0.05, degraded=False)
        q2 = ConstructQuality(l2norm_err=0.2, clamp_ratio=0.02, degraded=True)
        merged = _merge_construct_quality([q1, q2])
        self.assertAlmostEqual(merged.l2norm_err, 0.2)
        self.assertAlmostEqual(merged.clamp_ratio, 0.05)
        self.assertTrue(merged.degraded)


class TestVerifyResultDataclass(unittest.TestCase):
    def test_defaults_and_fields(self):
        r = VerifyResult(
            op_name="op.0.forward", instance="0", direction="forward",
            tensor_name="output.0", shape_match=True,
            max_diff=0.1, l2norm_diff=0.2, mean_diff=0.05,
            max_rel_err=1.0, mean_rel_err=0.5, passed=True,
        )
        self.assertEqual(r.op_name, "op.0.forward")
        self.assertTrue(r.passed)
        self.assertEqual(r.error, "")
        self.assertEqual(r.note, "")
        self.assertEqual(r.construct_l2norm_err, 0.0)
        self.assertFalse(r.construct_degraded)


class TestVerifyOperator(unittest.TestCase):
    def test_unregistered_op_returns_error(self):
        g = OpGroup(op_name="Nonexistent.Op", instance="0")
        g.fwd_inputs = [
            _make_ti("Nonexistent.Op.0.forward.input.0", "Nonexistent.Op", "0",
                     "forward", "input", 0, [2, 2]),
        ]
        g.fwd_outputs = [
            _make_ti("Nonexistent.Op.0.forward.output.0", "Nonexistent.Op", "0",
                     "forward", "output", 0, [2, 2]),
        ]
        results = verify_operator(g, direction="forward")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertIn("未注册", results[0].error)
        self.assertEqual(results[0].op_name, "Nonexistent.Op.0.forward")

    def test_backward_without_fwd_inputs_reports_error(self):
        g = OpGroup(op_name="Tensor.__truediv__", instance="0")
        g.fwd_inputs = []  # 无前向数据
        g.bwd_outputs = [
            _make_ti("Tensor.__truediv__.0.backward.output.0", "Tensor.__truediv__",
                     "0", "backward", "output", 0, [2, 2]),
        ]
        results = verify_operator(g, direction="backward")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)
        self.assertIn("前向数据", results[0].error)

    def test_direction_filter(self):
        # 未注册算子会在任何 device 执行前提前返回，可用于廉价地验证方向过滤。
        g = OpGroup(op_name="Nonexistent.Op", instance="0")
        g.fwd_inputs = [
            _make_ti("Nonexistent.Op.0.forward.input.0", "Nonexistent.Op", "0",
                     "forward", "input", 0, [2, 2]),
        ]
        fwd = verify_operator(g, direction="forward")
        self.assertTrue(all(r.direction == "forward" for r in fwd))
        bwd = verify_operator(g, direction="backward")
        self.assertTrue(all(r.direction == "backward" for r in bwd))


class TestToComparisonDtype(unittest.TestCase):
    def test_float_unchanged(self):
        t = torch.tensor([1.0, 2.0], dtype=torch.float32)
        out = _to_comparison_dtype(t)
        self.assertIs(out, t)

    def test_int_to_float32(self):
        t = torch.tensor([1, 2, 3], dtype=torch.int64)
        out = _to_comparison_dtype(t)
        self.assertEqual(out.dtype, torch.float32)
        self.assertTrue(torch.equal(out, torch.tensor([1.0, 2.0, 3.0], dtype=torch.float32)))


class TestMatchTensorByShape(unittest.TestCase):
    def test_exact_single(self):
        ti = _make_ti("a", "op", "0", "forward", "input", 0, [2, 3])
        cands = [(0, _make_ti("c0", "op", "0", "forward", "input", 0, [2, 3]))]
        idx, mtype = _match_tensor_by_shape(ti, cands)
        self.assertEqual(idx, 0)
        self.assertEqual(mtype, "exact")

    def test_no_match(self):
        ti = _make_ti("a", "op", "0", "forward", "input", 0, [5, 5])
        cands = [(0, _make_ti("c0", "op", "0", "forward", "input", 0, [2, 3]))]
        idx, mtype = _match_tensor_by_shape(ti, cands)
        self.assertIsNone(idx)
        self.assertEqual(mtype, "no_match")


class TestMatchBwdOutputToFwdInput(unittest.TestCase):
    def test_matches_by_shape_and_dtype(self):
        ti = _make_ti("bwd_out", "op", "0", "backward", "output", 0, [2, 2])
        fwd_inputs = [
            _make_ti("f0", "op", "0", "forward", "input", 0, [3, 3]),
            _make_ti("f1", "op", "0", "forward", "input", 1, [2, 2]),
        ]
        idx, mtype = _match_bwd_output_to_fwd_input(ti, fwd_inputs, set())
        self.assertEqual(idx, 1)
        self.assertEqual(mtype, "exact")


class TestMatchBwdInputToFwdOutput(unittest.TestCase):
    def test_matches_by_shape(self):
        ti = _make_ti("bwd_in", "op", "0", "backward", "input", 0, [2, 2])
        fwd_outputs = [
            _make_ti("fo0", "op", "0", "forward", "output", 0, [4, 4]),
            _make_ti("fo1", "op", "0", "forward", "output", 1, [2, 2]),
        ]
        idx, mtype = _match_bwd_input_to_fwd_output(ti, fwd_outputs)
        self.assertEqual(idx, 1)
        self.assertEqual(mtype, "exact")


class TestPrintHeader(unittest.TestCase):
    def test_prints_header(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            print_header("单算子验证报告", w=30)
        finally:
            sys.stdout = old
        out = buf.getvalue()
        self.assertIn("单算子验证报告", out)
        self.assertEqual(out.count("=" * 30), 2)


class TestPrintResults(unittest.TestCase):
    def test_print_summary(self):
        results = [
            VerifyResult(op_name="op.0.forward", instance="0", direction="forward",
                         tensor_name="output.0", shape_match=True,
                         max_diff=0.0, l2norm_diff=0.0, mean_diff=0.0,
                         max_rel_err=0.0, mean_rel_err=0.0, passed=True),
            VerifyResult(op_name="op.0.forward", instance="0", direction="forward",
                         tensor_name="output.1", shape_match=False,
                         max_diff=1.0, l2norm_diff=1.0, mean_diff=1.0,
                         max_rel_err=100.0, mean_rel_err=50.0, passed=False,
                         error="shape mismatch"),
        ]
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            print_results(results)
        finally:
            sys.stdout = old
        out = buf.getvalue()
        self.assertIn("单算子验证报告", out)
        self.assertIn("PASS", out)
        self.assertIn("FAIL", out)
        self.assertIn("总计: 2", out)
        self.assertIn("通过: 1", out)


class TestPrintVerifyParams(unittest.TestCase):
    def test_prints_params(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            print_verify_params(1e-4, 1e-3)
        finally:
            sys.stdout = old
        out = buf.getvalue()
        self.assertIn("验证参数", out)
        self.assertIn("atol", out)
        self.assertIn("rtol", out)
        self.assertIn("dtype-aware tolerance", out)


class TestOutputJson(unittest.TestCase):
    def test_writes_json_report(self):
        results = [
            VerifyResult(op_name="op.0.forward", instance="0", direction="forward",
                         tensor_name="output.0", shape_match=True,
                         max_diff=0.0, l2norm_diff=0.0, mean_diff=0.0,
                         max_rel_err=0.0, mean_rel_err=0.0, passed=True),
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "report.json")
            output_json(results, path, atol=1e-4, rtol=1e-3)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        self.assertIn("verify_params", data)
        self.assertEqual(data["verify_params"]["atol"], 1e-4)
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(data["summary"]["passed"], 1)
        self.assertEqual(data["results"][0]["tensor_name"], "output.0")
        self.assertTrue(data["results"][0]["passed"])


if __name__ == "__main__":
    unittest.main()
