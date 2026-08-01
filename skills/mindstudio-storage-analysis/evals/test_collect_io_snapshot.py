#!/usr/bin/env python3
"""Deterministic unit tests for the read-only IO snapshot collector."""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from contextlib import ExitStack, nullcontext, redirect_stderr
from io import StringIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import mock_open, patch

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import collect_io_snapshot as c  # noqa: E402
import analyze_io_snapshot as a  # noqa: E402


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

    def test_target_symlink_resolves_from_selected_process_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "nfs" / "train"
            target.mkdir(parents=True)
            link = root / "data-link"
            link.symlink_to(target, target_is_directory=True)

            resolved = c._resolve_target_path(str(link), c.os.getpid())

        self.assertEqual(resolved, str(target.resolve()))
        self.assertTrue(c._is_data_relevant_path_collector("/usrdata/shard.bin"))

    def test_path_without_pid_does_not_scan_all_processes(self):
        with patch.object(
            c.os, "scandir", side_effect=AssertionError("must not scan /proc")
        ):
            result = c.collect_process_io_map(None, "/data", 0)

        self.assertEqual(result.status, c.STATUS_UNSUPPORTED)
        self.assertIn("without --pid", result.error)

    def test_read_file_rejects_content_over_byte_budget(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
            stream.write("x" * (c._MAX_FILE_READ_BYTES + 1))
            stream.flush()
            content, error, status = c._read_file(stream.name)

        self.assertEqual(content, "")
        self.assertEqual(status, 3)
        self.assertIn("read budget", error)

    def test_open_file_records_honors_global_deadline(self):
        with (
            patch.object(c.time, "monotonic", return_value=10.0),
            patch.object(
                c.os, "scandir", side_effect=AssertionError("deadline expired")
            ),
        ):
            records, denied, truncated = c._opened_file_records(42, deadline=9.0)

        self.assertEqual(records, [])
        self.assertFalse(denied)
        self.assertTrue(truncated)

    def test_process_tree_uses_children_only_and_caps_descendants(self):
        with (
            patch.object(
                c.os, "listdir", side_effect=AssertionError("must not scan /proc")
            ),
            patch.object(
                c,
                "_children_of",
                side_effect=lambda pid: list(range(1000, 1400)) if pid == 42 else [],
            ),
        ):
            tree, truncated = c._process_tree(42)

        self.assertTrue(truncated)
        self.assertEqual(len(tree), c._MAX_PROCESS_TREE_PIDS)
        self.assertEqual(tree[0], {"pid": 42, "role": "root"})
        self.assertEqual(tree[1]["parent_pid"], 42)

    def test_children_parser_has_byte_and_pid_bounds(self):
        payload = " ".join(str(pid) for pid in range(10_000))
        with patch("builtins.open", mock_open(read_data=payload)):
            children = c._children_of(42)

        self.assertEqual(len(children), c._MAX_PROCESS_TREE_PIDS)

    def test_open_file_records_caps_fd_scan(self):
        def readlink(path):
            if path.endswith("/cwd"):
                return "/data"
            return f"/data/fd-{path.rsplit('/', 1)[-1]}"

        with (
            patch.object(c, "_MAX_OPEN_FILE_RECORDS", 2),
            patch.object(
                c.os,
                "scandir",
                return_value=nullcontext(
                    [SimpleNamespace(name=name) for name in ("0", "1", "2")]
                ),
            ),
            patch.object(c.os, "readlink", side_effect=readlink),
            patch.object(c, "_fd_mnt_id", return_value=10),
        ):
            records, denied, truncated = c._opened_file_records(42)

        self.assertFalse(denied)
        self.assertTrue(truncated)
        self.assertEqual([record["fd"] for record in records], [0, 1])

    def test_open_file_records_prioritize_explicit_target_path(self):
        def readlink(path):
            if path.endswith("/cwd"):
                return "/"
            fd = path.rsplit("/", 1)[-1]
            return "/data/target.bin" if fd == "2" else f"/lib/lib{fd}.so"

        with (
            patch.object(c, "_MAX_OPEN_FILE_RECORDS", 2),
            patch.object(
                c.os,
                "scandir",
                return_value=nullcontext(
                    [SimpleNamespace(name=name) for name in ("0", "1", "2")]
                ),
            ),
            patch.object(c.os, "readlink", side_effect=readlink),
            patch.object(c, "_fd_mnt_id", return_value=10),
        ):
            records, _denied, truncated = c._opened_file_records(42, "/data")

        self.assertTrue(truncated)
        self.assertLessEqual(len(records), 2)
        self.assertEqual(records[0]["path"], "/data/target.bin")

    def test_readahead_timeout_records_partial_and_uses_sysfs_fallback(self):
        def read_file(path):
            if path.endswith("read_ahead_kb"):
                return "128\n", "", 0
            return "", "", 1

        with (
            patch.object(c, "_list_block_devices", return_value=["sda"]),
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(c, "_run", return_value=(124, "", "timed out")) as run,
            patch.object(c, "_read_file", side_effect=read_file),
        ):
            readahead, _scheduler, partial = c.collect_readahead_scheduler()

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["blockdev", "--getra", "/dev/sda"])
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(
            run.call_args.kwargs["timeout"], c._STATIC_PROBE_TIMEOUT_SECONDS
        )
        self.assertEqual(readahead["/dev/sda"], 256)
        self.assertTrue(any("timed out" in item for item in partial))

    def test_readahead_stops_after_global_probe_budget(self):
        with (
            patch.object(c, "_list_block_devices", return_value=["sda", "sdb"]),
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(c, "_run", return_value=(0, "128\n", "")) as run,
            patch.object(c.time, "monotonic", side_effect=[0.0, 0.0, 5.0, 5.0]),
        ):
            readahead, scheduler, partial = c.collect_readahead_scheduler(5)

        run.assert_called_once_with(["blockdev", "--getra", "/dev/sda"], timeout=5)
        self.assertEqual(readahead, {"/dev/sda": 128})
        self.assertEqual(scheduler, {})
        self.assertTrue(any("budget 5s exhausted" in item for item in partial))

    def test_command_runner_caps_excess_stdout_without_buffering_it(self):
        with (
            patch.object(c, "_MAX_COMMAND_STDOUT_BYTES", 32),
            patch.object(c, "_COMMAND_DIAGNOSTIC_BYTES", 16),
        ):
            code, stdout, stderr = c._run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 4096)",
                ],
                timeout=5,
            )

        self.assertEqual(code, c._OUTPUT_LIMIT_EXIT_CODE)
        self.assertIn("output budget exceeded", stdout)
        self.assertIn("output exceeded", stderr)
        self.assertLess(len(stdout.encode("utf-8")), 128)

    def test_iostat_marks_capped_command_output_as_failed(self):
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(
                c,
                "_run_with_env",
                return_value=(
                    c._OUTPUT_LIMIT_EXIT_CODE,
                    "[truncated: command output budget exceeded]",
                    "iostat: stdout output exceeded its byte budget; process terminated",
                ),
            ),
        ):
            result = c.collect_iostat(1)

        self.assertEqual(result.status, c.STATUS_CMD_FAILED)
        self.assertLess(len(result.raw.encode("utf-8")), 128)
        self.assertIn("output exceeded", result.stderr)

    def test_process_mapping_merge_records_two_observations(self):
        previous = {
            "pid": 42,
            "path": "/data/shard.bin",
            "first_seen": "2026-07-22T00:00:00+00:00",
            "last_seen": "2026-07-22T00:00:01+00:00",
            "observation_count": 1,
        }
        current = {
            "pid": 42,
            "path": "/data/shard.bin",
            "first_seen": "2026-07-22T00:00:29+00:00",
            "last_seen": "2026-07-22T00:00:30+00:00",
            "observation_count": 1,
        }

        merged = c._merge_mapping_observation(previous, current, "fallback")

        self.assertEqual(merged["first_seen"], previous["first_seen"])
        self.assertEqual(merged["last_seen"], current["last_seen"])
        self.assertEqual(merged["observation_count"], 2)

    def test_pid_starttime_parser_handles_parenthesized_command(self):
        trailing = ["S", *("0" for _ in range(18)), "12345"]
        stat = f"42 (worker ) name) {' '.join(trailing)}\n"
        with patch.object(c, "_read_file", return_value=(stat, "", 0)):
            self.assertEqual(c._pid_starttime_ticks(42), 12345)

    def test_process_map_second_observation_refreshes_mount_identity(self):
        mount_tables = [
            [
                {
                    "mount_id": 10,
                    "mount_point": "/data",
                    "source": "/dev/sda",
                    "fstype": "ext4",
                    "major_minor": "8:0",
                }
            ],
            [
                {
                    "mount_id": 10,
                    "mount_point": "/data",
                    "source": "/dev/sdb",
                    "fstype": "ext4",
                    "major_minor": "8:16",
                }
            ],
        ]

        def canonical(major_minor, source, _cache=None):
            return {
                "canonical_device": source.removeprefix("/dev/"),
                "major_minor": major_minor,
                "backing_devices": [],
                "device_resolution": "sysfs",
            }

        opened = [
            {
                "path": "/data/shard.bin",
                "fd": 3,
                "mnt_id": 10,
                "path_source": "fd",
            }
        ]
        with (
            patch.object(c.os.path, "isdir", return_value=True),
            patch.object(
                c, "_process_tree", return_value=([{"pid": 42, "role": "root"}], False)
            ),
            patch.object(
                c, "_opened_file_records", return_value=(opened, False, False)
            ),
            patch.object(c, "_read_boot_id", return_value="boot-a"),
            patch.object(c, "_pid_starttime_ticks", return_value=100),
            patch.object(c, "_mount_namespace_key", return_value="mnt:[1]|root:1:1"),
            patch.object(
                c, "_mountinfo_table", side_effect=mount_tables
            ) as read_mountinfo,
            patch.object(c, "_resolve_canonical_device", side_effect=canonical),
            patch.object(c.time, "sleep"),
        ):
            result = c.collect_process_io_map(42, "/data", 0.1)

        mappings = result.parsed["mappings"]
        self.assertEqual(read_mountinfo.call_count, 2)
        self.assertEqual(
            {item["source"] for item in mappings}, {"/dev/sda", "/dev/sdb"}
        )
        self.assertEqual([item["observation_count"] for item in mappings], [1, 1])

    def test_process_map_does_not_merge_reused_numeric_pid(self):
        table = [
            {
                "mount_id": 10,
                "mount_point": "/data",
                "source": "/dev/sda",
                "fstype": "ext4",
                "major_minor": "8:0",
            }
        ]
        canonical = {
            "canonical_device": "sda",
            "major_minor": "8:0",
            "backing_devices": [],
            "device_resolution": "sysfs",
        }
        opened = [
            {
                "path": "/data/shard.bin",
                "fd": 3,
                "mnt_id": 10,
                "path_source": "fd",
            }
        ]
        with (
            patch.object(c.os.path, "isdir", return_value=True),
            patch.object(
                c, "_process_tree", return_value=([{"pid": 42, "role": "root"}], False)
            ),
            patch.object(
                c, "_opened_file_records", return_value=(opened, False, False)
            ),
            patch.object(c, "_read_boot_id", return_value="boot-a"),
            patch.object(c, "_pid_starttime_ticks", side_effect=[100, 100, 200, 200]),
            patch.object(c, "_mount_namespace_key", return_value="mnt:[1]|root:1:1"),
            patch.object(c, "_mountinfo_table", return_value=table),
            patch.object(c, "_resolve_canonical_device", return_value=canonical),
            patch.object(c.time, "sleep"),
        ):
            result = c.collect_process_io_map(42, "/data", 0.1)

        mappings = result.parsed["mappings"]
        self.assertEqual(len(mappings), 2)
        self.assertEqual({item["pid_starttime_ticks"] for item in mappings}, {100, 200})
        self.assertTrue(all(item["observation_count"] == 1 for item in mappings))

    def test_process_map_drops_identity_change_during_observation(self):
        table = [
            {
                "mount_id": 10,
                "mount_point": "/data",
                "source": "/dev/sda",
                "fstype": "ext4",
                "major_minor": "8:0",
            }
        ]
        opened = [
            {
                "path": "/data/shard.bin",
                "fd": 3,
                "mnt_id": 10,
                "path_source": "fd",
            }
        ]
        with (
            patch.object(c.os.path, "isdir", return_value=True),
            patch.object(
                c, "_process_tree", return_value=([{"pid": 42, "role": "root"}], False)
            ),
            patch.object(
                c, "_opened_file_records", return_value=(opened, False, False)
            ),
            patch.object(c, "_read_boot_id", return_value="boot-a"),
            patch.object(c, "_pid_starttime_ticks", side_effect=[100, 200]),
            patch.object(c, "_mount_namespace_key", return_value="mnt:[1]|root:1:1"),
            patch.object(c, "_mountinfo_table", return_value=table),
            patch.object(
                c,
                "_resolve_canonical_device",
                return_value={
                    "canonical_device": "sda",
                    "major_minor": "8:0",
                    "backing_devices": [],
                    "device_resolution": "sysfs",
                },
            ),
        ):
            result = c.collect_process_io_map(42, "/data", 0)

        self.assertEqual(result.parsed["mappings"], [])
        self.assertTrue(
            any("identity changed" in item for item in result.parsed["partial"])
        )

    def test_process_map_refreshes_canonical_backing_topology(self):
        table = [
            {
                "mount_id": 10,
                "mount_point": "/data",
                "source": "/dev/dm-0",
                "fstype": "ext4",
                "major_minor": "253:0",
            }
        ]
        canonical_results = [
            {
                "canonical_device": "dm-0",
                "major_minor": "253:0",
                "backing_devices": ["sda"],
                "device_resolution": "sysfs",
            },
            {
                "canonical_device": "dm-0",
                "major_minor": "253:0",
                "backing_devices": ["sdb"],
                "device_resolution": "sysfs",
            },
        ]
        opened = [
            {
                "path": "/data/shard.bin",
                "fd": 3,
                "mnt_id": 10,
                "path_source": "fd",
            }
        ]
        with (
            patch.object(c.os.path, "isdir", return_value=True),
            patch.object(
                c, "_process_tree", return_value=([{"pid": 42, "role": "root"}], False)
            ),
            patch.object(
                c, "_opened_file_records", return_value=(opened, False, False)
            ),
            patch.object(c, "_read_boot_id", return_value="boot-a"),
            patch.object(c, "_pid_starttime_ticks", return_value=100),
            patch.object(c, "_mount_namespace_key", return_value="mnt:[1]|root:1:1"),
            patch.object(c, "_mountinfo_table", return_value=table),
            patch.object(
                c, "_resolve_canonical_device_impl", side_effect=canonical_results
            ) as resolve_device,
            patch.object(c.time, "sleep"),
        ):
            result = c.collect_process_io_map(42, "/data", 0.1)

        mappings = result.parsed["mappings"]
        self.assertEqual(resolve_device.call_count, 2)
        self.assertEqual(
            {tuple(item["backing_devices"]) for item in mappings}, {("sda",), ("sdb",)}
        )
        self.assertTrue(all(item["observation_count"] == 1 for item in mappings))

    def test_process_map_unknown_fd_mount_id_does_not_use_path_prefix(self):
        table = [
            {
                "mount_id": 9,
                "mount_point": "/data",
                "source": "/dev/sda",
                "fstype": "ext4",
                "major_minor": "8:0",
            }
        ]
        with (
            patch.object(c, "_mount_namespace_key", return_value="mnt:[1]|root:1:1"),
            patch.object(c, "_mountinfo_table", return_value=table),
            patch.object(c, "_have_cmd", return_value=False),
        ):
            resolved = c._resolve_path_to_mount("/data/shard.bin", pid=42, mnt_id=10)

        self.assertIsNone(resolved)

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
        self.assertEqual(disk["util_sample_count"], 2)
        self.assertEqual(disk["util_percent"], 50.0)
        self.assertEqual(disk["avgqu_sz"], 2.0)
        self.assertAlmostEqual(disk["r_await_ms"], 10 / 3, places=4)

    def test_iostat_json_rejects_non_list_disk_container(self):
        for value in (1, True, {"disk_name": "sda"}, "sda"):
            with self.subTest(value=value):
                raw = json.dumps(
                    {"sysstat": {"hosts": [{"statistics": [{"disk": value}]}]}}
                )
                self.assertIsNone(c._parse_iostat_json(raw))

    def test_iostat_json_rejects_non_finite_or_non_scalar_metrics(self):
        for value in (10**400, "1e309", True, [], {}):
            with self.subTest(value=repr(value)):
                raw = json.dumps(
                    {
                        "sysstat": {
                            "hosts": [
                                {
                                    "statistics": [
                                        {"disk": [{"disk_device": "sda", "r/s": value}]}
                                    ]
                                }
                            ]
                        }
                    }
                )
                with patch.object(c, "_is_real_block_device", return_value=True):
                    self.assertIsNone(c._parse_iostat_json(raw))

    def test_iostat_json_rejects_mixed_valid_and_invalid_devices(self):
        for value in ("1e309", None, True, [], {}, 10**400):
            with self.subTest(value=repr(value)):
                raw = json.dumps(
                    {
                        "sysstat": {
                            "hosts": [
                                {
                                    "statistics": [
                                        {
                                            "disk": [
                                                {"disk_device": "sda", "r/s": 1},
                                                {"disk_device": "sdb", "r/s": value},
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                )
                with patch.object(c, "_is_real_block_device", return_value=True):
                    self.assertIsNone(c._parse_iostat_json(raw))

    def test_iostat_json_rejects_one_bad_report_for_valid_device(self):
        raw = json.dumps(
            {
                "sysstat": {
                    "hosts": [
                        {
                            "statistics": [
                                {"disk": [{"disk_device": "sda", "r/s": 1}]},
                                {"disk": [{"disk_device": "sda", "r/s": "1e309"}]},
                            ]
                        }
                    ]
                }
            }
        )
        with patch.object(c, "_is_real_block_device", return_value=True):
            self.assertIsNone(c._parse_iostat_json(raw))

    def test_iostat_json_rejects_mixed_non_object_records(self):
        valid_disk = {"disk_device": "sda", "r/s": 1}
        payloads = (
            [
                {"disk": [valid_disk]},
                None,
            ],
            [
                {"disk": [valid_disk, None]},
            ],
        )
        for statistics in payloads:
            with self.subTest(statistics=statistics):
                raw = json.dumps({"sysstat": {"hosts": [{"statistics": statistics}]}})
                with patch.object(c, "_is_real_block_device", return_value=True):
                    self.assertIsNone(c._parse_iostat_json(raw))

    def test_iostat_json_rejects_missing_disk_block_or_device_name(self):
        payloads = (
            [
                {"disk": [{"disk_device": "sda", "r/s": 1}]},
                {"avg-cpu": {"idle": 100}},
            ],
            [
                {
                    "disk": [
                        {"disk_device": "sda", "r/s": 1},
                        {"r/s": 2},
                    ]
                }
            ],
        )
        for statistics in payloads:
            with self.subTest(statistics=statistics):
                raw = json.dumps({"sysstat": {"hosts": [{"statistics": statistics}]}})
                with patch.object(c, "_is_real_block_device", return_value=True):
                    self.assertIsNone(c._parse_iostat_json(raw))

    def test_iostat_aggregation_of_large_finite_values_stays_finite(self):
        aggregated = c._aggregate_iostat_samples(
            [
                {
                    "r_per_s": 1e308,
                    "r_await_ms": 1e308,
                    "util_percent": 1e308,
                },
                {
                    "r_per_s": 1e308,
                    "r_await_ms": 1e308,
                    "util_percent": 1e308,
                },
            ]
        )

        self.assertIsNotNone(aggregated)
        self.assertTrue(math.isfinite(aggregated["r_per_s"]))
        self.assertTrue(math.isfinite(aggregated["r_await_ms"]))
        self.assertTrue(math.isfinite(aggregated["util_percent"]))
        self.assertTrue(math.isfinite(aggregated["util_p95"]))

    def test_iostat_text_rejects_present_invalid_numeric_token(self):
        for value in ("inf", "nan", "1e309", str(10**400), "invalid"):
            with self.subTest(value=value):
                text = f"Device r/s w/s await aqu-sz %util\nsda {value} 0 1 1 90\n"
                with patch.object(c, "_is_real_block_device", return_value=True):
                    self.assertIsNone(c._parse_iostat_text(text))

        alias_text = "Device r/s aqu-sz avgqu-sz %util\nsda 1 1 inf 90\n"
        with patch.object(c, "_is_real_block_device", return_value=True):
            self.assertIsNone(c._parse_iostat_text(alias_text))

    def test_iostat_text_rejects_garbage_block_or_short_device_row(self):
        payloads = (
            "Device r/s %util\nsda 1 90\n\ngarbage block\n",
            "Device r/s %util\nsda\n",
        )
        for text in payloads:
            with self.subTest(text=text):
                with patch.object(c, "_is_real_block_device", return_value=True):
                    self.assertIsNone(c._parse_iostat_text(text))

    def test_iostat_text_accepts_known_sysstat_preamble(self):
        text = """Linux host 5.10 aarch64

avg-cpu: %user %system %idle
0 0 100

Device r/s %util
sda 1 10
"""
        with patch.object(c, "_is_real_block_device", return_value=True):
            parsed = c._parse_iostat_text(text)
        self.assertEqual(parsed["disks"]["sda"]["r_per_s"], 1)

    def test_malformed_mixed_iostat_is_parse_failed_and_preserves_raw(self):
        raw = json.dumps(
            {
                "sysstat": {
                    "hosts": [
                        {
                            "statistics": [
                                {
                                    "disk": [
                                        {"disk_device": "sda", "r/s": 1},
                                        {
                                            "disk_device": "sdb",
                                            "r/s": "1e309",
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        )
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(c, "_is_real_block_device", return_value=True),
            patch.object(c, "_run_with_env", return_value=(0, raw, "")),
        ):
            result = c.collect_iostat(1)

        self.assertEqual(result.status, c.STATUS_PARSE_FAILED)
        self.assertIsNone(result.parsed)
        self.assertEqual(result.raw, raw)

    def test_sparse_iostat_util_uses_only_present_samples(self):
        aggregated = c._aggregate_iostat_samples(
            [
                {"util_percent": 90.0, "r_per_s": 10.0},
                {"r_per_s": 20.0},
                {"util_percent": 100.0, "r_per_s": 30.0},
            ]
        )
        self.assertEqual(aggregated["sample_count"], 3)
        self.assertEqual(aggregated["util_sample_count"], 2)
        self.assertEqual(aggregated["util_percent"], 95.0)

    def test_iostat_clamps_small_util_rounding_overshoot(self):
        aggregated = c._aggregate_iostat_samples(
            [
                {"util_percent": 100.1, "avgqu_sz": 2.0, "r_per_s": 100.0},
                {"util_percent": 100.0, "avgqu_sz": 2.0, "r_per_s": 100.0},
            ]
        )
        self.assertEqual(aggregated["util_percent"], 100.0)
        self.assertEqual(aggregated["util_max"], 100.0)

    def test_iostat_drops_isolated_impossible_util_in_long_window(self):
        samples = [
            {"util_percent": 0.2, "avgqu_sz": 0.0} for _ in range(199)
        ] + [{"util_percent": 488.5, "avgqu_sz": 0.0}]
        aggregated = c._aggregate_iostat_samples(samples)
        self.assertEqual(aggregated["util_invalid_sample_count"], 1)
        self.assertEqual(aggregated["util_sample_count"], 199)
        self.assertEqual(aggregated["util_max"], 0.2)
        self.assertEqual(aggregated["avgqu_sz_with_util_sample_count"], 199)

    def test_iostat_preserves_frequent_invalid_util_for_analyzer_rejection(self):
        aggregated = c._aggregate_iostat_samples(
            [
                {"util_percent": 10.0},
                {"util_percent": 10.0},
                {"util_percent": 10.0},
                {"util_percent": 488.5},
            ]
        )
        self.assertNotIn("util_invalid_sample_count", aggregated)
        self.assertEqual(aggregated["util_max"], 488.5)

    def test_sparse_iostat_supporting_fields_record_sample_counts(self):
        aggregated = c._aggregate_iostat_samples(
            [
                {"util_percent": 95.0, "avgqu_sz": 4.0, "await": 30.0},
                {"util_percent": 95.0},
                {"util_percent": 95.0},
            ]
        )
        self.assertEqual(aggregated["sample_count"], 3)
        self.assertEqual(aggregated["util_sample_count"], 3)
        self.assertEqual(aggregated["avgqu_sz_sample_count"], 1)
        self.assertEqual(aggregated["await_sample_count"], 1)
        self.assertEqual(aggregated["avgqu_sz_with_util_sample_count"], 1)
        self.assertEqual(aggregated["await_with_util_sample_count"], 1)

    def test_iostat_support_counts_require_same_report_as_util(self):
        aggregated = c._aggregate_iostat_samples(
            [
                {"util_percent": 95.0},
                {"util_percent": 95.0},
                {"util_percent": 95.0},
                {"avgqu_sz": 4.0, "await": 30.0},
                {"avgqu_sz": 4.0, "await": 30.0},
                {"avgqu_sz": 4.0, "await": 30.0},
            ]
        )
        self.assertEqual(aggregated["util_sample_count"], 3)
        self.assertEqual(aggregated["avgqu_sz_sample_count"], 3)
        self.assertEqual(aggregated["await_sample_count"], 3)
        self.assertEqual(aggregated["avgqu_sz_with_util_sample_count"], 0)
        self.assertEqual(aggregated["await_with_util_sample_count"], 0)

    def test_combined_await_is_weighted_by_total_iops(self):
        aggregated = c._aggregate_iostat_samples(
            [
                {"r_per_s": 1.0, "w_per_s": 0.0, "await": 100.0},
                {"r_per_s": 1000.0, "w_per_s": 0.0, "await": 1.0},
                {"r_per_s": 1000.0, "w_per_s": 0.0, "await": 1.0},
            ]
        )
        self.assertAlmostEqual(aggregated["await"], 2100 / 2001, places=4)

    def test_weighted_legacy_await_does_not_trigger_r100(self):
        text_output = """Device r/s w/s await aqu-sz %util
sda 1 0 100 0.1 95

Device r/s w/s await aqu-sz %util
sda 1000 0 1 0.1 95

Device r/s w/s await aqu-sz %util
sda 1000 0 1 0.1 95
"""
        with patch.object(c, "_is_real_block_device", return_value=True):
            parsed = c._parse_iostat_text(text_output)
        self.assertIsNotNone(parsed)
        parsed["disks"]["sda"]["device_type"] = "ssd"

        fixture_path = SKILL_ROOT / "evals" / "fixtures" / "rc-r100-bandwidth.json"
        snapshot = json.loads(fixture_path.read_text(encoding="utf-8"))
        snapshot["iostat"]["parsed"] = parsed
        snapshot["diskstats_sample"] = []
        finding = next(
            item
            for item in a.analyze_all(snapshot)["findings"]
            if item["rule_id"] == "R100"
        )
        self.assertAlmostEqual(parsed["disks"]["sda"]["await"], 2100 / 2001, places=4)
        self.assertEqual(finding["severity"], "info")
        self.assertFalse(finding.get("saturated_devices"))

    def test_empty_iostat_json_does_not_start_second_full_window(self):
        empty_json = json.dumps(
            {"sysstat": {"hosts": [{"statistics": [{"disk": []}]}]}}
        )
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(
                c,
                "_run_with_env",
                side_effect=[
                    (0, empty_json, ""),
                ],
            ),
        ):
            result = c.collect_iostat(1)
        self.assertEqual(result.status, c.STATUS_EMPTY)
        self.assertIsNone(result.parsed)

    def test_unrecognized_iostat_metrics_do_not_start_second_full_window(self):
        incompatible_json = json.dumps(
            {
                "sysstat": {
                    "hosts": [
                        {
                            "statistics": [
                                {"disk": [{"disk_name": "sda", "new_metric": 1}]}
                            ]
                        }
                    ]
                }
            }
        )
        timestamps = [
            "2026-07-22T00:00:00.000000+00:00",
            "2026-07-22T00:00:00.100000+00:00",
            "2026-07-22T00:00:01.100000+00:00",
            "2026-07-22T00:00:01.200000+00:00",
            "2026-07-22T00:00:02.200000+00:00",
        ]
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(c, "_now_iso", side_effect=timestamps),
            patch.object(c, "_device_type", return_value="unknown"),
            patch.object(
                c,
                "_run_with_env",
                side_effect=[
                    (0, incompatible_json, ""),
                ],
            ),
        ):
            result = c.collect_iostat(1)
        self.assertEqual(result.status, c.STATUS_PARSE_FAILED)
        self.assertEqual(result.started_at, timestamps[1])
        self.assertEqual(result.ended_at, timestamps[2])

    def test_unparseable_iostat_preserves_last_raw_output_and_window(self):
        incompatible_json = '{"unexpected": true}'
        timestamps = [
            "2026-07-22T00:00:00.000000+00:00",
            "2026-07-22T00:00:00.100000+00:00",
            "2026-07-22T00:00:01.100000+00:00",
            "2026-07-22T00:00:01.200000+00:00",
            "2026-07-22T00:00:02.200000+00:00",
            "2026-07-22T00:00:02.300000+00:00",
        ]
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(c, "_now_iso", side_effect=timestamps),
            patch.object(
                c,
                "_run_with_env",
                side_effect=[
                    (0, incompatible_json, ""),
                ],
            ),
        ):
            result = c.collect_iostat(1)
        self.assertEqual(result.status, c.STATUS_PARSE_FAILED)
        self.assertEqual(result.raw, incompatible_json)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.started_at, timestamps[1])
        self.assertEqual(result.ended_at, timestamps[2])

    def test_recognized_iostat_without_real_devices_is_empty(self):
        empty_json = json.dumps(
            {"sysstat": {"hosts": [{"statistics": [{"disk": []}]}]}}
        )
        timestamps = [
            "2026-07-22T00:00:00.000000+00:00",
            "2026-07-22T00:00:00.100000+00:00",
            "2026-07-22T00:00:01.100000+00:00",
            "2026-07-22T00:00:01.200000+00:00",
            "2026-07-22T00:00:02.200000+00:00",
        ]
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(c, "_now_iso", side_effect=timestamps),
            patch.object(
                c,
                "_run_with_env",
                side_effect=[
                    (0, empty_json, ""),
                ],
            ),
        ):
            result = c.collect_iostat(1)
        self.assertEqual(result.status, c.STATUS_EMPTY)
        self.assertEqual(result.raw, empty_json)
        self.assertEqual(result.started_at, timestamps[1])
        self.assertEqual(result.ended_at, timestamps[2])

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
        self.assertEqual(parsed["processes"][0]["active_sample_count"], 2)
        self.assertEqual(parsed["processes"][0]["command"], "python3")

    def test_pidstat_json_rejects_non_integer_or_non_finite_pid(self):
        payloads = (
            '{"sysstat":{"hosts":[{"statistics":[{"io":[{"PID":1e309}]}]}]}}',
            json.dumps(
                {"sysstat": {"hosts": [{"statistics": [{"io": [{"PID": True}]}]}]}}
            ),
            json.dumps(
                {"sysstat": {"hosts": [{"statistics": [{"io": [{"PID": 42.5}]}]}]}}
            ),
        )
        for raw in payloads:
            with self.subTest(raw=raw):
                self.assertIsNone(c._parse_pidstat_json(raw))

    def test_pidstat_json_rejects_non_finite_or_non_scalar_rates(self):
        for value in (10**400, "1e309", True, [], {}):
            with self.subTest(value=repr(value)):
                raw = json.dumps(
                    {
                        "sysstat": {
                            "hosts": [
                                {
                                    "statistics": [
                                        {
                                            "io": [
                                                {
                                                    "PID": 42,
                                                    "kB_rd/s": value,
                                                    "kB_wr/s": 0,
                                                    "kB_ccwr/s": 0,
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                )
                self.assertIsNone(c._parse_pidstat_json(raw))

    def test_pidstat_json_rejects_mixed_non_object_records(self):
        valid_task = {
            "PID": 42,
            "kB_rd/s": 1,
            "kB_wr/s": 0,
            "kB_ccwr/s": 0,
        }
        payloads = (
            [{"io": [valid_task]}, None],
            [{"io": [valid_task, None]}],
        )
        for statistics in payloads:
            with self.subTest(statistics=statistics):
                raw = json.dumps({"sysstat": {"hosts": [{"statistics": statistics}]}})
                self.assertIsNone(c._parse_pidstat_json(raw))

    def test_pidstat_json_rejects_unknown_block_or_missing_core_rate(self):
        complete = {"PID": 42, "kB_rd/s": 1, "kB_wr/s": 2}
        payloads = (
            [{"io": [complete]}, {"cpu-load": []}],
            [{"io": [{"PID": 42, "kB_rd/s": 1}]}],
            [{"io": [{"PID": 42, "kB_wr/s": 1}]}],
        )
        for statistics in payloads:
            with self.subTest(statistics=statistics):
                raw = json.dumps({"sysstat": {"hosts": [{"statistics": statistics}]}})
                self.assertIsNone(c._parse_pidstat_json(raw))

        raw = json.dumps({"sysstat": {"hosts": [{"statistics": [{"io": [complete]}]}]}})
        parsed = c._parse_pidstat_json(raw)
        self.assertEqual(parsed["processes"][0]["kbccwd_per_s"], 0)

    def test_pidstat_aggregation_of_large_finite_values_stays_finite(self):
        raw = json.dumps(
            {
                "sysstat": {
                    "hosts": [
                        {
                            "statistics": [
                                {
                                    "io": [
                                        {
                                            "PID": 42,
                                            "kB_rd/s": 1e308,
                                            "kB_wr/s": 1e308,
                                            "kB_ccwr/s": 1e308,
                                        }
                                    ]
                                },
                                {
                                    "io": [
                                        {
                                            "PID": 42,
                                            "kB_rd/s": 1e308,
                                            "kB_wr/s": 1e308,
                                            "kB_ccwr/s": 1e308,
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
        process = parsed["processes"][0]
        self.assertTrue(math.isfinite(process["kbr_per_s"]))
        self.assertTrue(math.isfinite(process["kbw_per_s"]))
        self.assertTrue(math.isfinite(process["kbccwd_per_s"]))

    def test_pidstat_json_counts_alternating_read_write_as_active(self):
        statistics = []
        for index in range(4):
            statistics.append(
                {
                    "io": [
                        {
                            "UID": "1000",
                            "PID": 42,
                            "kB_rd/s": 150 if index % 2 == 0 else 0,
                            "kB_wr/s": 0 if index % 2 == 0 else 150,
                            "kB_ccwr/s": 0,
                            "cmd": "python3",
                        }
                    ]
                }
            )
        raw = json.dumps({"sysstat": {"hosts": [{"statistics": statistics}]}})

        parsed = c._parse_pidstat_json(raw)

        process = parsed["processes"][0]
        self.assertEqual(parsed["reports"], 4)
        self.assertEqual(process["kbr_per_s"], 75)
        self.assertEqual(process["kbw_per_s"], 75)
        self.assertEqual(process["active_sample_count"], 4)

    def test_pidstat_text_counts_alternating_read_write_as_active(self):
        reports = []
        for index in range(4):
            read = 150 if index % 2 == 0 else 0
            write = 0 if index % 2 == 0 else 150
            reports.append(
                "12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command\n"
                f"12:00:0{index + 1} 1000 42 {read} {write} 0 0 python3"
            )

        parsed = c._parse_pidstat_text("\n\n".join(reports))

        process = parsed["processes"][0]
        self.assertEqual(parsed["reports"], 4)
        self.assertEqual(process["kbr_per_s"], 75)
        self.assertEqual(process["kbw_per_s"], 75)
        self.assertEqual(process["active_sample_count"], 4)

    def test_pidstat_text_excludes_average_block_from_report_count(self):
        reports = []
        for index, read in enumerate((150, 150, 150, 0, 0), start=1):
            reports.append(
                "12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command\n"
                f"12:00:0{index} 1000 42 {read} 0 0 0 python3"
            )
        reports.append(
            "Average: UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command\n"
            "Average: 1000 42 90 0 0 0 python3"
        )

        parsed = c._parse_pidstat_text("\n\n".join(reports))

        process = parsed["processes"][0]
        self.assertEqual(parsed["reports"], 5)
        self.assertEqual(process["sample_count"], 5)
        self.assertEqual(process["active_sample_count"], 3)
        self.assertEqual(
            a._active_io_pids(parsed["processes"], parsed["reports"]), {42}
        )

    def test_pidstat_text_rejects_present_invalid_rate(self):
        for value in ("inf", "nan", "1e309", str(10**400), "invalid"):
            with self.subTest(value=value):
                text = (
                    "12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command\n"
                    f"12:00:01 1000 42 {value} 0 0 0 python3"
                )
                self.assertIsNone(c._parse_pidstat_text(text))

    def test_pidstat_text_rejects_invalid_pid_without_dropping_row(self):
        for pid in ("0", "2147483648", "42.5", "invalid", "99999999999"):
            with self.subTest(pid=pid):
                text = (
                    "12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command\n"
                    f"12:00:01 1000 {pid} 1 0 0 0 python3"
                )
                self.assertIsNone(c._parse_pidstat_text(text))

    def test_pidstat_text_requires_core_rates_but_allows_missing_cancel_rate(self):
        invalid_headers = (
            ("12:00:00 UID PID kB_rd/s Command\n12:00:01 1000 42 1 python3"),
            ("12:00:00 UID PID kB_wr/s Command\n12:00:01 1000 42 1 python3"),
        )
        for text in invalid_headers:
            with self.subTest(text=text):
                self.assertIsNone(c._parse_pidstat_text(text))

        compatible = (
            "12:00:00 UID PID kB_rd/s kB_wr/s Command\n12:00:01 1000 42 1 2 python3"
        )
        parsed = c._parse_pidstat_text(compatible)
        self.assertEqual(parsed["processes"][0]["kbccwd_per_s"], 0)

    def test_pidstat_text_uses_each_report_header(self):
        first = (
            "12:00:00 UID PID kB_rd/s kB_wr/s Command\n12:00:01 1000 42 10 20 python3"
        )
        second = (
            "12:00:00 PID UID kB_wr/s kB_rd/s Command\n12:00:02 42 1000 40 30 python3"
        )

        parsed = c._parse_pidstat_text(f"{first}\n\n{second}")

        process = parsed["processes"][0]
        self.assertEqual(process["kbr_per_s"], 20)
        self.assertEqual(process["kbw_per_s"], 30)

    def test_pidstat_text_rejects_garbage_block_or_short_row(self):
        valid = (
            "12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command\n"
            "12:00:01 1000 42 1 0 0 0 python3"
        )
        payloads = (
            f"{valid}\n\ngarbage block",
            (
                "12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command\n"
                "12:00:01 1000"
            ),
        )
        for text in payloads:
            with self.subTest(text=text):
                self.assertIsNone(c._parse_pidstat_text(text))

    def test_pidstat_text_large_finite_values_stay_finite(self):
        report = (
            "12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command\n"
            "12:00:01 1000 42 1e308 1e308 1e308 0 python3"
        )

        parsed = c._parse_pidstat_text(f"{report}\n\n{report}")

        self.assertIsNotNone(parsed)
        process = parsed["processes"][0]
        self.assertTrue(math.isfinite(process["kbr_per_s"]))
        self.assertTrue(math.isfinite(process["kbw_per_s"]))
        self.assertTrue(math.isfinite(process["kbccwd_per_s"]))

    def test_pidstat_json_without_processes_is_empty(self):
        raw = json.dumps({"sysstat": {"hosts": [{"statistics": [{"io": []}]}]}})
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(c, "_run_with_env", return_value=(0, raw, "")),
        ):
            result = c.collect_pidstat(1)

        self.assertEqual(result.status, c.STATUS_EMPTY)
        self.assertEqual(result.parsed["processes"], [])
        self.assertEqual(result.parsed["source_format"], "json")

    def test_malformed_pidstat_text_is_parse_failed_and_preserves_raw(self):
        bad_text = (
            "12:00:00 UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay Command\n"
            "12:00:01 1000 42 inf 0 0 0 python3"
        )
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(
                c,
                "_run_with_env",
                side_effect=[
                    (1, "", "invalid option -- o"),
                    (0, bad_text, ""),
                ],
            ),
        ):
            result = c.collect_pidstat(1)

        self.assertEqual(result.status, c.STATUS_PARSE_FAILED)
        self.assertIsNone(result.parsed)
        self.assertEqual(result.raw, bad_text)

    def test_unrecognized_pidstat_json_does_not_start_second_full_window(self):
        unknown_json = json.dumps(
            {"sysstat": {"hosts": [{"statistics": [{"cpu-load": []}]}]}}
        )
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(
                c,
                "_run_with_env",
                return_value=(0, unknown_json, ""),
            ),
        ):
            result = c.collect_pidstat(1)
        self.assertEqual(result.status, c.STATUS_PARSE_FAILED)
        self.assertIsNone(result.parsed)

    def test_pidstat_fallback_records_winning_attempt_window(self):
        unknown_json = json.dumps(
            {"sysstat": {"hosts": [{"statistics": [{"cpu-load": []}]}]}}
        )
        timestamps = [
            "2026-07-22T00:00:00.000000+00:00",
            "2026-07-22T00:00:00.100000+00:00",
            "2026-07-22T00:00:01.100000+00:00",
            "2026-07-22T00:00:01.200000+00:00",
            "2026-07-22T00:00:02.200000+00:00",
        ]
        with (
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(c, "_now_iso", side_effect=timestamps),
            patch.object(c, "_run_with_env", return_value=(0, unknown_json, "")),
        ):
            result = c.collect_pidstat(1)
        self.assertEqual(result.started_at, timestamps[0])
        self.assertEqual(result.ended_at, timestamps[3])

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
                side_effect=[(1, "", "invalid option"), (0, empty_text, "")],
            ),
        ):
            result = c.collect_pidstat(1)
        self.assertEqual(result.status, c.STATUS_EMPTY)
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

    def test_diskstats_accepts_mmc_ceph_and_network_block_devices(self):
        rows = "\n".join(
            [
                "179 0 mmcblk0 1 0 1 1 1 0 1 1 0 1 1",
                "179 1 mmcblk0p1 1 0 1 1 1 0 1 1 0 1 1",
                "252 0 rbd0 1 0 1 1 1 0 1 1 0 1 1",
                "252 1 rbd0p1 1 0 1 1 1 0 1 1 0 1 1",
                "43 0 nbd0 1 0 1 1 1 0 1 1 0 1 1",
                "7 0 loop0 1 0 1 1 1 0 1 1 0 1 1",
            ]
        )
        with patch.object(c, "_read_file", return_value=("", "missing", 1)):
            disks = c._parse_diskstats(rows)
        self.assertEqual(set(disks), {"mmcblk0", "rbd0", "nbd0"})

    def test_block_device_heuristic_normalizes_new_device_partitions(self):
        for source, expected in (
            ("/dev/mmcblk0p1", "mmcblk0"),
            ("/dev/rbd0p1", "rbd0"),
            ("/dev/nbd0p1", "nbd0"),
        ):
            with self.subTest(source=source):
                result = c._resolve_canonical_device_impl("", source)
                self.assertEqual(result["canonical_device"], expected)
                self.assertEqual(result["device_resolution"], "heuristic")

    def test_nfs_mount_discovery_failure_is_propagated(self):
        failed = c.ProviderResult(
            source="mounts", status=c.STATUS_PERMISSION, error="denied"
        )
        with patch.object(c, "collect_mounts", return_value=failed):
            result = c.collect_nfs(1)
        self.assertEqual(result.status, c.STATUS_PERMISSION)
        self.assertIn("mount discovery failed", result.error)

    def test_nfsiostat_tail_does_not_extend_mountstats_evidence_window(self):
        mounts = _provider(
            "mounts",
            parsed=[
                {
                    "device": "server:/data",
                    "mount_point": "/mnt/data",
                    "fstype": "nfs4",
                }
            ],
        )
        mountstats = (
            "device server:/data mounted on /mnt/data with fstype nfs4 statvers=1.1\n"
            "per-op statistics\n"
            "READ: 100 100 0 0 0 0 500 700 0\n"
        )
        timestamps = [
            "2026-07-23T00:00:00+00:00",
            "2026-07-23T00:00:01+00:00",
            "2026-07-23T00:00:11+00:00",
        ]
        with (
            patch.object(c, "collect_mounts", return_value=mounts),
            patch.object(
                c,
                "_read_file",
                side_effect=[
                    (mountstats, "", 0),
                    ("rpc 100 0\n", "", 0),
                    (mountstats, "", 0),
                    ("rpc 100 0\n", "", 0),
                ],
            ),
            patch.object(c, "_now_iso", side_effect=timestamps),
            patch.object(c.time, "sleep"),
            patch.object(c, "_have_cmd", return_value=True),
            patch.object(c, "_run", return_value=(0, "slow nfsiostat output", "")),
            patch.object(c, "_mount_namespace_key", return_value="mnt:[1]|root:1:1"),
        ):
            result = c.collect_nfs(10)

        self.assertEqual(result.started_at, timestamps[1])
        self.assertEqual(result.ended_at, timestamps[2])
        self.assertIn("slow nfsiostat output", result.parsed["nfsiostat_raw"])

    def test_non_contract_nfs_prefix_is_not_collected(self):
        mounts = _provider(
            "mounts",
            parsed=[
                {
                    "device": "server:/data",
                    "mount_point": "/data",
                    "fstype": "nfsbogus",
                }
            ],
        )
        with patch.object(c, "collect_mounts", return_value=mounts):
            result = c.collect_nfs(1)
        self.assertEqual(result.status, c.STATUS_UNSUPPORTED)

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

    def test_mount_identity_is_collected_concurrently_with_dynamic_window(self):
        mounts_started = Event()

        def collect_iostat(_duration):
            if not mounts_started.wait(timeout=1):
                raise RuntimeError("mount collection did not start in dynamic window")
            return _provider("iostat")

        def collect_mounts(_pid):
            mounts_started.set()
            return _provider("mounts", parsed=[])

        with (
            patch.object(
                c,
                "collect_block_devices",
                return_value=(_provider("block_devices"), []),
            ),
            patch.object(c, "collect_iostat", side_effect=collect_iostat),
            patch.object(c, "collect_pidstat", return_value=_provider("pidstat")),
            patch.object(c, "collect_nfs", return_value=_provider("nfs")),
            patch.object(
                c,
                "collect_process_io_map",
                return_value=_provider("process_io_map"),
            ),
            patch.object(c, "collect_mounts", side_effect=collect_mounts),
            patch.object(c, "collect_df", return_value=_provider("df")),
            patch.object(c, "collect_memory", return_value=_provider("memory")),
            patch.object(c, "collect_readahead_scheduler", return_value=({}, {}, [])),
        ):
            snapshot = c.collect(1, None, None)

        self.assertTrue(mounts_started.is_set())
        self.assertEqual(snapshot.mounts_provider.status, c.STATUS_OK)
        self.assertEqual(snapshot.iostat.status, c.STATUS_OK)

    def test_static_probes_do_not_extend_snapshot_dynamic_window(self):
        timestamps = iter(
            (
                "2026-07-20T00:00:00+00:00",
                "2026-07-20T00:00:01+00:00",
                "2026-07-20T00:00:02+00:00",
                "2026-07-20T00:00:03+00:00",
            )
        )
        with (
            patch.object(c, "_now_iso", side_effect=lambda: next(timestamps)),
            patch.object(
                c,
                "collect_block_devices",
                return_value=(_provider("block_devices"), []),
            ),
            patch.object(c, "collect_iostat", return_value=_provider("iostat")),
            patch.object(c, "collect_pidstat", return_value=_provider("pidstat")),
            patch.object(c, "collect_nfs", return_value=_provider("nfs")),
            patch.object(
                c,
                "collect_process_io_map",
                return_value=_provider("process_io_map"),
            ),
            patch.object(
                c, "collect_mounts", return_value=_provider("mounts", parsed=[])
            ),
            patch.object(c, "collect_df", return_value=_provider("df")),
            patch.object(c, "collect_memory", return_value=_provider("memory")),
            patch.object(c, "collect_readahead_scheduler", return_value=({}, {}, [])),
        ):
            snapshot = c.collect(1, None, None)

        self.assertEqual(snapshot.window["start"], "2026-07-20T00:00:00+00:00")
        self.assertEqual(snapshot.window["end"], "2026-07-20T00:00:01+00:00")

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
