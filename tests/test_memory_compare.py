#!/usr/bin/env python3
"""
Unit tests for memory_compare.py
"""

import json
import os
import pickle  # nosec B403
import sys
import tempfile
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "skills", "pytorch-snapshot-memory-comparator", "scripts")
)

from memory_compare import (
    analyze_snapshot_state,
    compare_snapshots,
    compute_expansion_count,
    compute_grown_segments,
    detect_backend,
    fmt_bytes,
    get_segments_by_device,
    load_snapshot,
    print_comparison,
    print_device_overview,
)


def _make_segment(addr=0, total_size=0, allocated_size=0, active_size=0, device=0, segment_type="large", blocks=None):
    return {
        "address": addr,
        "total_size": total_size,
        "allocated_size": allocated_size,
        "active_size": active_size,
        "device": device,
        "segment_type": segment_type,
        "blocks": blocks or [],
    }


def _make_block(size=0, state="active_allocated"):
    return {"size": size, "state": state}


class TestFormatBytes(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(fmt_bytes(0), "0 B")
        self.assertEqual(fmt_bytes(512), "512 B")

    def test_kb(self):
        self.assertEqual(fmt_bytes(2048), "2.0 KB")

    def test_mb(self):
        self.assertEqual(fmt_bytes(5 * 1024 * 1024), "5.0 MB")

    def test_gb(self):
        self.assertEqual(fmt_bytes(2 * 1024**3), "2.00 GB")


class TestDetectBackend(unittest.TestCase):
    def test_detect_cuda(self):
        snap = [_make_segment()]
        self.assertEqual(detect_backend(snap), "cuda")

    def test_detect_npu_by_allocator(self):
        snap = [_make_segment()]
        snap[0]["allocator_name"] = "NPUCachingAllocator"
        self.assertEqual(detect_backend(snap), "npu")

    def test_detect_npu_by_segment_type(self):
        snap = [_make_segment(segment_type="npu_large")]
        self.assertEqual(detect_backend(snap), "npu")

    def test_detect_empty(self):
        self.assertEqual(detect_backend([]), "unknown")

    def test_detect_unknown(self):
        self.assertEqual(detect_backend([{}]), "cuda")


class TestGetSegmentsByDevice(unittest.TestCase):
    def test_filter_by_device(self):
        snap = [
            _make_segment(addr=1, device=0),
            _make_segment(addr=2, device=0),
            _make_segment(addr=3, device=1),
        ]
        result = get_segments_by_device(snap, device_id=0)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["address"], 1)
        self.assertEqual(result[1]["address"], 2)

    def test_no_filter(self):
        snap = [
            _make_segment(addr=1, device=0),
            _make_segment(addr=2, device=1),
        ]
        result = get_segments_by_device(snap)
        self.assertEqual(len(result), 2)


class TestAnalyzeSnapshotState(unittest.TestCase):
    def test_empty(self):
        stats = analyze_snapshot_state([])
        self.assertEqual(stats["total_reserved"], 0)
        self.assertEqual(stats["total_allocated"], 0)
        self.assertEqual(stats["segment_count"], 0)
        self.assertEqual(stats["block_count"], 0)

    def test_single_segment(self):
        segs = [
            _make_segment(
                addr=0x1000,
                total_size=1024,
                allocated_size=512,
                active_size=256,
                segment_type="small",
                device=0,
                blocks=[_make_block(256, "active_allocated"), _make_block(256, "inactive")],
            ),
        ]
        stats = analyze_snapshot_state(segs)
        self.assertEqual(stats["total_reserved"], 1024)
        self.assertEqual(stats["total_allocated"], 512)
        self.assertEqual(stats["total_active"], 256)
        self.assertEqual(stats["total_free"], 512)
        self.assertEqual(stats["segment_count"], 1)
        self.assertEqual(stats["block_count"], 2)
        self.assertEqual(stats["largest_segment"], 1024)
        self.assertEqual(stats["largest_block"], 256)
        self.assertEqual(stats["block_states"]["active_allocated"], 1)
        self.assertEqual(stats["block_states"]["inactive"], 1)
        self.assertEqual(stats["segment_types"]["small"], 1)

    def test_multiple_segments(self):
        segs = [
            _make_segment(
                addr=1, total_size=1024, allocated_size=1024, segment_type="large", blocks=[_make_block(1024)]
            ),
            _make_segment(
                addr=2,
                total_size=2048,
                allocated_size=1024,
                segment_type="small",
                blocks=[_make_block(512), _make_block(512)],
            ),
            _make_segment(addr=3, total_size=4096, allocated_size=0, segment_type="large", blocks=[]),
        ]
        stats = analyze_snapshot_state(segs)
        self.assertEqual(stats["total_reserved"], 7168)
        self.assertEqual(stats["total_allocated"], 2048)
        self.assertEqual(stats["total_free"], 5120)
        self.assertEqual(stats["segment_count"], 3)
        self.assertEqual(stats["block_count"], 3)
        self.assertEqual(stats["largest_segment"], 4096)
        self.assertEqual(stats["largest_block"], 1024)
        self.assertEqual(stats["segment_types"]["large"], 2)
        self.assertEqual(stats["segment_types"]["small"], 1)


class TestComputeExpansionCount(unittest.TestCase):
    def test_no_expansion(self):
        segs_a = [_make_segment(addr=1), _make_segment(addr=2)]
        segs_b = [_make_segment(addr=1), _make_segment(addr=2)]
        self.assertEqual(compute_expansion_count(segs_a, segs_b), 0)

    def test_new_segments(self):
        segs_a = [_make_segment(addr=1)]
        segs_b = [_make_segment(addr=1), _make_segment(addr=2), _make_segment(addr=3)]
        self.assertEqual(compute_expansion_count(segs_a, segs_b), 2)

    def test_all_new(self):
        segs_a = []
        segs_b = [_make_segment(addr=1)]
        self.assertEqual(compute_expansion_count(segs_a, segs_b), 1)


class TestComputeGrownSegments(unittest.TestCase):
    def test_no_growth(self):
        segs_a = [_make_segment(addr=1, total_size=1024)]
        segs_b = [_make_segment(addr=1, total_size=1024)]
        result = compute_grown_segments(segs_a, segs_b)
        self.assertEqual(result, [])

    def test_grown(self):
        segs_a = [_make_segment(addr=1, total_size=1024)]
        segs_b = [_make_segment(addr=1, total_size=2048)]
        result = compute_grown_segments(segs_a, segs_b)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["size_a"], 1024)
        self.assertEqual(result[0]["size_b"], 2048)
        self.assertEqual(result[0]["growth"], 1024)

    def test_shrink_not_grown(self):
        segs_a = [_make_segment(addr=1, total_size=2048)]
        segs_b = [_make_segment(addr=1, total_size=1024)]
        result = compute_grown_segments(segs_a, segs_b)
        self.assertEqual(result, [])

    def test_mixed(self):
        segs_a = [
            _make_segment(addr=1, total_size=1024),
            _make_segment(addr=2, total_size=2048),
        ]
        segs_b = [
            _make_segment(addr=1, total_size=4096),
            _make_segment(addr=2, total_size=1024),
        ]
        result = compute_grown_segments(segs_a, segs_b)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["growth"], 3072)


class TestCompareSnapshots(unittest.TestCase):
    def test_basic_comparison(self):
        snap_a = [
            _make_segment(
                addr=1,
                total_size=1024,
                allocated_size=512,
                active_size=256,
                blocks=[_make_block(256, "active_allocated")],
            ),
        ]
        snap_b = [
            _make_segment(
                addr=1,
                total_size=2048,
                allocated_size=1024,
                active_size=512,
                blocks=[_make_block(512, "active_allocated")],
            ),
            _make_segment(
                addr=2,
                total_size=4096,
                allocated_size=2048,
                active_size=1024,
                blocks=[_make_block(1024, "active_allocated"), _make_block(1024, "inactive")],
            ),
        ]

        result = compare_snapshots(snap_a, snap_b)

        self.assertEqual(result["summary"]["reserved_a"], 1024)
        self.assertEqual(result["summary"]["reserved_b"], 6144)
        self.assertEqual(result["summary"]["reserved_diff"], 5120)
        self.assertEqual(result["summary"]["allocated_a"], 512)
        self.assertEqual(result["summary"]["allocated_b"], 3072)
        self.assertEqual(result["summary"]["allocated_diff"], 2560)
        self.assertEqual(result["expansions"]["new_segments"], 1)
        self.assertEqual(len(result["expansions"]["grown_segments"]), 1)

    def test_comparison_with_devices(self):
        snap_a = [
            _make_segment(addr=1, total_size=1024, device=0),
            _make_segment(addr=2, total_size=2048, device=1),
        ]
        snap_b = [
            _make_segment(addr=1, total_size=4096, device=0),
            _make_segment(addr=2, total_size=2048, device=1),
        ]

        result = compare_snapshots(snap_a, snap_b, device_a=0, device_b=0)
        self.assertEqual(result["summary"]["reserved_a"], 1024)
        self.assertEqual(result["summary"]["reserved_b"], 4096)
        self.assertEqual(result["summary"]["reserved_diff"], 3072)

    def test_fragmentation_calculation(self):
        snap_a = [_make_segment(addr=1, total_size=1000, allocated_size=800)]
        snap_b = [_make_segment(addr=1, total_size=1000, allocated_size=950)]

        result = compare_snapshots(snap_a, snap_b)

        self.assertEqual(result["summary"]["fragmentation_a"], 20.0)
        self.assertEqual(result["summary"]["fragmentation_b"], 5.0)


class TestLoadSnapshot(unittest.TestCase):
    def test_load_valid_pickle(self):
        snap = [_make_segment(addr=1, total_size=1024)]
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            pickle.dump(snap, f)
            tmp_path = f.name

        try:
            result = load_snapshot(tmp_path)
            self.assertIsInstance(result, list)
            self.assertEqual(len(result), 1)
        finally:
            os.unlink(tmp_path)

    def test_load_nonexistent(self):
        with self.assertRaises(FileNotFoundError):
            load_snapshot("/nonexistent/path.pkl")

    def test_load_invalid_format(self):
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            pickle.dump("not_a_list", f)
            tmp_path = f.name

        try:
            with self.assertRaises(ValueError):
                load_snapshot(tmp_path)
        finally:
            os.unlink(tmp_path)


class TestPrintFunctions(unittest.TestCase):
    def test_print_comparison(self):
        snap_a = [_make_segment(addr=1, total_size=1024, allocated_size=512)]
        snap_b = [_make_segment(addr=1, total_size=2048, allocated_size=1024)]
        result = compare_snapshots(snap_a, snap_b)

        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            print_comparison(result, ("snap_a.pkl", "snap_b.pkl"))
        output = f.getvalue()

        self.assertIn("PyTorch Memory Snapshot 对比报告", output)
        self.assertIn("Reserved (峰值)", output)
        self.assertIn("扩容分析", output)

    def test_print_device_overview(self):
        snap = [
            _make_segment(addr=1, total_size=1024, allocated_size=512, device=0),
            _make_segment(addr=2, total_size=2048, allocated_size=1024, device=1),
        ]

        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            print_device_overview(snap)
        output = f.getvalue()

        self.assertIn("Memory Snapshot 全卡概览", output)
        self.assertIn("Device", output)


class TestJSONReport(unittest.TestCase):
    def test_output_json_report(self):
        snap_a = [_make_segment(addr=1, total_size=1024, allocated_size=512)]
        snap_b = [_make_segment(addr=1, total_size=2048, allocated_size=1024)]
        result = compare_snapshots(snap_a, snap_b)

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False, encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
            tmp_path = f.name

        try:
            self.assertTrue(os.path.exists(tmp_path))
            with open(tmp_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded["summary"]["reserved_diff"], 1024)
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
