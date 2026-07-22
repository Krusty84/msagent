#!/usr/bin/env python3
"""Deterministic unit tests for the read-only IO snapshot collector."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import collect_io_snapshot as c  # noqa: E402


def _provider(source: str, parsed=None) -> c.ProviderResult:
    return c.ProviderResult(
        source=source, status=c.STATUS_OK, parsed={} if parsed is None else parsed
    )


class TestCollectorContracts(unittest.TestCase):
    def test_parse_interval_rejects_unsafe_values(self):
        for value in (True, False, 0, -1, 1.5, float("nan"), float("inf"), "1"):
            with self.subTest(value=value):
                self.assertIsNone(c.parse_interval(value))
        self.assertEqual(c.parse_interval(1), 1)
        self.assertEqual(c.parse_interval(30.0), 30)
        self.assertEqual(c.parse_interval(86400), 86400)

    def test_explicit_target_path_controls_collector_relevance(self):
        self.assertTrue(
            c._is_data_relevant_path_collector("/opt/dataset/shard.bin", "/opt/dataset")
        )
        self.assertFalse(
            c._is_data_relevant_path_collector("/cache/unrelated.bin", "/nfs/train")
        )
        self.assertTrue(c._is_data_relevant_path_collector("/usrdata/shard.bin"))

    def test_iostat_json_supports_sysstat_aliases(self):
        raw = json.dumps(
            {
                "sysstat": {
                    "hosts": [
                        {
                            "statistics": [
                                {
                                    "disk": [
                                        {
                                            "disk_device": "sda",
                                            "r/s": 10,
                                            "rkB/s": 100,
                                            "r_await": 2,
                                            "avgqu-sz": 1,
                                            "util": 40,
                                        }
                                    ]
                                },
                                {
                                    "disk": [
                                        {
                                            "disk_name": "sda",
                                            "r/s": 20,
                                            "rkB/s": 200,
                                            "r_await": 4,
                                            "aqu-sz": 3,
                                            "%util": 60,
                                        }
                                    ]
                                },
                            ]
                        }
                    ]
                }
            }
        )
        parsed = c._parse_iostat_json(raw)
        self.assertIsNotNone(parsed)
        disk = parsed["disks"]["sda"]
        self.assertEqual(disk["sample_count"], 2)
        self.assertEqual(disk["util_percent"], 50.0)
        self.assertEqual(disk["avgqu_sz"], 2.0)
        self.assertAlmostEqual(disk["r_await_ms"], 10 / 3, places=4)

    def test_empty_iostat_json_falls_back_to_text(self):
        empty_json = json.dumps(
            {"sysstat": {"hosts": [{"statistics": [{"disk": []}]}]}}
        )
        text_output = """Linux host 5.10 aarch64

Device r/s w/s rkB/s wkB/s rrqm/s wrqm/s r_await w_await aqu-sz %util
sda 10 0 100 0 0 0 2 0 1 40
"""
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(
                c,
                "_run_with_env",
                side_effect=[
                    (0, empty_json, ""),
                    (0, text_output, ""),
                ],
            ),
        ):
            result = c.collect_iostat(1)
        self.assertEqual(result.status, c.STATUS_OK)
        self.assertEqual(result.parsed["source_format"], "text")
        self.assertIn("sda", result.parsed["disks"])

    def test_pidstat_json_supports_modern_io_schema(self):
        raw = json.dumps(
            {
                "sysstat": {
                    "hosts": [
                        {
                            "statistics": [
                                {
                                    "io": [
                                        {
                                            "UID": "1000",
                                            "PID": "42",
                                            "kB_rd/s": 100,
                                            "kB_wr/s": 20,
                                            "kB_ccwr/s": 1,
                                            "cmd": "python3",
                                        }
                                    ]
                                },
                                {
                                    "io": [
                                        {
                                            "UID": "1000",
                                            "PID": 42,
                                            "kB_rd/s": 300,
                                            "kB_wr/s": 40,
                                            "kB_ccwr/s": 3,
                                            "cmd": "python3",
                                        }
                                    ]
                                },
                            ]
                        }
                    ]
                }
            }
        )
        parsed = c._parse_pidstat_json(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["reports"], 2)
        self.assertEqual(parsed["processes"][0]["pid"], 42)
        self.assertEqual(parsed["processes"][0]["kbr_per_s"], 200)
        self.assertEqual(parsed["processes"][0]["command"], "python3")

    def test_unrecognized_pidstat_json_falls_back_to_text(self):
        unknown_json = json.dumps(
            {"sysstat": {"hosts": [{"statistics": [{"cpu-load": []}]}]}}
        )
        text_output = """Linux host 5.10 aarch64

12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command
12:00:01 1000 42 100.00 20.00 0.00 0 python3
"""
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(
                c,
                "_run_with_env",
                side_effect=[(0, unknown_json, ""), (0, text_output, "")],
            ),
        ):
            result = c.collect_pidstat(1)
        self.assertEqual(result.status, c.STATUS_OK)
        self.assertEqual(result.parsed["source_format"], "text")
        self.assertEqual(result.parsed["processes"][0]["pid"], 42)
        self.assertEqual(result.parsed["processes"][0]["command"], "python3")

    def test_pidstat_text_with_no_io_rows_is_valid_empty_observation(self):
        empty_text = """Linux host 5.10 aarch64

12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command

Average: UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command
"""
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(
                c,
                "_run_with_env",
                side_effect=[(1, "", "unsupported"), (0, empty_text, "")],
            ),
        ):
            result = c.collect_pidstat(1)
        self.assertEqual(result.status, c.STATUS_OK)
        self.assertEqual(result.parsed["processes"], [])
        self.assertEqual(result.parsed["source_format"], "text")

    def test_mountstats_delta_uses_window_counters(self):
        before = """device server:/data mounted on /mnt/data with fstype nfs4 statvers=1.1
bytes: 1000 2000 0 0 0 0 0 0
per-op statistics
READ: 100 101 0 0 0 0 500 700 0
GETATTR: 50 50 0 0 0 0 250 400 0
"""
        after = """device server:/data mounted on /mnt/data with fstype nfs4 statvers=1.1
bytes: 9000 6000 0 0 0 0 0 0
per-op statistics
READ: 300 305 0 0 0 0 15500 20700 0
GETATTR: 150 151 0 0 0 0 2250 3400 0
"""
        metrics = c._diff_mount_metrics(
            c._parse_mountstats(before), c._parse_mountstats(after)
        )
        self.assertEqual(len(metrics), 1)
        item = metrics[0]
        self.assertEqual(item["windowing"], "delta")
        self.assertEqual(item["ops"], 300.0)
        self.assertEqual(item["retrans"], 5.0)
        self.assertEqual(item["avg_data_rtt_ms"], 75.0)
        self.assertEqual(item["avg_metadata_rtt_ms"], 20.0)
        self.assertEqual(item["bytes_read_delta"], 8000.0)

    def test_stacked_mounts_keep_distinct_sources(self):
        before = """device a:/data mounted on /mnt/data with fstype nfs4 statvers=1.1
per-op statistics
READ: 10 10 0 0 0 0 100 100 0
device b:/data mounted on /mnt/data with fstype nfs4 statvers=1.1
per-op statistics
READ: 20 20 0 0 0 0 200 200 0
"""
        after = """device a:/data mounted on /mnt/data with fstype nfs4 statvers=1.1
per-op statistics
READ: 20 20 0 0 0 0 200 200 0
device b:/data mounted on /mnt/data with fstype nfs4 statvers=1.1
per-op statistics
READ: 40 40 0 0 0 0 400 400 0
"""
        parsed = c._parse_mountstats(before)
        self.assertEqual(len(parsed), 2)
        metrics = c._diff_mount_metrics(parsed, c._parse_mountstats(after))
        self.assertEqual({item["source"] for item in metrics}, {"a:/data", "b:/data"})
        self.assertEqual({item["ops"] for item in metrics}, {10.0, 20.0})

    def test_second_diskstats_read_failure_is_reported(self):
        first = "8 0 sda 1 0 1 1 1 0 1 1 0 1 1\n"
        with (
            patch.object(
                c,
                "_read_file",
                side_effect=[(first, "", 0), ("", "read failed", 3)],
            ),
            patch.object(c, "_is_real_block_device", return_value=True),
            patch.object(c.time, "sleep"),
        ):
            provider, samples = c.collect_block_devices(1)
        self.assertEqual(provider.status, c.STATUS_CMD_FAILED)
        self.assertEqual(samples, [])

    def test_nfs_mount_discovery_failure_is_propagated(self):
        failed = c.ProviderResult(
            source="mounts", status=c.STATUS_PERMISSION, error="denied"
        )
        with patch.object(c, "collect_mounts", return_value=failed):
            result = c.collect_nfs(1)
        self.assertEqual(result.status, c.STATUS_PERMISSION)
        self.assertIn("mount discovery failed", result.error)

    def test_df_partial_command_failure_is_not_ok(self):
        space = "Filesystem Size Used Avail Use% Mounted on\n/dev/sda 10G 1G 9G 10% /\n"
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(
                c,
                "_run",
                side_effect=[(0, space, ""), (1, "", "inode df failed")],
            ),
        ):
            result = c.collect_df()
        self.assertEqual(result.status, c.STATUS_CMD_FAILED)
        self.assertIn("inode df failed", result.stderr)

    def test_all_provider_crashes_are_isolated(self):
        crashing = {
            "collect_block_devices": "block",
            "collect_iostat": "iostat",
            "collect_pidstat": "pidstat",
            "collect_nfs": "nfs",
            "collect_process_io_map": "process map",
            "collect_mounts": "mounts",
            "collect_df": "df",
            "collect_memory": "memory",
            "collect_readahead_scheduler": "readahead",
        }
        with ExitStack() as stack:
            for name, message in crashing.items():
                stack.enter_context(
                    patch.object(c, name, side_effect=RuntimeError(message))
                )
            snapshot = c.collect(1, None, None)

        for name in (
            "block_devices",
            "iostat",
            "pidstat",
            "process_io_map",
            "memory",
            "df",
            "nfs",
        ):
            self.assertEqual(getattr(snapshot, name).status, c.STATUS_CMD_FAILED)
        self.assertEqual(snapshot.mounts_provider.status, c.STATUS_CMD_FAILED)
        self.assertGreaterEqual(len(snapshot.availability.errors), 8)
        self.assertTrue(
            any("readahead_scheduler" in item for item in snapshot.availability.partial)
        )
        c.IoSnapshot.model_validate_json(snapshot.model_dump_json())

    def test_invalid_block_provider_result_is_isolated(self):
        with (
            patch.object(c, "collect_block_devices", return_value="bad"),
            patch.object(c, "collect_iostat", return_value=_provider("iostat")),
            patch.object(c, "collect_pidstat", return_value=_provider("pidstat")),
            patch.object(c, "collect_nfs", return_value=_provider("nfs")),
            patch.object(
                c,
                "collect_process_io_map",
                return_value=_provider("process_io_map"),
            ),
            patch.object(
                c,
                "collect_mounts",
                return_value=_provider("mounts", parsed=[]),
            ),
            patch.object(c, "collect_df", return_value=_provider("df")),
            patch.object(c, "collect_memory", return_value=_provider("memory")),
            patch.object(c, "collect_readahead_scheduler", return_value=({}, {}, [])),
        ):
            snapshot = c.collect(1, None, None)
        self.assertEqual(snapshot.block_devices.status, c.STATUS_CMD_FAILED)
        self.assertEqual(snapshot.diskstats_sample, [])

    def test_snapshot_write_is_atomic_and_validated(self):
        snapshot = c.IoSnapshot(collected_at=c._now_iso())
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "snapshot.json"
            self.assertEqual(c.write_snapshot(snapshot, str(output)), 0)
            saved = json.loads(output.read_text(encoding="utf-8"))
            c.IoSnapshot.model_validate(saved)
            leftovers = list(Path(temp_dir).glob(".*.tmp"))
            self.assertEqual(leftovers, [])

    def test_snapshot_write_missing_parent_fails_without_temp_file(self):
        snapshot = c.IoSnapshot(collected_at=c._now_iso())
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "missing" / "snapshot.json"
            stderr = StringIO()
            with redirect_stderr(stderr):
                rc = c.write_snapshot(snapshot, str(output))
            self.assertEqual(rc, 1)
            self.assertIn("输出目录不存在", stderr.getvalue())
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(temp_dir).rglob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
