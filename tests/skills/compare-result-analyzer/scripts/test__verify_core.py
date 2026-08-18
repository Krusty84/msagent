#!/usr/bin/env python3
"""
_verify_core.py 单元测试
"""

import os
import csv
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "skills", "compare-result-analyzer", "scripts"))

import torch

import _verify_core as vc
from _verify_core import (
    TensorInfo,
    ConstructQuality,
    PATTERN,
    parse_csv,
    group_ops,
    register_op,
    OP_REGISTRY,
    _resolve_by_getattr,
    _wrap_tensor_method,
    _resolve_by_prefix_map,
    _try_resolve_op,
    _parse_op_list_item,
    get_operator_fn,
    _effective_tolerance,
    _get_construct_rng_state,
    _estimate_range_tightness,
    _make_truncnorm,
    _validate_construction,
    make_tensor_from_stats,
    extract_stats,
    _to_native_scalar,
    construct_tensor_pair,
)


def _make_csv(rows, header):
    """helper: write a CSV with given header + rows to a temp file, return path"""
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return path


class TestTensorInfoProperties(unittest.TestCase):
    def _make_ti(self, direction="forward", io_type="input", npu_dtype="torch.float32"):
        return TensorInfo(
            name="Tensor.__add__.1.forward.input.0",
            op_name="Tensor.__add__",
            instance="1",
            direction=direction,
            io_type=io_type,
            idx=0,
            shape=[2, 3],
            npu_dtype=npu_dtype,
            requires_grad=True,
            npu_max=1.0,
            npu_min=-1.0,
            npu_mean=0.0,
            npu_l2norm=2.0,
        )

    def test_is_forward(self):
        ti = self._make_ti(direction="forward")
        self.assertTrue(ti.is_forward)
        self.assertFalse(ti.is_backward)

    def test_is_backward(self):
        ti = self._make_ti(direction="backward")
        self.assertFalse(ti.is_forward)
        self.assertTrue(ti.is_backward)

    def test_torch_dtype(self):
        ti = self._make_ti(npu_dtype="torch.float32")
        self.assertEqual(ti.torch_dtype, torch.float32)
        ti2 = self._make_ti(npu_dtype="torch.int64")
        self.assertEqual(ti2.torch_dtype, torch.int64)
        # unknown dtype falls back to float32
        ti3 = self._make_ti(npu_dtype="torch.nonexistent")
        self.assertEqual(ti3.torch_dtype, torch.float32)


class TestParseCsv(unittest.TestCase):
    HEADER = [
        "NPU Name", "NPU Tensor Shape", "NPU Dtype", "NPU Requires_grad",
        "NPU max", "NPU min", "NPU mean", "NPU l2norm",
    ]

    def test_basic_parse(self):
        path = _make_csv(
            [
                {
                    "NPU Name": "Tensor.__add__.1.forward.input.0",
                    "NPU Tensor Shape": "[2, 3]",
                    "NPU Dtype": "torch.float32",
                    "NPU Requires_grad": "True",
                    "NPU max": "1.0", "NPU min": "-1.0",
                    "NPU mean": "0.0", "NPU l2norm": "2.0",
                },
                {
                    "NPU Name": "Tensor.__add__.1.forward.output.0",
                    "NPU Tensor Shape": "[2, 3]",
                    "NPU Dtype": "torch.float32",
                    "NPU Requires_grad": "False",
                    "NPU max": "1.0", "NPU min": "-1.0",
                    "NPU mean": "0.0", "NPU l2norm": "2.0",
                },
            ],
            self.HEADER,
        )
        try:
            tensors = parse_csv(path)
            self.assertEqual(len(tensors), 2)
            t0 = tensors[0]
            self.assertEqual(t0.op_name, "Tensor.__add__")
            self.assertEqual(t0.instance, "1")
            self.assertEqual(t0.direction, "forward")
            self.assertEqual(t0.io_type, "input")
            self.assertEqual(t0.idx, 0)
            self.assertEqual(t0.shape, [2, 3])
            self.assertEqual(t0.npu_dtype, "torch.float32")
            self.assertTrue(t0.requires_grad)
            self.assertEqual(t0.npu_max, 1.0)
            self.assertEqual(t0.npu_min, -1.0)
            self.assertEqual(t0.npu_mean, 0.0)
            self.assertEqual(t0.npu_l2norm, 2.0)
            self.assertFalse(t0.is_none)
        finally:
            os.remove(path)


class TestGroupOps(unittest.TestCase):
    def test_grouping_and_sort(self):
        # unsorted idx to verify sort
        t_in1 = TensorInfo("Op.mul.1.forward.input.1", "Op.mul", "1", "forward", "input", 1,
                           [2], "torch.float32", False, 1.0, -1.0, 0.0, 1.0)
        t_in0 = TensorInfo("Op.mul.1.forward.input.0", "Op.mul", "1", "forward", "input", 0,
                           [2], "torch.float32", False, 1.0, -1.0, 0.0, 1.0)
        t_out = TensorInfo("Op.mul.1.forward.output.0", "Op.mul", "1", "forward", "output", 0,
                           [2], "torch.float32", False, 1.0, -1.0, 0.0, 1.0)
        t_bwd_in = TensorInfo("Op.mul.1.backward.input.0", "Op.mul", "1", "backward", "input", 0,
                              [2], "torch.float32", False, 1.0, -1.0, 0.0, 1.0)
        groups = group_ops([t_in1, t_in0, t_out, t_bwd_in])
        self.assertIn("Op.mul.1", groups)
        g = groups["Op.mul.1"]
        self.assertEqual(len(g.fwd_inputs), 2)
        self.assertEqual(len(g.fwd_outputs), 1)
        self.assertEqual(len(g.bwd_inputs), 1)
        self.assertEqual(len(g.bwd_outputs), 0)
        # sorted by idx: input.0 before input.1
        self.assertEqual(g.fwd_inputs[0].idx, 0)
        self.assertEqual(g.fwd_inputs[1].idx, 1)


class TestRegisterOp(unittest.TestCase):
    def test_register_and_lookup(self):
        @register_op("Test.CustomOp")
        def _custom(x):
            return x

        self.assertIn("Test.CustomOp", OP_REGISTRY)
        self.assertIs(OP_REGISTRY["Test.CustomOp"], _custom)


class TestResolveByGetattr(unittest.TestCase):
    def test_resolves_existing(self):
        fn = _resolve_by_getattr(torch, "nn.functional.relu")
        self.assertTrue(callable(fn))


class TestWrapTensorMethod(unittest.TestCase):
    def test_wraps_callable(self):
        wrapped = _wrap_tensor_method(torch.Tensor.abs)
        self.assertTrue(callable(wrapped))
        x = torch.tensor([-1.0, 2.0, -3.0])
        result = wrapped(x)
        self.assertTrue(torch.equal(result, x.abs()))


class TestResolveByPrefixMap(unittest.TestCase):
    def test_tensor_prefix(self):
        fn = _resolve_by_prefix_map("Tensor.__add__")
        self.assertTrue(callable(fn))


class TestTryResolveOp(unittest.TestCase):
    def test_tensor_method(self):
        fn = _try_resolve_op("Tensor.__sub__")
        self.assertTrue(callable(fn))


class TestParseOpListItem(unittest.TestCase):
    def test_with_backward(self):
        key, direction = _parse_op_list_item("Tensor.__truediv__.3.backward")
        self.assertEqual(key, "Tensor.__truediv__.3")
        self.assertEqual(direction, "backward")


class TestGetOperatorFn(unittest.TestCase):
    def test_exact_match(self):
        fn = get_operator_fn("Tensor.__add__", auto_register=False)
        self.assertTrue(callable(fn))

    def test_not_found_no_auto(self):
        self.assertIsNone(get_operator_fn("Nonexistent.op", auto_register=False))


class TestEffectiveTolerance(unittest.TestCase):
    def test_float16(self):
        atol, rtol = _effective_tolerance(torch.float16, 1e-4, 1e-3)
        self.assertEqual((atol, rtol), (1e-3, 1e-3))


class TestGetConstructRngState(unittest.TestCase):
    def setUp(self):
        self._saved_seed = vc._CONSTRUCT_SEED
        self._saved_counter = vc._CONSTRUCT_SEED_COUNTER

    def tearDown(self):
        vc._CONSTRUCT_SEED = self._saved_seed
        vc._CONSTRUCT_SEED_COUNTER = self._saved_counter

    def test_with_seed_returns_generator(self):
        vc._CONSTRUCT_SEED = 42
        vc._CONSTRUCT_SEED_COUNTER = 0
        gen = _get_construct_rng_state()
        self.assertIsInstance(gen, torch.Generator)
        # counter incremented
        self.assertEqual(vc._CONSTRUCT_SEED_COUNTER, 1)
        gen2 = _get_construct_rng_state()
        self.assertIsInstance(gen2, torch.Generator)
        self.assertEqual(vc._CONSTRUCT_SEED_COUNTER, 2)


class TestEstimateRangeTightness(unittest.TestCase):
    def test_tight_range(self):
        # span=1, std≈l2norm/sqrt(n)=10/10=1, 4σ=4, span<4σ → tight
        self.assertTrue(_estimate_range_tightness({"max": 1, "min": 0, "l2norm": 10}, 100))

    def test_wide_range(self):
        # span=100, std≈10/sqrt(100)=1, 4σ=4, span>4σ → not tight
        self.assertFalse(_estimate_range_tightness({"max": 100, "min": 0, "l2norm": 10}, 100))


class TestMakeTruncnorm(unittest.TestCase):
    def test_basic_shape(self):
        stats = {"max": 1.0, "min": -1.0, "mean": 0.0, "l2norm": 5.0}
        t = _make_truncnorm([4, 5], torch.float32, stats)
        self.assertEqual(t.shape, torch.Size([4, 5]))
        self.assertTrue((t <= 1.0).all())
        self.assertTrue((t >= -1.0).all())


class TestValidateConstruction(unittest.TestCase):
    def test_scalar_returns_default(self):
        t = torch.tensor(1.0)
        q = _validate_construction(t, {"max": 1, "min": -1, "mean": 0, "l2norm": 1})
        self.assertIsInstance(q, ConstructQuality)
        self.assertEqual(q.l2norm_err, 0.0)
        self.assertFalse(q.degraded)


class TestMakeTensorFromStats(unittest.TestCase):
    def test_randn_strategy(self):
        stats = {"max": 10.0, "min": -10.0, "mean": 0.0, "l2norm": 5.0}
        t = make_tensor_from_stats([4, 5], torch.float32, stats, strategy="randn")
        self.assertEqual(t.shape, torch.Size([4, 5]))
        self.assertTrue((t <= 10.0).all())
        self.assertTrue((t >= -10.0).all())


class TestExtractStats(unittest.TestCase):
    def test_extract(self):
        ti = TensorInfo("n", "op", "1", "forward", "input", 0, [2], "torch.float32",
                        False, 1.5, -0.5, 0.3, 2.2)
        s = extract_stats(ti)
        self.assertEqual(s, {"max": 1.5, "min": -0.5, "mean": 0.3, "l2norm": 2.2})


class TestToNativeScalar(unittest.TestCase):
    def test_float(self):
        self.assertEqual(_to_native_scalar("<class 'float'>", 4.0), 4.0)
        self.assertIsInstance(_to_native_scalar("<class 'float'>", 4.0), float)

    def test_complex(self):
        c = _to_native_scalar("<class 'complex'>", 3.0)
        self.assertEqual(c, complex(3.0, 0.0))
        self.assertIsInstance(c, complex)


class TestConstructTensorPair(unittest.TestCase):
    def test_none_tensor_returns_none_pair(self):
        ti = TensorInfo("x", "op", "1", "forward", "input", 0, [], "torch.float32",
                        False, 0, 0, 0, 0, is_none=True)
        cpu_t, npu_t, q = construct_tensor_pair(ti)
        self.assertIsNone(cpu_t)
        self.assertIsNone(npu_t)
        self.assertIsInstance(q, ConstructQuality)

    def test_numeric_scalar_native(self):
        ti = TensorInfo("Op.mul.2.forward.input.1", "Op.mul", "2", "forward", "input", 1,
                        [2], "<class 'float'>", False, 4.0, 4.0, 4.0, 4.0, is_none=False)
        cpu_t, npu_t, q = construct_tensor_pair(ti)
        self.assertEqual(cpu_t, 4.0)
        self.assertEqual(npu_t, 4.0)
        self.assertFalse(q.degraded)


class TestBuiltinOps(unittest.TestCase):
    def test_add(self):
        x = torch.tensor([1.0, 2.0])
        y = torch.tensor([3.0, 4.0])
        out = OP_REGISTRY["Tensor.__add__"](x, y)
        self.assertTrue(torch.allclose(out, torch.tensor([4.0, 6.0])))

    def test_sub(self):
        x = torch.tensor([5.0])
        y = torch.tensor([2.0])
        out = OP_REGISTRY["Tensor.__sub__"](x, y)
        self.assertTrue(torch.allclose(out, torch.tensor([3.0])))

    def test_truediv(self):
        x = torch.tensor([2.0, 4.0])
        y = torch.tensor([2.0, 4.0])
        out = OP_REGISTRY["Tensor.__truediv__"](x, y)
        self.assertTrue(torch.allclose(out, torch.tensor([1.0, 1.0])))

    def test_matmul(self):
        x = torch.eye(2)
        y = torch.tensor([[1.0, 2.0], [3.0, 4.0]])
        out = OP_REGISTRY["torch.matmul"](x, y)
        self.assertTrue(torch.allclose(out, y))


class TestPattern(unittest.TestCase):
    def test_matches_standard_name(self):
        m = PATTERN.match("Tensor.__add__.3.forward.input.0")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("op"), "Tensor.__add__")
        self.assertEqual(m.group("instance"), "3")
        self.assertEqual(m.group("direction"), "forward")
        self.assertEqual(m.group("io_type"), "input")
        self.assertEqual(m.group("idx"), "0")


if __name__ == "__main__":
    unittest.main()
