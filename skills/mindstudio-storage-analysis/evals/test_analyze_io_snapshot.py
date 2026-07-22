#!/usr/bin/env python3
"""Deterministic unit tests for analyzer contracts and R500 safeguards."""

from __future__ import annotations

import copy
import json
import random
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = SKILL_ROOT / "evals" / "fixtures"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import analyze_io_snapshot as a  # noqa: E402


def _fixture(name: str) -> tuple[dict, dict | None]:
    with (FIXTURES / name).open(encoding="utf-8") as stream:
        snapshot = json.load(stream)
    return snapshot, snapshot.pop("_profile", None)


def _finding(result: dict, rule_id: str) -> dict:
    return next(item for item in result["findings"] if item["rule_id"] == rule_id)


class TestAnalyzerContracts(unittest.TestCase):
    def test_r100_without_provider_window_is_capped_at_medium(self):
        snapshot, _ = _fixture("rc-r100-missing-window.json")
        finding = _finding(a.analyze_all(snapshot), "R100")
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertFalse(finding["evidence_window_valid"])
        self.assertIn("时间窗", finding["summary"])

    def test_r500_verified_overlap_reaches_high(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "high")
        self.assertEqual(finding["severity"], "high")
        self.assertIn("传导链成立", finding["summary"])

    def test_r500_overlap_without_profile_window_is_rejected(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        profile = copy.deepcopy(profile)
        profile.pop("profile_window")
        result = a.analyze_all(snapshot, profile)
        finding = _finding(result, "R500")
        self.assertEqual(finding["confidence"], "none")
        self.assertNotIn("传导链成立", finding["summary"])
        self.assertTrue(
            any(
                "profile_window" in error
                for error in result["profile_validation_errors"]
            )
        )

    def test_r500_overlap_with_stale_profile_window_is_rejected(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        profile = copy.deepcopy(profile)
        profile["profile_window"] = {
            "start": "2026-07-01T11:00:00+08:00",
            "end": "2026-07-01T11:00:10+08:00",
        }
        result = a.analyze_all(snapshot, profile)
        finding = _finding(result, "R500")
        self.assertEqual(finding["confidence"], "none")
        self.assertTrue(
            any(
                "profile_window" in error
                for error in result["profile_validation_errors"]
            )
        )

    def test_stale_profile_drops_all_dynamic_metrics(self):
        snapshot, profile = _fixture("rc-r500-mte2-handoff.json")
        profile = copy.deepcopy(profile)
        profile["profile_window"] = {
            "start": "2026-07-01T11:00:00+08:00",
            "end": "2026-07-01T11:00:10+08:00",
        }
        normalized_snapshot, normalized_profile, _, errors, fatal = (
            a.validate_analysis_request(snapshot, profile)
        )
        self.assertIsNone(fatal)
        self.assertTrue(normalized_snapshot)
        self.assertNotIn("mte2_ratio", normalized_profile)
        self.assertTrue(any("重叠不足 50%" in error for error in errors))

    def test_direct_r500_call_cannot_bypass_profile_window_check(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        profile = copy.deepcopy(profile)
        profile.pop("profile_window")
        host = a.analyze_r100(snapshot)
        finding = a.analyze_r500_with_host(snapshot, profile, [host])
        self.assertEqual(finding["confidence"], "medium")
        self.assertNotIn("传导链成立", finding["summary"])

    def test_r400_high_carries_explicit_window_provenance(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        finding = _finding(a.analyze_all(snapshot), "R400")
        self.assertEqual(finding["confidence"], "high")
        self.assertTrue(finding["evidence_window_valid"])

    def test_r500_controlled_experiment_reaches_high(self):
        snapshot, profile = _fixture("rc-r500-conduction.json")
        profile = copy.deepcopy(profile)
        profile["conduction_evidence"] = {
            "controlled_experiment": {"result": "improved"}
        }
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "high")
        self.assertIn("对照实验已改善", finding["summary"])

    def test_r500_string_true_does_not_upgrade(self):
        snapshot, profile = _fixture("rc-r500-invalid-conduction.json")
        result = a.analyze_all(snapshot, profile)
        finding = _finding(result, "R500")
        self.assertEqual(finding["confidence"], "medium")
        self.assertNotIn("传导链成立", finding["summary"])
        self.assertTrue(
            any(
                "io_npu_overlap_observed" in error
                for error in result["profile_validation_errors"]
            )
        )

    def test_invalid_controlled_experiment_does_not_upgrade(self):
        snapshot, profile = _fixture("rc-r500-conduction.json")
        profile = copy.deepcopy(profile)
        profile["conduction_evidence"] = {"controlled_experiment": {"result": "yes"}}
        result = a.analyze_all(snapshot, profile)
        finding = _finding(result, "R500")
        self.assertEqual(finding["confidence"], "medium")
        self.assertTrue(
            any(
                "controlled_experiment.result" in error
                for error in result["profile_validation_errors"]
            )
        )

    def test_unknown_schema_major_is_fatal(self):
        result = a.analyze_all({"schema_version": "9.0"})
        self.assertIn("error", result)
        self.assertEqual(result["findings"], [])

    def test_schema_version_requires_exact_major_minor_format(self):
        for version in ("1", "1.2.3", "1.x"):
            with self.subTest(version=version):
                result = a.analyze_all({"schema_version": version})
                self.assertIn("error", result)

    def test_mount_collection_failure_is_not_treated_as_local_storage(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["mounts_provider"] = {
            "source": "mounts",
            "status": "permission_denied",
            "error": "denied",
        }
        finding = _finding(a.analyze_all(snapshot), "R200")
        self.assertEqual(finding["confidence"], "none")
        self.assertFalse(finding["performance_window_evaluated"])
        self.assertIn("无法判断", finding["summary"])

    def test_mount_provider_status_must_match_mount_list(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        snapshot["mounts_provider"] = {
            "source": "mounts",
            "status": "empty",
            "parsed": [],
        }
        normalized, errors = a.normalize_and_validate(snapshot)
        self.assertEqual(normalized["mounts_provider"]["status"], "parse_failed")
        self.assertTrue(any("conflicts" in item for item in errors))

    def test_mount_provider_without_current_window_cannot_rule_out_nfs(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["mounts_provider"].pop("started_at")
        snapshot["mounts_provider"].pop("ended_at")
        finding = _finding(a.analyze_all(snapshot), "R200")
        self.assertEqual(finding["confidence"], "none")
        self.assertFalse(finding["performance_window_evaluated"])

    def test_legacy_mount_error_survives_availability_rebuild(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot.pop("mounts_provider")
        snapshot["availability"] = {
            "missing": [],
            "partial": [],
            "errors": ["mounts: permission_denied"],
        }
        normalized, errors = a.normalize_and_validate(snapshot)
        self.assertEqual(normalized["mounts_provider"]["status"], "permission_denied")
        self.assertIn("mounts_provider: permission_denied", errors)

    def test_stale_provider_without_top_window_is_capped(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot.pop("window")
        result = a.analyze_all(snapshot)
        finding = _finding(result, "R100")
        self.assertEqual(finding["confidence"], "medium")
        self.assertFalse(finding["evidence_window_valid"])
        self.assertTrue(
            any("window.start/end" in item for item in result["validation_errors"])
        )

    def test_provider_outside_window_cannot_claim_high(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        snapshot["nfs"]["started_at"] = "2026-07-01T11:59:58.100000+08:00"
        snapshot["nfs"]["ended_at"] = "2026-07-01T11:59:59.100000+08:00"
        result = a.analyze_all(snapshot)
        finding = _finding(result, "R200")
        self.assertEqual(finding["confidence"], "medium")
        self.assertFalse(finding["evidence_window_valid"])
        self.assertTrue(
            any(
                "nfs: invalid or outside" in item
                for item in result["validation_errors"]
            )
        )

    def test_window_must_be_anchored_to_collected_at(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["collected_at"] = "2026-07-02T12:00:00+08:00"
        finding = _finding(a.analyze_all(snapshot), "R100")
        self.assertEqual(finding["confidence"], "medium")
        self.assertFalse(finding["evidence_window_valid"])

    def test_out_of_range_util_invalidates_iostat(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]["util_percent"] = 1000
        normalized, errors = a.normalize_and_validate(snapshot)
        self.assertEqual(normalized["iostat"]["status"], "parse_failed")
        self.assertTrue(
            any("iostat" in item and "invalid metric" in item for item in errors)
        )

    def test_huge_json_integer_is_rejected_without_crashing(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]["r_per_s"] = 10**400
        normalized, errors = a.normalize_and_validate(snapshot)
        self.assertEqual(normalized["iostat"]["status"], "parse_failed")
        self.assertTrue(any("invalid metric" in item for item in errors))
        a.analyze_all(snapshot)

    def test_non_object_iostat_disks_invalidates_provider(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["iostat"]["parsed"]["disks"] = "not-an-object"
        normalized, errors = a.normalize_and_validate(snapshot)
        self.assertEqual(normalized["iostat"]["status"], "parse_failed")
        self.assertTrue(any("disks not object" in item for item in errors))

    def test_r300_cumulative_sums_are_converted_to_per_op_averages(self):
        snapshot = {
            "schema_version": "1.4",
            "collected_at": "2026-07-01T12:00:00+08:00",
            "window": {
                "start": "2026-07-01T12:00:00+08:00",
                "end": "2026-07-01T12:00:30+08:00",
            },
            "mounts": [{"fstype": "nfs4", "device": "h:/d", "mount_point": "/data"}],
            "mounts_provider": {
                "source": "mounts",
                "status": "ok",
                "started_at": "2026-07-01T12:00:00+08:00",
                "ended_at": "2026-07-01T12:00:01+08:00",
            },
            "nfs": {
                "source": "nfs",
                "status": "ok",
                "started_at": "2026-07-01T12:00:00+08:00",
                "ended_at": "2026-07-01T12:00:30+08:00",
                "parsed": {
                    "mount_metrics": [
                        {
                            "mount_point": "/data",
                            "source": "h:/d",
                            "fstype": "nfs4",
                            "windowing": "delta",
                            "metadata_ops": 100,
                            "metadata_sum_rtt_ms": 500,
                            "metadata_sum_execute_ms": 1000,
                        }
                    ]
                },
            },
        }
        fast = _finding(a.analyze_all(snapshot), "R300")
        self.assertFalse(fast.get("metadata_slow_mounts"))

        snapshot["nfs"]["parsed"]["mount_metrics"][0].update(
            metadata_sum_rtt_ms=1500,
            metadata_sum_execute_ms=2500,
        )
        slow = _finding(a.analyze_all(snapshot), "R300")
        self.assertEqual(slow["confidence"], "high")
        self.assertEqual(slow["metadata_slow_mounts"][0]["avg_metadata_rtt_ms"], 15)

    def test_unresolved_target_pid_cannot_inherit_hostwide_r100(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        snapshot["target"] = {"pid": 999999, "path": None}
        snapshot["process_io_map"] = {
            "source": "process_io_map",
            "status": "missing",
        }
        result = a.analyze_all(snapshot, profile)
        self.assertEqual(_finding(result, "R100")["confidence"], "high")
        r500 = _finding(result, "R500")
        self.assertEqual(r500["confidence"], "none")
        self.assertNotIn("传导链成立", r500["summary"])

    def test_stale_target_mapping_cannot_scope_current_host_evidence(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        snapshot["target"] = {"pid": 42, "path": "/data"}
        snapshot["process_io_map"] = {
            "source": "process_io_map",
            "status": "ok",
            "started_at": "2000-01-01T00:00:00+00:00",
            "ended_at": "2000-01-01T00:00:30+00:00",
            "parsed": {
                "mappings": [
                    {
                        "pid": 42,
                        "path": "/data/shard.bin",
                        "path_relevant": True,
                        "canonical_device": "sda",
                        "backing_devices": [],
                        "device_resolution": "sysfs",
                    }
                ]
            },
        }
        r500 = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(r500["confidence"], "none")

    def test_explicit_target_path_ignores_unrelated_fd_mapping(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        nfs_mount = {
            "device": "server:/train",
            "mount_point": "/nfs/train",
            "fstype": "nfs4",
            "options": "rw",
        }
        snapshot["target"] = {"pid": 42, "path": "/nfs/train"}
        snapshot["mounts"] = [nfs_mount]
        snapshot["mounts_provider"] = {
            "source": "mounts",
            "status": "ok",
            "started_at": "2026-07-01T12:00:00+08:00",
            "ended_at": "2026-07-01T12:00:01+08:00",
            "parsed": [nfs_mount],
        }
        snapshot["process_io_map"] = {
            "source": "process_io_map",
            "status": "ok",
            "started_at": "2026-07-01T12:00:00+08:00",
            "ended_at": "2026-07-01T12:00:30+08:00",
            "parsed": {
                "mappings": [
                    {
                        "pid": 42,
                        "path": "/cache/unrelated.bin",
                        "path_relevant": True,
                        "source": "/dev/sda",
                        "fstype": "ext4",
                        "canonical_device": "sda",
                        "backing_devices": [],
                        "device_resolution": "sysfs",
                    }
                ]
            },
        }
        result = a.analyze_all(snapshot, profile)
        self.assertEqual(_finding(result, "R100")["confidence"], "high")
        self.assertEqual(
            _finding(result, "R200")["nfs_metric_required_scope"], "target_path"
        )
        r500 = _finding(result, "R500")
        self.assertEqual(r500["confidence"], "none")
        self.assertNotIn("传导链成立", r500["summary"])

    def test_r400_candidates_exclude_unrelated_paths_with_explicit_target(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        snapshot["target"] = {"pid": None, "path": "/data/train"}
        mappings = snapshot["process_io_map"]["parsed"]["mappings"]
        for mapping in mappings:
            mapping["path"] = f"/workspace/logs/{mapping['pid']}.log"
            mapping["path_relevant"] = True
        finding = _finding(a.analyze_all(snapshot), "R400")
        self.assertEqual(finding["confidence"], "low")
        self.assertEqual(finding["severity"], "info")
        self.assertNotIn("candidate_conflicts", finding)
        self.assertIn("未发现多个进程访问同一设备", finding["summary"])

    def test_analyze_all_does_not_mutate_input(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        original_snapshot = copy.deepcopy(snapshot)
        original_profile = copy.deepcopy(profile)
        a.analyze_all(snapshot, profile)
        self.assertEqual(snapshot, original_snapshot)
        self.assertEqual(profile, original_profile)

    def test_seeded_malformed_inputs_never_raise(self):
        rng = random.Random(20260720)
        scalars = [
            None,
            True,
            False,
            0,
            1,
            -1,
            0.5,
            float("nan"),
            float("inf"),
            10**400,
            "",
            "x",
            "1",
            "2026-07-20T00:00:00+00:00",
        ]
        keys = [
            "schema_version",
            "collected_at",
            "window",
            "target",
            "mounts",
            "mounts_provider",
            "diskstats_sample",
            "iostat",
            "pidstat",
            "nfs",
            "df",
            "process_io_map",
            "memory",
            "block_devices",
            "availability",
            "status",
            "parsed",
            "disks",
            "filesystems",
            "processes",
            "mappings",
            "timestamp",
            "started_at",
            "ended_at",
        ]

        def value(depth: int = 0):
            if depth > 3 or rng.random() < 0.45:
                return rng.choice(scalars)
            if rng.random() < 0.5:
                return [value(depth + 1) for _ in range(rng.randrange(4))]
            return {rng.choice(keys): value(depth + 1) for _ in range(rng.randrange(5))}

        for _ in range(1000):
            a.analyze_all(value(), value())


if __name__ == "__main__":
    unittest.main()
