#!/usr/bin/env python3
"""Unit tests for deterministic msprof profile summarization."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import summarize_msprof as summary  # noqa: E402


FIELDS = [
    "Device_id",
    "Task Start Time(us)",
    "Task Duration(us)",
    "mte2_ratio",
]


def _write_profile(path: Path, rows: list[dict[str, object]]) -> Path:
    output = path / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    csv_path = output / "op_summary_20260720.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


class TestMsprofSummary(unittest.TestCase):
    def test_computes_union_free_time_and_weighted_mte2(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_profile(
                root,
                [
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 1_000_000,
                        "Task Duration(us)": 10,
                        "mte2_ratio": 0.2,
                    },
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 1_000_020,
                        "Task Duration(us)": 10,
                        "mte2_ratio": 0.4,
                    },
                ],
            )
            payload = summary.summarize(root)
        self.assertAlmostEqual(payload["device_free_percent"], 100 / 3, places=5)
        self.assertEqual(payload["mte2_ratio"], 0.3)
        self.assertEqual(payload["provenance"]["task_count"], 2)
        self.assertFalse(payload["provenance"]["conduction_evidence_inferred"])

    def test_overlapping_tasks_are_not_double_counted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_profile(
                root,
                [
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 100,
                        "Task Duration(us)": 20,
                        "mte2_ratio": 0.2,
                    },
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 110,
                        "Task Duration(us)": 20,
                        "mte2_ratio": 0.4,
                    },
                ],
            )
            payload = summary.summarize(root)
        self.assertEqual(payload["device_free_percent"], 0.0)

    def test_multiple_devices_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_profile(
                root,
                [
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 100,
                        "Task Duration(us)": 10,
                        "mte2_ratio": 0.2,
                    },
                    {
                        "Device_id": 1,
                        "Task Start Time(us)": 100,
                        "Task Duration(us)": 10,
                        "mte2_ratio": 0.4,
                    },
                ],
            )
            with self.assertRaisesRegex(ValueError, "select one"):
                summary.summarize(root)
            payload = summary.summarize(root, device=1)
        self.assertEqual(payload["provenance"]["device_id"], 1)

    def test_rejects_invalid_ratio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_profile(
                root,
                [
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 100,
                        "Task Duration(us)": 10,
                        "mte2_ratio": 1.1,
                    }
                ],
            )
            with self.assertRaisesRegex(ValueError, "between 0 and 1"):
                summary.summarize(root)


if __name__ == "__main__":
    unittest.main()
