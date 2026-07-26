#!/usr/bin/env python3
"""Unit tests for the read-only live environment runner."""

from __future__ import annotations

import json
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_live_eval as live


class TestLiveEvalHelpers(unittest.TestCase):
    @staticmethod
    def _snapshot(
        *,
        target_path: str = "/local/model",
        nfs_metrics: list[dict] | None = None,
    ) -> dict:
        started_at = "2026-07-20T00:00:00+00:00"
        ended_at = "2026-07-20T00:00:10+00:00"
        providers = {
            name: {
                "status": "ok",
                "started_at": started_at,
                "ended_at": ended_at,
                "parsed": {},
            }
            for name in (
                "block_devices",
                "iostat",
                "pidstat",
                "process_io_map",
                "memory",
                "df",
            )
        }
        mounts = [
            {
                "device": "/dev/sda1",
                "mount_point": "/",
                "fstype": "ext4",
            },
            {
                "device": "nfs.example:/dataset",
                "mount_point": "/mnt/dataset",
                "fstype": "nfs4",
            },
        ]
        return {
            "schema_version": "1.4",
            "collected_at": started_at,
            "duration_seconds": 10,
            "window": {"start": started_at, "end": ended_at},
            "target": {"path": target_path},
            "mounts": mounts,
            "mounts_provider": {
                "status": "ok",
                "started_at": started_at,
                "ended_at": ended_at,
                "parsed": mounts,
            },
            "nfs": {
                "status": "ok",
                "started_at": started_at,
                "ended_at": ended_at,
                "parsed": {"mount_metrics": nfs_metrics or []},
            },
            **providers,
        }

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

    def test_profile_rejects_excessive_json_nesting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "profile.json"
            profile.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
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
            "profile_host_overlap_rules": ["R100"],
            "certified_profile_metrics": ["device_free_percent"],
            "certified_conduction_evidence": ["timeline_overlap"],
            "evidence_fields": [
                "profile.device_free_percent",
                "profile.conduction_evidence",
                "profile.conduction_evidence.overlap_provenance/controlled_experiment",
                "profile.profile_window.scope",
                "profile.provenance.device_free_percent",
            ],
        }
        self.assertFalse(live._r500_is_certified(confirmed))
        self.assertFalse(
            live._r500_is_certified({**confirmed, "profile_host_overlap_rules": []})
        )
        self.assertFalse(
            live._r500_is_certified(
                {**confirmed, "evidence_fields": ["profile.conduction_evidence"]}
            )
        )
        self.assertFalse(
            live._r500_is_certified({**confirmed, "certified_profile_metrics": []})
        )
        self.assertFalse(
            live._r500_is_certified({**confirmed, "certified_conduction_evidence": []})
        )

    def test_nfs_live_check_rejects_activity_from_unrelated_mount(self):
        snapshot = self._snapshot(
            target_path="/local/model",
            nfs_metrics=[
                {
                    "source": "nfs.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs4",
                    "windowing": "delta",
                    "ops": 1000,
                }
            ],
        )

        check = live._target_nfs_live_check(snapshot, required=True)

        self.assertEqual(check.status, "FAIL")
        self.assertIn("non-NFS mount", check.detail)

    def test_nfs_live_check_respects_deeper_local_overmount(self):
        snapshot = self._snapshot(
            target_path="/mnt/dataset/cache/shard.bin",
            nfs_metrics=[
                {
                    "source": "nfs.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs4",
                    "windowing": "delta",
                    "ops": 1000,
                }
            ],
        )
        snapshot["mounts"].append(
            {
                "device": "/dev/sdb1",
                "mount_point": "/mnt/dataset/cache",
                "fstype": "ext4",
            }
        )
        snapshot["mounts_provider"]["parsed"] = snapshot["mounts"]

        check = live._target_nfs_live_check(snapshot, required=True)

        self.assertEqual(check.status, "FAIL")
        self.assertIn("non-NFS mount", check.detail)

    def test_nfs_live_check_counts_only_identity_matched_target_metrics(self):
        snapshot = self._snapshot(
            target_path="/mnt/dataset/train/shard.bin",
            nfs_metrics=[
                {
                    "source": "other.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs4",
                    "windowing": "delta",
                    "ops": 1000,
                },
                {
                    "source": "nfs.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs",
                    "windowing": "delta",
                    "ops": 7,
                },
            ],
        )

        check = live._target_nfs_live_check(snapshot, required=True)

        self.assertEqual(check.status, "PASS")
        self.assertIn("7 operation(s)", check.detail)

    def test_nfs_live_check_rejects_stale_provider_window(self):
        snapshot = self._snapshot(
            target_path="/mnt/dataset/train/shard.bin",
            nfs_metrics=[
                {
                    "source": "nfs.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs4",
                    "windowing": "delta",
                    "ops": 7,
                }
            ],
        )
        snapshot["nfs"]["started_at"] = "2026-07-19T23:00:00+00:00"
        snapshot["nfs"]["ended_at"] = "2026-07-19T23:00:10+00:00"

        check = live._target_nfs_live_check(snapshot, required=True)

        self.assertEqual(check.status, "FAIL")
        self.assertIn("provider window", check.detail)

    def test_nfs_live_check_rejects_disjoint_provider_windows(self):
        snapshot = self._snapshot(
            target_path="/mnt/dataset/train/shard.bin",
            nfs_metrics=[
                {
                    "source": "nfs.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs4",
                    "windowing": "delta",
                    "ops": 7,
                }
            ],
        )
        snapshot["duration_seconds"] = 120
        snapshot["window"]["end"] = "2026-07-20T00:02:00+00:00"
        snapshot["mounts_provider"]["started_at"] = "2026-07-20T00:00:00+00:00"
        snapshot["mounts_provider"]["ended_at"] = "2026-07-20T00:00:01+00:00"
        snapshot["nfs"]["started_at"] = "2026-07-20T00:01:59+00:00"
        snapshot["nfs"]["ended_at"] = "2026-07-20T00:02:00+00:00"

        check = live._target_nfs_live_check(snapshot, required=True)

        self.assertEqual(check.status, "FAIL")
        self.assertIn("do not overlap", check.detail)

    def test_nfs_live_check_accepts_adjacent_provider_windows(self):
        snapshot = self._snapshot(
            target_path="/mnt/dataset/train/shard.bin",
            nfs_metrics=[
                {
                    "source": "nfs.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs4",
                    "windowing": "delta",
                    "ops": 7,
                }
            ],
        )
        snapshot["nfs"]["ended_at"] = "2026-07-20T00:00:08+00:00"
        snapshot["mounts_provider"]["started_at"] = "2026-07-20T00:00:09+00:00"

        check = live._target_nfs_live_check(snapshot, required=True)

        self.assertEqual(check.status, "PASS")

    def test_nfs_live_check_rejects_non_contract_fstype(self):
        snapshot = self._snapshot(
            target_path="/mnt/dataset/train/shard.bin",
            nfs_metrics=[
                {
                    "source": "nfs.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs4",
                    "windowing": "delta",
                    "ops": 7,
                }
            ],
        )
        snapshot["mounts"][1]["fstype"] = "nfsbogus"

        check = live._target_nfs_live_check(snapshot, required=True)

        self.assertEqual(check.status, "FAIL")
        self.assertIn("non-NFS mount", check.detail)

    def test_nfs_live_check_rejects_boolean_ops(self):
        snapshot = self._snapshot(
            target_path="/mnt/dataset/train/shard.bin",
            nfs_metrics=[
                {
                    "source": "nfs.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs4",
                    "windowing": "delta",
                    "ops": True,
                }
            ],
        )

        check = live._target_nfs_live_check(snapshot, required=True)

        self.assertEqual(check.status, "FAIL")
        self.assertIn("invalid ops", check.detail)

    def test_provider_checks_reject_unhashable_status_without_crashing(self):
        checks = []
        live._provider_checks(
            {"mounts_provider": {"status": ["ok"]}},
            checks,
        )
        mounts_check = next(
            check for check in checks if check.id == "provider-mounts_provider"
        )
        self.assertEqual(mounts_check.status, "FAIL")

    def test_run_rejects_unrelated_nfs_activity_end_to_end(self):
        snapshot = self._snapshot(
            nfs_metrics=[
                {
                    "source": "nfs.example:/dataset",
                    "mount_point": "/mnt/dataset",
                    "fstype": "nfs4",
                    "windowing": "delta",
                    "ops": 1000,
                }
            ]
        )
        commands = []

        def fake_run(command, timeout):
            del timeout
            commands.append(command)
            if str(live.COLLECTOR) in command:
                output = Path(command[command.index("--out") + 1])
                output.write_text(json.dumps(snapshot), encoding="utf-8")
            elif str(live.ANALYZER) in command:
                output = Path(command[command.index("--output") + 1])
                output.write_text(json.dumps({"findings": []}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        args = SimpleNamespace(
            duration=1,
            path=Path("/local/model"),
            pid=4242,
            snapshot=None,
            profile=None,
            require_npu=False,
            require_npu_runtime=False,
            require_nfs=True,
            require_r500_high=False,
        )
        with (
            patch.object(live.platform, "system", return_value="Linux"),
            patch.object(live.os, "access", return_value=True),
            patch.object(
                live,
                "_npu_hardware_check",
                return_value=(live.Check("ascend-hardware", "PASS", "ok"), 1),
            ),
            patch.object(
                live,
                "_npu_runtime_check",
                return_value=live.Check("ascend-runtime", "PASS", "ok"),
            ),
            patch.object(live, "_run", side_effect=fake_run),
        ):
            rc, report = live.run(args)

        nfs_check = next(
            check for check in report["checks"] if check["id"] == "nfs-live-window"
        )
        self.assertEqual(rc, 1)
        self.assertEqual(nfs_check["status"], "FAIL")
        collector_command = next(
            command for command in commands if str(live.COLLECTOR) in command
        )
        self.assertIn("--pid", collector_command)
        self.assertEqual(
            collector_command[collector_command.index("--pid") + 1], "4242"
        )

    def test_run_uses_supplied_snapshot_for_profile_analysis(self):
        snapshot = self._snapshot()
        commands = []
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.json"
            profile_path = Path(temp_dir) / "profile.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            profile_path.write_text(
                json.dumps(
                    {
                        "device_free_percent": 20,
                        "profile_window": {
                            "start": "2026-07-20T00:00:00+00:00",
                            "end": "2026-07-20T00:00:01+00:00",
                            "scope": "matched_workload_device_timeline",
                        },
                        "provenance": {
                            "device_free_percent": {
                                "source_type": "profiler_timeline",
                                "artifact_id": "fixture://live-eval/device-0-timeline",
                                "device_id": 0,
                                "metric": "device_free_percent",
                                "extraction_method": "device_idle_interval_ratio",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            def fake_run(command, timeout):
                del timeout
                commands.append(command)
                self.assertIn(str(live.ANALYZER), command)
                self.assertEqual(command[2], str(snapshot_path))
                self.assertIn("--profile", command)
                self.assertEqual(
                    command[command.index("--profile") + 1], str(profile_path)
                )
                output = Path(command[command.index("--output") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "rule_id": "R500",
                                    "confidence": "high",
                                    "severity": "high",
                                    "profile_host_overlap_rules": ["R100"],
                                    "certified_profile_metrics": [
                                        "device_free_percent"
                                    ],
                                    "certified_conduction_evidence": [
                                        "timeline_overlap"
                                    ],
                                    "evidence_fields": [
                                        "profile.device_free_percent",
                                        "profile.conduction_evidence",
                                        "profile.conduction_evidence.overlap_provenance/controlled_experiment",
                                        "profile.profile_window.scope",
                                        "profile.provenance.device_free_percent",
                                    ],
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            args = SimpleNamespace(
                duration=1,
                path=None,
                snapshot=snapshot_path,
                profile=profile_path,
                require_npu=False,
                require_npu_runtime=False,
                require_nfs=False,
                require_r500_high=True,
            )
            with (
                patch.object(live.platform, "system", return_value="Linux"),
                patch.object(live.os, "access", return_value=True),
                patch.object(
                    live,
                    "_npu_hardware_check",
                    return_value=(
                        live.Check("ascend-hardware", "PASS", "ok"),
                        1,
                    ),
                ),
                patch.object(
                    live,
                    "_npu_runtime_check",
                    return_value=live.Check("ascend-runtime", "PASS", "ok"),
                ),
                patch.object(live, "_run", side_effect=fake_run),
            ):
                rc, report = live.run(args)

        self.assertEqual(rc, 1)
        self.assertEqual(len(commands), 1)
        self.assertFalse(any(str(live.COLLECTOR) in command for command in commands))
        r500_check = next(
            check for check in report["checks"] if check["id"] == "r500-profile"
        )
        self.assertEqual(r500_check["status"], "PASS")
        disabled = next(
            check
            for check in report["checks"]
            if check["id"] == "r500-high-certification"
        )
        self.assertEqual(disabled["status"], "FAIL")

    def test_run_real_analyzer_rejects_stale_profile_window(self):
        snapshot = self._snapshot()
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.json"
            profile_path = Path(temp_dir) / "profile.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            profile_path.write_text(
                json.dumps(
                    {
                        "device_free_percent": 25,
                        "profile_window": {
                            "start": "2026-07-19T00:00:00+00:00",
                            "end": "2026-07-19T00:00:10+00:00",
                            "scope": "matched_workload_device_timeline",
                        },
                        "provenance": {
                            "device_free_percent": {
                                "source_type": "profiler_timeline",
                                "artifact_id": "fixture://stale/device-0-timeline",
                                "device_id": 0,
                                "metric": "device_free_percent",
                                "extraction_method": "device_idle_interval_ratio",
                            }
                        },
                        "conduction_evidence": {"io_npu_overlap_observed": True},
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                duration=1,
                path=None,
                snapshot=snapshot_path,
                profile=profile_path,
                require_npu=False,
                require_npu_runtime=False,
                require_nfs=False,
                require_r500_high=True,
            )
            with (
                patch.object(live.platform, "system", return_value="Linux"),
                patch.object(live.os, "access", return_value=True),
                patch.object(
                    live,
                    "_npu_hardware_check",
                    return_value=(
                        live.Check("ascend-hardware", "PASS", "ok"),
                        1,
                    ),
                ),
                patch.object(
                    live,
                    "_npu_runtime_check",
                    return_value=live.Check("ascend-runtime", "PASS", "ok"),
                ),
            ):
                rc, report = live.run(args)

        r500_check = next(
            check for check in report["checks"] if check["id"] == "r500-profile"
        )
        self.assertEqual(rc, 1)
        self.assertEqual(r500_check["status"], "FAIL")
        disabled = next(
            check
            for check in report["checks"]
            if check["id"] == "r500-high-certification"
        )
        self.assertEqual(disabled["status"], "FAIL")
        self.assertIn("重叠不足", r500_check["detail"])

    def test_run_real_analyzer_requires_conduction_for_r500_high(self):
        snapshot = self._snapshot()
        snapshot["target"] = {"pid": None, "path": None}
        snapshot["mounts"] = snapshot["mounts"][:1]
        snapshot["mounts_provider"]["parsed"] = snapshot["mounts"]
        snapshot["nfs"] = {"status": "unsupported", "parsed": {}}
        snapshot["iostat"]["parsed"] = {
            "disks": {
                "sda": {
                    "r_per_s": 100,
                    "rkB_per_s": 5000,
                    "r_await_ms": 30,
                    "avgqu_sz": 4,
                    "util_percent": 99,
                    "util_max": 99,
                    "util_p95": 99,
                    "sample_count": 5,
                    "util_sample_count": 5,
                    "avgqu_sz_sample_count": 5,
                    "r_await_ms_sample_count": 5,
                    "avgqu_sz_with_util_sample_count": 5,
                    "r_await_ms_with_util_sample_count": 5,
                    "device_type": "ssd",
                }
            },
            "reports": 5,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.json"
            profile_path = Path(temp_dir) / "profile.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            profile_path.write_text(
                json.dumps(
                    {
                        "device_free_percent": 25,
                        "profile_window": {
                            "start": "2026-07-20T00:00:02+00:00",
                            "end": "2026-07-20T00:00:08+00:00",
                            "scope": "matched_workload_device_timeline",
                        },
                        "provenance": {
                            "device_free_percent": {
                                "source_type": "profiler_timeline",
                                "artifact_id": "fixture://no-conduction/device-0-timeline",
                                "device_id": 0,
                                "metric": "device_free_percent",
                                "extraction_method": "device_idle_interval_ratio",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                duration=1,
                path=None,
                snapshot=snapshot_path,
                profile=profile_path,
                require_npu=False,
                require_npu_runtime=False,
                require_nfs=False,
                require_r500_high=True,
            )
            with (
                patch.object(live.platform, "system", return_value="Linux"),
                patch.object(live.os, "access", return_value=True),
                patch.object(
                    live,
                    "_npu_hardware_check",
                    return_value=(
                        live.Check("ascend-hardware", "PASS", "ok"),
                        1,
                    ),
                ),
                patch.object(
                    live,
                    "_npu_runtime_check",
                    return_value=live.Check("ascend-runtime", "PASS", "ok"),
                ),
            ):
                rc, report = live.run(args)

        r500_check = next(
            check for check in report["checks"] if check["id"] == "r500-profile"
        )
        self.assertEqual(rc, 1)
        self.assertEqual(r500_check["status"], "PASS")
        disabled = next(
            check
            for check in report["checks"]
            if check["id"] == "r500-high-certification"
        )
        self.assertEqual(disabled["status"], "FAIL")
        self.assertIn("confidence=medium", r500_check["detail"])
        self.assertIn("certified=False", r500_check["detail"])

    def test_run_rejects_non_object_supplied_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.json"
            snapshot_path.write_text("[]", encoding="utf-8")
            args = SimpleNamespace(
                duration=1,
                path=None,
                snapshot=snapshot_path,
                profile=None,
                require_npu=False,
                require_npu_runtime=False,
                require_nfs=False,
                require_r500_high=False,
            )
            with (
                patch.object(live.platform, "system", return_value="Linux"),
                patch.object(live.os, "access", return_value=True),
                patch.object(
                    live,
                    "_npu_hardware_check",
                    return_value=(
                        live.Check("ascend-hardware", "PASS", "ok"),
                        1,
                    ),
                ),
                patch.object(
                    live,
                    "_npu_runtime_check",
                    return_value=live.Check("ascend-runtime", "PASS", "ok"),
                ),
                patch.object(live, "_run") as runner,
            ):
                rc, report = live.run(args)

        runner.assert_not_called()
        self.assertEqual(rc, 1)
        snapshot_check = next(
            check for check in report["checks"] if check["id"] == "snapshot-json"
        )
        self.assertEqual(snapshot_check["status"], "FAIL")

    def test_run_rejects_excessive_snapshot_nesting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.json"
            snapshot_path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
            args = SimpleNamespace(
                duration=1,
                path=None,
                snapshot=snapshot_path,
                profile=None,
                require_npu=False,
                require_npu_runtime=False,
                require_nfs=False,
                require_r500_high=False,
            )
            with (
                patch.object(live.platform, "system", return_value="Linux"),
                patch.object(live.os, "access", return_value=True),
                patch.object(
                    live,
                    "_npu_hardware_check",
                    return_value=(
                        live.Check("ascend-hardware", "PASS", "ok"),
                        1,
                    ),
                ),
                patch.object(
                    live,
                    "_npu_runtime_check",
                    return_value=live.Check("ascend-runtime", "PASS", "ok"),
                ),
                patch.object(live, "_run") as runner,
            ):
                rc, report = live.run(args)

        runner.assert_not_called()
        self.assertEqual(rc, 1)
        snapshot_check = next(
            check for check in report["checks"] if check["id"] == "snapshot-json"
        )
        self.assertEqual(snapshot_check["status"], "FAIL")

    def test_run_fails_on_analyzer_validation_errors(self):
        snapshot = self._snapshot()
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            def fake_run(command, timeout):
                del timeout
                output = Path(command[command.index("--output") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "findings": [],
                            "validation_errors": ["mounts: malformed"],
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            args = SimpleNamespace(
                duration=1,
                path=None,
                snapshot=snapshot_path,
                profile=None,
                require_npu=False,
                require_npu_runtime=False,
                require_nfs=False,
                require_r500_high=False,
            )
            with (
                patch.object(live.platform, "system", return_value="Linux"),
                patch.object(live.os, "access", return_value=True),
                patch.object(
                    live,
                    "_npu_hardware_check",
                    return_value=(
                        live.Check("ascend-hardware", "PASS", "ok"),
                        1,
                    ),
                ),
                patch.object(
                    live,
                    "_npu_runtime_check",
                    return_value=live.Check("ascend-runtime", "PASS", "ok"),
                ),
                patch.object(live, "_run", side_effect=fake_run),
            ):
                rc, report = live.run(args)

        validation_check = next(
            check for check in report["checks"] if check["id"] == "snapshot-validation"
        )
        self.assertEqual(rc, 1)
        self.assertEqual(validation_check["status"], "FAIL")

    def test_main_requires_snapshot_for_profile(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            live.main(["--profile", "npu_metrics.json"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--profile requires --snapshot", stderr.getvalue())

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
