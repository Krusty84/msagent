#!/usr/bin/env python3
"""Unit tests for deterministic msprof profile summarization."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import summarize_msprof as summary  # noqa: E402


FIELDS = [
    "Device_id",
    "Task Start Time(us)",
    "Task Duration(us)",
    "mte2_ratio",
]


def _write_profile(
    path: Path,
    rows: list[dict[str, object]],
    fields: list[str] | None = None,
) -> Path:
    output = path / "mindstudio_profiler_output"
    output.mkdir(parents=True)
    csv_path = output / "op_summary_20260720.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


class TestMsprofSummary(unittest.TestCase):
    def test_outputs_non_certifying_gap_and_ratio_proxies(self):
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
        proxies = payload["diagnostic_proxies"]
        self.assertAlmostEqual(
            proxies["op_summary_task_gap_proxy_percent"], 100 / 3, places=5
        )
        self.assertEqual(
            proxies["mte2_ratio_by_column"]["mte2_ratio"]["arithmetic_mean"],
            0.3,
        )
        self.assertNotIn("device_free_percent", payload)
        self.assertNotIn("mte2_ratio", payload)
        self.assertNotIn("profile_window", payload)
        self.assertEqual(payload["provenance"]["task_count"], 2)
        self.assertEqual(payload["provenance"]["r500_certifying_metrics"], [])
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
        self.assertEqual(
            payload["diagnostic_proxies"]["op_summary_task_gap_proxy_percent"],
            0.0,
        )

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

    def test_accepts_prefixed_ratio_columns_and_na_values(self):
        fields = [
            "Device_id",
            "Task Start Time(us)",
            "Task Duration(us)",
            "aic_mte2_ratio",
            "aiv_mte2_ratio",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_profile(
                root,
                [
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 100,
                        "Task Duration(us)": 10,
                        "aic_mte2_ratio": "N/A",
                        "aiv_mte2_ratio": 0.4,
                    },
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 120,
                        "Task Duration(us)": 10,
                        "aic_mte2_ratio": 0.2,
                        "aiv_mte2_ratio": "",
                    },
                ],
                fields,
            )
            payload = summary.summarize(root)
        ratios = payload["diagnostic_proxies"]["mte2_ratio_by_column"]
        self.assertEqual(ratios["aic_mte2_ratio"]["sample_count"], 1)
        self.assertEqual(ratios["aiv_mte2_ratio"]["sample_count"], 1)

    def test_pmu_ratio_column_is_optional(self):
        fields = ["Device_id", "Task Start Time(us)", "Task Duration(us)"]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_profile(
                root,
                [
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 100,
                        "Task Duration(us)": 10,
                    }
                ],
                fields,
            )
            payload = summary.summarize(root)
        self.assertEqual(payload["diagnostic_proxies"]["mte2_ratio_by_column"], {})

    def test_rejects_task_end_overflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_profile(
                root,
                [
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 1e308,
                        "Task Duration(us)": 1e308,
                        "mte2_ratio": 0.2,
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "task end time must be finite"):
                summary.summarize(root)

    def test_rejects_csv_over_row_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_profile(
                root,
                [
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": index,
                        "Task Duration(us)": 1,
                        "mte2_ratio": 0.2,
                    }
                    for index in range(3)
                ],
            )
            with patch.object(summary, "_MAX_OP_SUMMARY_ROWS", 2):
                with self.assertRaisesRegex(ValueError, "row budget"):
                    summary.summarize(root)

    def test_directory_budget_counts_empty_subdirectories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(3):
                (root / f"empty-{index}").mkdir()
            _write_profile(
                root,
                [
                    {
                        "Device_id": 0,
                        "Task Start Time(us)": 1,
                        "Task Duration(us)": 1,
                        "mte2_ratio": 0.2,
                    }
                ],
            )
            with patch.object(summary, "_MAX_PROFILE_WALK_ENTRIES", 2):
                with self.assertRaisesRegex(ValueError, "traversal"):
                    summary.summarize(root)


if __name__ == "__main__":
    unittest.main()
