#!/usr/bin/env python3
"""Deterministic unit tests for analyzer contracts and R500 safeguards."""

from __future__ import annotations

import copy
import json
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
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


def _audited_experiment(profile: dict, target: dict | None = None) -> dict:
    provenance = profile["provenance"]["device_free_percent"]
    return {
        "result": "improved",
        "experiment_id": "fixture://controlled-experiment/cache-ab",
        "device_id": provenance["device_id"],
        "metric": "device_free_percent",
        "action": "read-only local-cache A/B comparison",
        "target": target or {"pid": None, "path": None},
        "baseline": {
            "artifact_id": provenance["artifact_id"],
            "window": copy.deepcopy(profile["profile_window"]),
            "device_free_percent": profile["device_free_percent"],
        },
        "treatment": {
            "artifact_id": "fixture://controlled-experiment/treatment-timeline",
            "window": {
                "start": "2026-07-01T12:01:00+08:00",
                "end": "2026-07-01T12:01:15+08:00",
            },
            "device_free_percent": 4,
        },
    }


def _diskstats_snapshot(duration: int, pressured: bool) -> dict:
    snapshot, _ = _fixture("rc-r100-bandwidth.json")
    snapshot["iostat"] = {"source": "iostat", "status": "missing"}
    start = a._parse_iso(snapshot["window"]["start"])
    if start is None:
        raise AssertionError("fixture window must be parseable")

    if pressured:
        reads = 200 * duration
        counters = {
            "reads_completed": reads,
            "writes_completed": 0,
            "sectors_read": 40000 * duration,
            "sectors_written": 0,
            "time_reading_ms": reads * 30,
            "time_writing_ms": 0,
            "time_io_ms": duration * 1000,
            "weighted_time_io_ms": duration * 4000,
        }
    else:
        reads = 10 * duration
        counters = {
            "reads_completed": reads,
            "writes_completed": 0,
            "sectors_read": 80 * duration,
            "sectors_written": 0,
            "time_reading_ms": reads,
            "time_writing_ms": 0,
            "time_io_ms": duration * 200,
            "weighted_time_io_ms": duration * 100,
        }

    zero = {key: 0 for key in counters}
    snapshot["diskstats_sample"] = [
        {"sample_index": 0, "timestamp": start, "disks": {"sda": zero}},
        {
            "sample_index": 1,
            "timestamp": start + duration,
            "disks": {"sda": counters},
        },
    ]
    return snapshot


class TestAnalyzerContracts(unittest.TestCase):
    def test_target_pid_ignores_unrelated_nfs_process_mapping(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        snapshot["target"] = {"pid": 99, "path": None}
        snapshot["process_io_map"] = {
            "source": "process_io_map",
            "status": "ok",
            "parsed": {
                "pid_tree": [{"pid": 99, "role": "root"}],
                "mappings": [
                    {
                        "pid": 42,
                        "path": "/data/shard.bin",
                        "source": "server:/data",
                        "mount_point": "/data",
                        "fstype": "nfs4",
                    }
                ],
            },
        }

        identities, scope = a._required_nfs_identities(snapshot, snapshot["mounts"])

        self.assertEqual(identities, set())
        self.assertEqual(scope, "target_process_io_map_unresolved")

    def test_target_pid_without_process_map_cannot_bind_unrelated_nfs(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        _, profile = _fixture("rc-r500-conduction-confirmed.json")
        snapshot["target"] = {"pid": 999, "path": None}
        snapshot["process_io_map"] = {
            "source": "process_io_map",
            "status": "missing",
        }

        result = a.analyze_all(snapshot, profile)
        r200 = _finding(result, "R200")
        r500 = _finding(result, "R500")

        self.assertEqual(r200["confidence"], "none")
        self.assertEqual(
            r200["nfs_metric_required_scope"],
            "target_process_io_map_unresolved",
        )
        self.assertNotIn("confirmed_mounts", r200)
        self.assertFalse(r500["target_binding_certified"])
        self.assertNotIn("profile_host_overlap_rules", r500)
        self.assertNotIn("传导链成立", r500["summary"])

    def test_target_pid_accepts_only_descendants_chained_to_root(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        identity = {
            "boot_id": "11111111-1111-1111-1111-111111111111",
            "pid_starttime_ticks": 4200,
        }
        snapshot["target"] = {"pid": 42, "path": None}
        snapshot["process_io_map"] = {
            "source": "process_io_map",
            "status": "ok",
            "started_at": "2026-07-01T12:00:00+08:00",
            "ended_at": "2026-07-01T12:00:30+08:00",
            "parsed": {
                "observation_samples": 2,
                "pid_tree": [
                    {"pid": 42, "role": "root", **identity},
                    {
                        "pid": 999,
                        "role": "descendant",
                        **identity,
                    },
                ],
                "mappings": [
                    {
                        "pid": 999,
                        "path": "/data/shard.bin",
                        "source": "h:/d",
                        "mount_point": "/data",
                        "fstype": "nfs4",
                        "observation_count": 2,
                        **identity,
                    }
                ],
            },
        }

        identities, scope = a._required_nfs_identities(snapshot, snapshot["mounts"])
        self.assertEqual(identities, set())
        self.assertEqual(scope, "target_process_io_map_unresolved")

        snapshot["process_io_map"]["parsed"]["pid_tree"][1]["parent_pid"] = 42
        identities, scope = a._required_nfs_identities(snapshot, snapshot["mounts"])
        self.assertEqual(
            identities,
            {("h:/d", "/data", "nfs")},
        )
        self.assertEqual(scope, "target_process_io_map")

    def test_r100_without_provider_window_is_capped_at_medium(self):
        snapshot, _ = _fixture("rc-r100-missing-window.json")
        finding = _finding(a.analyze_all(snapshot), "R100")
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertFalse(finding["evidence_window_valid"])
        self.assertIn("时间窗", finding["summary"])

    def test_invalid_device_baseline_cannot_manufacture_r100_high(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        disk = snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]
        disk["avgqu_sz"] = 0.2
        snapshot["device_baselines"] = {"nvme0n1": {"max_read_mbps": True}}

        result = a.analyze_all(snapshot)
        finding = _finding(result, "R100")

        self.assertEqual(finding["confidence"], "medium")
        self.assertFalse(finding["assessed_devices"][0]["baseline_backed"])
        self.assertTrue(
            any("device_baselines" in error for error in result["validation_errors"])
        )

    def test_empty_nfs_transmissions_is_normalized_without_crashing(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        metric = snapshot["nfs"]["parsed"]["mount_metrics"][0]
        metric["transmissions"] = ""

        normalized, errors = a.normalize_and_validate(snapshot)
        result = a.analyze_all(snapshot)

        self.assertEqual(errors, [])
        self.assertIsNone(
            normalized["nfs"]["parsed"]["mount_metrics"][0]["transmissions"]
        )
        self.assertTrue(result["findings"])

    def test_r500_self_declared_overlap_is_capped_at_medium(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["certified_profile_metrics"], ["device_free_percent"])
        self.assertEqual(finding["certified_conduction_evidence"], [])
        self.assertEqual(
            finding["unverified_conduction_evidence"], ["timeline_overlap"]
        )
        self.assertIn("已提供但未经可信工件核验", finding["summary"])
        self.assertTrue(
            any("artifact verifier" in item for item in finding["missing_evidence"])
        )
        self.assertFalse(
            any(
                "io_npu_overlap_observed" in item
                for item in finding["missing_evidence"]
            )
        )
        self.assertNotIn("传导链成立", finding["summary"])

    def test_r500_uncertified_scope_cannot_reach_high(self):
        for scope in (
            None,
            "arbitrary_scope",
            "between_first_and_last_exported_device_task",
            "op_summary_task_gap",
        ):
            with self.subTest(scope=scope):
                snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
                profile = copy.deepcopy(profile)
                if scope is None:
                    profile["profile_window"].pop("scope")
                else:
                    profile["profile_window"]["scope"] = scope

                result = a.analyze_all(snapshot, profile)
                finding = _finding(result, "R500")

                self.assertEqual(finding["confidence"], "medium")
                self.assertNotIn("传导链成立", finding["summary"])
                self.assertEqual(finding["certified_profile_metrics"], [])
                self.assertTrue(
                    any(
                        "profile_window.scope" in error
                        for error in result["profile_validation_errors"]
                    )
                )

    def test_r500_missing_or_op_summary_provenance_cannot_reach_high(self):
        for provenance in (
            None,
            {
                "device_free_percent": {
                    "source_type": "msprof_op_summary",
                    "artifact_id": "fixture://op-summary.csv",
                    "device_id": 0,
                    "metric": "device_free_percent",
                    "extraction_method": "task_gap_ratio",
                }
            },
        ):
            with self.subTest(provenance=provenance):
                snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
                profile = copy.deepcopy(profile)
                if provenance is None:
                    profile.pop("provenance")
                else:
                    profile["provenance"] = provenance

                result = a.analyze_all(snapshot, profile)
                finding = _finding(result, "R500")

                self.assertEqual(finding["confidence"], "medium")
                self.assertNotIn("传导链成立", finding["summary"])
                self.assertEqual(finding["certified_profile_metrics"], [])
                self.assertTrue(
                    any(
                        "provenance" in error
                        for error in result["profile_validation_errors"]
                    )
                )

    def test_r500_uncertified_negative_metric_cannot_downgrade_storage(self):
        snapshot, profile = _fixture("rc-r500-downgrade.json")
        profile = copy.deepcopy(profile)
        profile.pop("provenance")

        result = a.analyze_all(snapshot, profile)
        finding = _finding(result, "R500")

        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertFalse(finding.get("priority_downgrade"))
        self.assertEqual(finding["certified_profile_metrics"], [])

    def test_r500_target_mapping_requires_repeated_identity_bound_observation(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        snapshot["target"] = {"pid": 42, "path": "/data"}
        snapshot["process_io_map"] = {
            "source": "process_io_map",
            "status": "ok",
            "started_at": "2026-07-01T12:00:00+08:00",
            "ended_at": "2026-07-01T12:00:30+08:00",
            "parsed": {
                "observation_samples": 2,
                "pid_tree": [{"pid": 42, "role": "root"}],
                "mappings": [
                    {
                        "pid": 42,
                        "boot_id": "11111111-1111-1111-1111-111111111111",
                        "pid_starttime_ticks": 4200,
                        "source": "/dev/sda",
                        "canonical_device": "sda",
                        "device_resolution": "sysfs",
                        "path": "/data/shard.bin",
                        "first_seen": "2026-07-01T12:00:00+08:00",
                        "last_seen": "2026-07-01T12:00:30+08:00",
                        "observation_count": 1,
                    }
                ],
            },
        }
        profile = copy.deepcopy(profile)
        profile["conduction_evidence"]["overlap_provenance"]["target"] = {
            "pid": 42,
            "path": "/data",
        }

        result = a.analyze_all(snapshot, profile)
        finding = _finding(result, "R500")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertNotIn("传导链成立", finding["summary"])
        self.assertFalse(result.get("validation_errors"))

        snapshot["process_io_map"]["parsed"]["mappings"][0]["observation_count"] = 2
        strong = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(strong["confidence"], "medium")
        self.assertNotIn("传导链成立", strong["summary"])

    def test_r500_rejects_target_mapping_that_does_not_overlap_r100(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        mapping = snapshot["process_io_map"]["parsed"]["mappings"][0]
        mapping["last_seen"] = "2026-07-01T12:00:05+08:00"
        snapshot["iostat"]["started_at"] = "2026-07-01T12:00:10+08:00"
        profile["profile_window"]["start"] = "2026-07-01T12:00:15+08:00"
        profile["profile_window"]["end"] = "2026-07-01T12:00:20+08:00"
        provenance = profile["conduction_evidence"]["overlap_provenance"]
        provenance["host_evidence_interval"] = {
            "start": "2026-07-01T12:00:10+08:00",
            "end": "2026-07-01T12:00:30+08:00",
        }

        finding = _finding(a.analyze_all(snapshot, profile), "R500")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertNotIn("传导链成立", finding["summary"])

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

    def test_r500_profile_must_overlap_actual_host_evidence(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        snapshot["iostat"]["started_at"] = "2026-07-01T12:00:16+08:00"
        snapshot["iostat"]["ended_at"] = "2026-07-01T12:00:30+08:00"
        profile = copy.deepcopy(profile)
        profile["profile_window"] = {
            "start": "2026-07-01T12:00:00+08:00",
            "end": "2026-07-01T12:00:14+08:00",
        }
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["profile_host_overlap_rules"], [])
        self.assertNotIn("传导链成立", finding["summary"])

    def test_controlled_experiment_cannot_bypass_host_profile_overlap(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        snapshot["iostat"]["started_at"] = "2026-07-01T12:00:16+08:00"
        snapshot["iostat"]["ended_at"] = "2026-07-01T12:00:30+08:00"
        profile = copy.deepcopy(profile)
        profile["profile_window"] = {
            "start": "2026-07-01T12:00:00+08:00",
            "end": "2026-07-01T12:00:14+08:00",
        }
        profile["conduction_evidence"] = {
            "controlled_experiment": _audited_experiment(profile, snapshot["target"])
        }
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["profile_host_overlap_rules"], [])
        self.assertNotIn("传导链成立", finding["summary"])

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

    def test_disjoint_negative_host_evidence_cannot_high_confidence_handoff(self):
        snapshot, profile = _fixture("rc-r500-mte2-handoff.json")
        snapshot["iostat"]["started_at"] = "2026-07-01T12:00:16+08:00"
        snapshot["iostat"]["ended_at"] = "2026-07-01T12:00:30+08:00"
        profile = copy.deepcopy(profile)
        profile["profile_window"] = {
            "start": "2026-07-01T12:00:00+08:00",
            "end": "2026-07-01T12:00:14+08:00",
        }
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "low")
        self.assertIn("不同窗", finding["summary"])

    def test_disjoint_negative_host_evidence_cannot_rule_out_storage_idle(self):
        snapshot, _ = _fixture("rc-r500-mte2-handoff.json")
        snapshot["iostat"]["started_at"] = "2026-07-01T12:00:16+08:00"
        snapshot["iostat"]["ended_at"] = "2026-07-01T12:00:30+08:00"
        profile = {
            "device_free_percent": 25,
            "profile_window": {
                "start": "2026-07-01T12:00:00+08:00",
                "end": "2026-07-01T12:00:14+08:00",
            },
        }
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "low")
        self.assertIn("不同窗", finding["summary"])

    def test_disjoint_profile_cannot_downgrade_confirmed_host_issue(self):
        snapshot, profile = _fixture("rc-r500-downgrade.json")
        snapshot["iostat"]["started_at"] = "2026-07-01T12:00:16+08:00"
        snapshot["iostat"]["ended_at"] = "2026-07-01T12:00:30+08:00"
        profile = copy.deepcopy(profile)
        profile["profile_window"] = {
            "start": "2026-07-01T12:00:00+08:00",
            "end": "2026-07-01T12:00:14+08:00",
        }
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertFalse(finding.get("priority_downgrade"))

    def test_direct_api_experiment_requires_snapshot_profile_overlap(self):
        snapshot, _ = _fixture("rc-r500-conduction-confirmed.json")
        profile = {
            "device_free_percent": 25,
            "profile_window": {
                "start": "2026-07-01T11:00:00+08:00",
                "end": "2026-07-01T11:00:10+08:00",
            },
            "conduction_evidence": {"controlled_experiment": {"result": "improved"}},
        }
        host = a.analyze_r100(snapshot)
        finding = a.analyze_r500_with_host(snapshot, profile, [host])
        self.assertEqual(finding["confidence"], "none")
        self.assertNotIn("传导链成立", finding["summary"])

    def test_direct_r500_call_cannot_bypass_profile_window_check(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        profile = copy.deepcopy(profile)
        profile.pop("profile_window")
        host = a.analyze_r100(snapshot)
        finding = a.analyze_r500_with_host(snapshot, profile, [host])
        self.assertEqual(finding["confidence"], "none")
        self.assertNotIn("传导链成立", finding["summary"])

    def test_direct_r500_ignores_forged_host_findings(self):
        snapshot, profile = _fixture("rc-r500-mte2-handoff.json")
        forged = [
            {
                "rule_id": "R100",
                "severity": "high",
                "confidence": "high",
                "evidence_window_valid": True,
                "evidence_interval": [1782878400.0, 1782878430.0],
                "saturated_devices": [{"device": "nvme0n1", "level": "sustained"}],
            }
        ]

        finding = a.analyze_r500_with_host(snapshot, profile or {}, forged)

        self.assertEqual(finding["severity"], "info")
        self.assertNotIn("Host IO 压力链成立", finding["summary"])
        self.assertEqual(finding.get("handoff"), "ascend-computation-analysis")

    def test_r400_high_carries_explicit_window_provenance(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        finding = _finding(a.analyze_all(snapshot), "R400")
        self.assertEqual(finding["confidence"], "high")
        self.assertTrue(finding["evidence_window_valid"])
        self.assertEqual(len(finding["evidence_interval"]), 2)

    def test_r400_partial_process_map_cannot_reach_high(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        snapshot["process_io_map"]["parsed"]["partial"] = [
            "fd scan capped; mapping coverage is partial"
        ]

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertEqual(finding["confidence"], "medium")
        self.assertIn("process_io_map.partial", finding["evidence_fields"])
        self.assertTrue(
            any("完整 PID/FD 覆盖" in item for item in finding["missing_evidence"])
        )

    def test_r500_partial_target_mapping_cannot_reach_high(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        snapshot["process_io_map"]["parsed"]["partial"] = ["fd scan capped"]

        finding = _finding(a.analyze_all(snapshot, profile), "R500")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertNotIn("传导链成立", finding["summary"])

    def test_r400_does_not_transfer_private_backing_pressure_to_shared_disk(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        saturated = snapshot["iostat"]["parsed"]["disks"]["sda"]
        snapshot["iostat"]["parsed"]["disks"] = {
            "sda": copy.deepcopy(saturated),
            "sdb": copy.deepcopy(saturated),
            "sdc": {
                "r_per_s": 20,
                "rkB_per_s": 2000,
                "r_await_ms": 1,
                "avgqu_sz": 0.1,
                "util_percent": 10,
                "sample_count": 5,
                "device_type": "ssd",
            },
        }
        snapshot["process_io_map"]["parsed"]["mappings"] = [
            {
                "pid": 100,
                "source": "/dev/dm-0",
                "canonical_device": "dm-0",
                "backing_devices": ["sda", "sdc"],
                "device_resolution": "sysfs",
                "path": "/data/shard-0.bin",
                "observation_count": 2,
            },
            {
                "pid": 101,
                "source": "/dev/dm-1",
                "canonical_device": "dm-1",
                "backing_devices": ["sdb", "sdc"],
                "device_resolution": "sysfs",
                "path": "/data/shard-1.bin",
                "observation_count": 2,
            },
        ]

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertNotIn("sdc", finding.get("device_pid_conflicts", {}))
        self.assertIn("sdc", finding.get("candidate_conflicts", {}))
        self.assertTrue(
            any("sdc: 设备未饱和" in item for item in finding["missing_evidence"])
        )

    def test_r400_accepts_saturation_reported_on_dm_logical_device(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        saturated = snapshot["iostat"]["parsed"]["disks"].pop("sda")
        snapshot["iostat"]["parsed"]["disks"]["dm-0"] = saturated
        for mapping in snapshot["process_io_map"]["parsed"]["mappings"]:
            mapping.update(
                source="/dev/dm-0",
                canonical_device="dm-0",
                backing_devices=["sda"],
            )

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertEqual(finding["confidence"], "high")
        self.assertEqual(finding["device_pid_conflicts"], {"dm-0": [100, 101]})

    def test_r400_disjoint_pid_mapping_observations_cannot_reach_high(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        first, second = snapshot["process_io_map"]["parsed"]["mappings"]
        first.update(
            first_seen="2026-07-01T12:00:00+08:00",
            last_seen="2026-07-01T12:00:01+08:00",
        )
        second.update(
            first_seen="2026-07-01T12:00:29+08:00",
            last_seen="2026-07-01T12:00:30+08:00",
        )

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertNotIn("device_pid_conflicts", finding)
        self.assertTrue(any("观测区间" in item for item in finding["missing_evidence"]))

    def test_r400_single_mapping_observation_cannot_reach_high(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        for mapping in snapshot["process_io_map"]["parsed"]["mappings"]:
            mapping["observation_count"] = 1

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertTrue(
            any("实际观测至少两次" in item for item in finding["missing_evidence"])
        )

    def test_r400_cannot_splice_identity_and_time_from_different_mappings(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        mappings = snapshot["process_io_map"]["parsed"]["mappings"]
        split_evidence = []
        for mapping in mappings:
            strong_identity = copy.deepcopy(mapping)
            strong_identity.pop("first_seen")
            strong_identity.pop("last_seen")
            strong_identity["observation_count"] = 0
            timed_heuristic = copy.deepcopy(mapping)
            timed_heuristic["path"] += ".mirror"
            timed_heuristic["device_resolution"] = "heuristic"
            split_evidence.extend([strong_identity, timed_heuristic])
        snapshot["process_io_map"]["parsed"]["mappings"] = split_evidence

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertNotIn("device_pid_conflicts", finding)

    def test_r400_sparse_pid_io_activity_cannot_reach_high(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        for process in snapshot["pidstat"]["parsed"]["processes"]:
            process["active_sample_count"] = 2

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertNotIn("device_pid_conflicts", finding)

    def test_r400_mapping_without_process_identity_cannot_reach_high(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        for mapping in snapshot["process_io_map"]["parsed"]["mappings"]:
            mapping.pop("boot_id")
            mapping.pop("pid_starttime_ticks")

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertNotIn("device_pid_conflicts", finding)
        self.assertTrue(any("PID 身份" in item for item in finding["missing_evidence"]))

    def test_r400_rejects_mappings_from_different_boot_ids(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        snapshot["process_io_map"]["parsed"]["mappings"][1]["boot_id"] = (
            "22222222-2222-2222-2222-222222222222"
        )

        normalized, errors = a.normalize_and_validate(snapshot)
        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertEqual(normalized["process_io_map"]["status"], "parse_failed")
        self.assertIn("multiple boot_id", normalized["process_io_map"]["error"])
        self.assertTrue(any("multiple boot_id" in error for error in errors))
        self.assertNotEqual(finding["confidence"], "high")

    def test_r400_accepts_alternating_read_write_activity(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        snapshot["pidstat"]["parsed"]["reports"] = 4
        for process in snapshot["pidstat"]["parsed"]["processes"]:
            process.update(
                kbr_per_s=75,
                kbw_per_s=75,
                sample_count=4,
                active_sample_count=4,
            )

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertEqual(finding["confidence"], "high")
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["device_pid_conflicts"], {"sda": [100, 101]})

    def test_r400_rejects_non_integer_pidstat_reports(self):
        for value in (5.9, "5", True, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                snapshot, _ = _fixture("rc-r400-conflict.json")
                snapshot["pidstat"]["parsed"]["reports"] = value

                normalized, errors = a.normalize_and_validate(snapshot)
                result = a.analyze_all(snapshot)

                self.assertEqual(normalized["pidstat"]["status"], "parse_failed")
                self.assertTrue(any("pidstat" in error for error in errors))
                self.assertNotEqual(_finding(result, "R400")["confidence"], "high")
                self.assertIn("validation_errors", result)

    def test_r400_rejects_invalid_pidstat_sample_relationships(self):
        variants = (
            {"sample_count": 5.9},
            {"sample_count": "5"},
            {"active_sample_count": True},
            {"active_sample_count": -1},
            {"sample_count": 6},
            {"active_sample_count": 6},
        )
        for update in variants:
            with self.subTest(update=update):
                snapshot, _ = _fixture("rc-r400-conflict.json")
                snapshot["pidstat"]["parsed"]["processes"][0].update(update)

                normalized, errors = a.normalize_and_validate(snapshot)
                result = a.analyze_all(snapshot)

                self.assertEqual(normalized["pidstat"]["status"], "parse_failed")
                self.assertTrue(any("pidstat" in error for error in errors))
                self.assertNotEqual(_finding(result, "R400")["confidence"], "high")
                self.assertTrue(
                    any("pidstat" in error for error in result["validation_errors"])
                )

    def test_r400_rejects_non_integer_observation_samples(self):
        for value in (2.9, "2", True, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                snapshot, _ = _fixture("rc-r400-conflict.json")
                snapshot["process_io_map"]["parsed"]["observation_samples"] = value

                normalized, errors = a.normalize_and_validate(snapshot)
                result = a.analyze_all(snapshot)

                self.assertEqual(normalized["process_io_map"]["status"], "parse_failed")
                self.assertTrue(any("observation_samples" in error for error in errors))
                self.assertNotEqual(_finding(result, "R400")["confidence"], "high")
                self.assertTrue(
                    any(
                        "observation_samples" in error
                        for error in result["validation_errors"]
                    )
                )

    def test_r400_drops_invalid_mapping_observation_count(self):
        for value in (2.9, "2", True, -1, 3, float("nan"), float("inf")):
            with self.subTest(value=value):
                snapshot, _ = _fixture("rc-r400-conflict.json")
                mappings = snapshot["process_io_map"]["parsed"]["mappings"]
                mappings[0]["observation_count"] = value

                normalized, errors = a.normalize_and_validate(snapshot)
                result = a.analyze_all(snapshot)

                self.assertEqual(normalized["process_io_map"]["status"], "ok")
                self.assertEqual(
                    len(normalized["process_io_map"]["parsed"]["mappings"]), 1
                )
                self.assertTrue(any("mappings[0]" in error for error in errors))
                self.assertNotEqual(_finding(result, "R400")["confidence"], "high")
                self.assertTrue(
                    any("mappings[0]" in error for error in result["validation_errors"])
                )

    def test_r500_self_declared_controlled_experiment_is_capped(self):
        snapshot, profile = _fixture("rc-r500-conduction.json")
        profile = copy.deepcopy(profile)
        profile["conduction_evidence"] = {
            "controlled_experiment": _audited_experiment(profile, snapshot["target"])
        }
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["certified_conduction_evidence"], [])
        self.assertNotIn("传导链成立", finding["summary"])

    def test_r500_rejects_r400_conflict_outside_target_pid_scope(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        healthy = copy.deepcopy(snapshot["iostat"]["parsed"]["disks"]["sda"])
        healthy.update(
            util_percent=10,
            util_max=10,
            util_p95=10,
            avgqu_sz=0.1,
            r_await_ms=0.5,
        )
        snapshot["iostat"]["parsed"]["disks"]["sdb"] = healthy
        snapshot["target"] = {"pid": 999, "path": None}
        parsed = snapshot["process_io_map"]["parsed"]
        parsed["pid_tree"] = [
            {
                "pid": 999,
                "role": "root",
                "boot_id": "11111111-1111-1111-1111-111111111111",
                "pid_starttime_ticks": 9990,
            }
        ]
        parsed["mappings"].append(
            {
                "pid": 999,
                "boot_id": "11111111-1111-1111-1111-111111111111",
                "pid_starttime_ticks": 9990,
                "source": "/dev/sdb",
                "canonical_device": "sdb",
                "backing_devices": [],
                "device_resolution": "sysfs",
                "path": "/data/target.bin",
                "first_seen": "2026-07-01T12:00:00+08:00",
                "last_seen": "2026-07-01T12:00:30+08:00",
                "observation_count": 2,
            }
        )
        _, profile = _fixture("rc-r500-conduction-confirmed.json")
        profile = copy.deepcopy(profile)
        overlap = profile["conduction_evidence"]["overlap_provenance"]
        overlap["host_rule_ids"] = ["R400"]
        overlap["target"] = {"pid": 999, "path": None}

        result = a.analyze_all(snapshot, profile)
        r400 = _finding(result, "R400")
        r500 = _finding(result, "R500")

        self.assertEqual(r400["confidence"], "high")
        self.assertNotEqual(r500["severity"], "high")
        self.assertNotIn("传导链成立", r500["summary"])
        self.assertEqual(r500["certified_conduction_evidence"], [])

    def test_r500_null_target_cannot_certify_conduction(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        snapshot.pop("target")
        snapshot.pop("process_io_map")
        profile = copy.deepcopy(profile)
        profile["conduction_evidence"]["overlap_provenance"]["target"] = {
            "pid": None,
            "path": None,
        }

        finding = _finding(a.analyze_all(snapshot, profile), "R500")

        self.assertEqual(finding["confidence"], "medium")
        self.assertFalse(finding["target_binding_certified"])
        self.assertNotIn("传导链成立", finding["summary"])

    def test_r500_bare_conduction_assertions_cannot_reach_high(self):
        for conduction_evidence in (
            {"io_npu_overlap_observed": True},
            {"controlled_experiment": {"result": "improved"}},
        ):
            with self.subTest(conduction_evidence=conduction_evidence):
                snapshot, profile = _fixture("rc-r500-conduction.json")
                profile = copy.deepcopy(profile)
                profile["conduction_evidence"] = conduction_evidence

                result = a.analyze_all(snapshot, profile)
                finding = _finding(result, "R500")

                self.assertEqual(finding["confidence"], "medium")
                self.assertEqual(finding["certified_conduction_evidence"], [])
                self.assertTrue(result.get("profile_validation_errors"))

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

    def test_huge_schema_major_and_unhashable_provenance_are_structured_errors(self):
        huge = a.analyze_all({"schema_version": f"{'9' * 5000}.0"})
        self.assertIn("error", huge)
        self.assertIn("too long", huge["error"])

        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        profile = copy.deepcopy(profile)
        entry = profile["provenance"]["device_free_percent"]
        entry["source_type"] = []
        entry["extraction_method"] = {}

        result = a.analyze_all(snapshot, profile)
        finding = _finding(result, "R500")

        self.assertEqual(finding["confidence"], "medium")
        self.assertTrue(
            any(
                "source_type and extraction_method" in error
                for error in result["profile_validation_errors"]
            )
        )

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

    def test_empty_mount_collection_cannot_rule_out_network_storage(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["mounts"] = []
        snapshot["mounts_provider"].update(status="empty", parsed=[])

        finding = _finding(a.analyze_all(snapshot), "R200")

        self.assertEqual(finding["confidence"], "none")
        self.assertEqual(finding["severity"], "info")
        self.assertFalse(finding["performance_window_evaluated"])
        self.assertFalse(finding["evidence_window_valid"])
        self.assertIn("采集为空", finding["summary"])

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

    def test_unhashable_mount_provider_status_is_normalized(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        snapshot["mounts_provider"]["status"] = ["ok"]
        normalized, errors = a.normalize_and_validate(snapshot)
        self.assertEqual(normalized["mounts_provider"]["status"], "parse_failed")
        self.assertTrue(any("invalid status" in item for item in errors))

    def test_legacy_available_provider_is_deeply_validated(self):
        snapshot, _ = _fixture("rc-r400-conflict.json")
        snapshot["pidstat"].pop("status")
        snapshot["pidstat"]["available"] = True
        snapshot["pidstat"]["parsed"]["processes"].append([])

        normalized, errors = a.normalize_and_validate(snapshot)
        result = a.analyze_all(snapshot)

        self.assertEqual(normalized["pidstat"]["status"], "parse_failed")
        self.assertTrue(any("pidstat" in item for item in errors))
        self.assertTrue(any("pidstat" in item for item in result["validation_errors"]))
        self.assertNotEqual(_finding(result, "R400")["confidence"], "high")
        self.assertNotIn("device_pid_conflicts", _finding(result, "R400"))
        self.assertIn("采集错误", _finding(result, "R000")["summary"])

    def test_malformed_legacy_availability_does_not_raise(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot.pop("mounts_provider")
        snapshot["availability"] = {
            "missing": 1,
            "partial": {"mounts": "empty"},
            "errors": True,
        }
        normalized, _errors = a.normalize_and_validate(snapshot)
        self.assertIn("mounts_provider", normalized)

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

    def test_static_context_outside_dynamic_window_is_not_invalid(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["df"] = {
            "source": "df",
            "status": "ok",
            "started_at": "2026-07-01T12:00:31+08:00",
            "ended_at": "2026-07-01T12:00:32+08:00",
            "parsed": {"filesystems": []},
        }
        snapshot["memory"] = {
            "source": "memory",
            "status": "ok",
            "started_at": "2026-07-01T12:00:31+08:00",
            "ended_at": "2026-07-01T12:00:32+08:00",
            "parsed": {"memavailable": 1},
        }

        _, errors = a.normalize_and_validate(snapshot)

        self.assertFalse(
            any(name in item for name in ("df", "memory") for item in errors)
        )

    def test_disjoint_mount_and_nfs_windows_cannot_claim_high(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        snapshot["mounts_provider"]["started_at"] = "2026-07-01T12:00:00+08:00"
        snapshot["mounts_provider"]["ended_at"] = "2026-07-01T12:00:01+08:00"
        snapshot["nfs"]["started_at"] = "2026-07-01T12:00:29+08:00"
        snapshot["nfs"]["ended_at"] = "2026-07-01T12:00:30+08:00"

        finding = _finding(a.analyze_all(snapshot), "R200")

        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertFalse(finding["evidence_window_valid"])

    def test_adjacent_mount_and_nfs_windows_can_claim_high(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        snapshot["nfs"]["started_at"] = "2026-07-01T12:00:00+08:00"
        snapshot["nfs"]["ended_at"] = "2026-07-01T12:00:28+08:00"
        snapshot["mounts_provider"]["started_at"] = "2026-07-01T12:00:29+08:00"
        snapshot["mounts_provider"]["ended_at"] = "2026-07-01T12:00:30+08:00"

        finding = _finding(a.analyze_all(snapshot), "R200")

        self.assertEqual(finding["confidence"], "high")
        self.assertTrue(finding["evidence_window_valid"])

    def test_r200_ignores_major_timeout_without_consistent_request_delta(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        metric = snapshot["nfs"]["parsed"]["mount_metrics"][0]
        metric.update(
            ops=0,
            transmissions=0,
            retrans=0,
            major_timeouts=1,
            avg_rtt_ms=0,
            avg_execute_ms=0,
        )

        finding = _finding(a.analyze_all(snapshot), "R200")

        self.assertNotEqual(finding["confidence"], "high")
        self.assertFalse(finding.get("confirmed_mounts"))
        self.assertTrue(
            any("major-timeout" in note for note in finding.get("handoff_notes", []))
        )

    def test_r200_accepts_major_timeout_with_consistent_request_delta(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        metric = snapshot["nfs"]["parsed"]["mount_metrics"][0]
        metric.update(
            ops=1,
            transmissions=1,
            retrans=0,
            major_timeouts=1,
            avg_rtt_ms=0,
            avg_execute_ms=0,
        )

        finding = _finding(a.analyze_all(snapshot), "R200")

        self.assertEqual(finding["confidence"], "high")
        self.assertEqual(finding["confirmed_mounts"][0]["major_timeouts"], 1.0)

    def test_non_contract_nfs_prefix_cannot_claim_r200(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        snapshot["mounts"][0]["fstype"] = "nfsbogus"
        snapshot["nfs"]["parsed"]["mount_metrics"][0]["fstype"] = "nfsbogus"

        finding = _finding(a.analyze_all(snapshot), "R200")

        self.assertEqual(finding["severity"], "info")
        self.assertFalse(finding.get("confirmed_mounts"))

    def test_root_nfs_mount_is_bound_to_target_path(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        snapshot["target"] = {"path": "/data/train", "pid": None}
        snapshot["mounts"][0]["mount_point"] = "/"
        snapshot["nfs"]["parsed"]["mount_metrics"][0].update(
            mount_point="/",
            metadata_ops=100,
            avg_metadata_rtt_ms=15,
            avg_metadata_execute_ms=25,
        )
        result = a.analyze_all(snapshot)
        r200 = _finding(result, "R200")
        r300 = _finding(result, "R300")
        self.assertEqual(r200["confidence"], "high")
        self.assertEqual(r300["confidence"], "high")
        self.assertEqual(len(r200["evidence_interval"]), 2)
        self.assertEqual(len(r300["evidence_interval"]), 2)
        self.assertEqual(r200["nfs_metric_required_scope"], "target_path")

    def test_deeper_local_mount_shadows_parent_nfs_for_target(self):
        snapshot, _ = _fixture("rc-r200-confirmed.json")
        snapshot["target"] = {"path": "/data/cache/shard.bin", "pid": None}
        snapshot["mounts"].append(
            {
                "device": "/dev/sdb1",
                "mount_point": "/data/cache",
                "fstype": "ext4",
            }
        )
        result = a.analyze_all(snapshot)
        r200 = _finding(result, "R200")
        self.assertEqual(r200["nfs_metric_required_scope"], "target_path_non_nfs")
        self.assertFalse(r200.get("confirmed_mounts"))

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

    def test_empty_iostat_device_cannot_report_healthy(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["iostat"]["parsed"]["disks"] = {"sda": {}}
        snapshot["diskstats_sample"] = []
        result = a.analyze_all(snapshot)
        finding = _finding(result, "R100")
        self.assertEqual(finding["confidence"], "none")
        self.assertIn("无法判定", finding["summary"])
        self.assertTrue(
            any("no usable IO metric" in item for item in result["validation_errors"])
        )

    def test_partial_iostat_fields_cannot_report_high_confidence_health(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["iostat"]["parsed"]["disks"] = {"sda": {"r_per_s": 0}}
        snapshot["diskstats_sample"] = []
        finding = _finding(a.analyze_all(snapshot), "R100")
        self.assertEqual(finding["severity"], "info")
        self.assertEqual(finding["confidence"], "low")
        self.assertIn("字段覆盖不足", finding["summary"])

    def test_sparse_util_samples_cannot_claim_sustained_pressure(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        metrics = snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]
        metrics.update(
            util_percent=95,
            util_max=100,
            util_p95=100,
            avgqu_sz=4,
            sample_count=5,
            util_sample_count=1,
            avgqu_sz_with_util_sample_count=1,
            r_await_ms_with_util_sample_count=1,
        )
        finding = _finding(a.analyze_all(snapshot), "R100")
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertFalse(
            any(
                device.get("level") == "sustained"
                for device in finding.get("saturated_devices", [])
            )
        )

    def test_huge_json_integer_is_rejected_without_crashing(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]["r_per_s"] = 10**400
        normalized, errors = a.normalize_and_validate(snapshot)
        self.assertEqual(normalized["iostat"]["status"], "parse_failed")
        self.assertTrue(any("invalid metric" in item for item in errors))
        a.analyze_all(snapshot)

    def test_deep_json_input_returns_structured_resource_error(self):
        nested: list[object] = []
        for _ in range(a._MAX_JSON_DEPTH + 1):
            nested = [nested]

        result = a.analyze_all({"schema_version": "1.4", "nested": nested})

        self.assertEqual(result["findings"], [])
        self.assertIn("resource limit exceeded", result["error"])

    def test_cli_rejects_deep_json_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "deep.json"
            snapshot_path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr):
                rc = a.main([str(snapshot_path)])

        self.assertEqual(rc, 1)
        self.assertIn("Snapshot JSON", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

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

    def test_target_with_partial_metrics_cannot_inherit_other_device_health(self):
        snapshot, profile = _fixture("rc-r500-conduction-confirmed.json")
        snapshot["iostat"]["parsed"]["disks"]["sdb"] = {"r_per_s": 0}
        snapshot["target"] = {"pid": 42, "path": "/data"}
        snapshot["process_io_map"] = {
            "source": "process_io_map",
            "status": "ok",
            "started_at": "2026-07-01T12:00:00+08:00",
            "ended_at": "2026-07-01T12:00:30+08:00",
            "parsed": {
                "mappings": [
                    {
                        "pid": 42,
                        "path": "/data/shard.bin",
                        "canonical_device": "sdb",
                        "backing_devices": [],
                        "device_resolution": "sysfs",
                    }
                ]
            },
        }
        r500 = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(r500["confidence"], "none")
        self.assertEqual(r500["severity"], "info")
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
        self.assertIn("未观察到至少两个 PID", finding["summary"])
        self.assertTrue(any("至少两个" in item for item in finding["missing_evidence"]))

    def test_r400_single_device_mappings_report_missing_contention_evidence(self):
        snapshot, _ = _fixture("rc-r400-no-conflict.json")

        finding = _finding(a.analyze_all(snapshot), "R400")

        self.assertEqual(finding["confidence"], "low")
        self.assertIn("未观察到至少两个 PID", finding["summary"])
        self.assertTrue(
            any("同设备映射" in item for item in finding["missing_evidence"])
        )
        self.assertTrue(any("pidstat" in item for item in finding["missing_evidence"]))

    def test_r400_without_evidence_has_info_severity(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        finding = _finding(a.analyze_all(snapshot), "R400")
        self.assertEqual(finding["confidence"], "none")
        self.assertEqual(finding["severity"], "info")

    def test_r300_without_candidate_signal_has_info_severity(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        finding = _finding(a.analyze_all(snapshot), "R300")
        self.assertEqual(finding["severity"], "info")
        self.assertEqual(finding["confidence"], "low")

    def test_single_healthy_iostat_sample_cannot_rule_out_host_io(self):
        snapshot, profile = _fixture("rc-r500-mte2-handoff.json")
        metrics = snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]
        metrics.update(
            sample_count=1,
            util_sample_count=1,
            avgqu_sz_sample_count=1,
            r_await_ms_sample_count=1,
            avgqu_sz_with_util_sample_count=1,
            r_await_ms_with_util_sample_count=1,
        )

        result = a.analyze_all(snapshot, profile)
        r100 = _finding(result, "R100")
        r500 = _finding(result, "R500")

        self.assertNotEqual(r100["confidence"], "high")
        self.assertIn("样本", r100["summary"])
        self.assertNotEqual(r500["confidence"], "high")

    def test_sparse_queue_and_await_cannot_claim_sustained_r100(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        metrics = snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]
        metrics.update(
            sample_count=3,
            util_sample_count=3,
            avgqu_sz_sample_count=1,
            r_await_ms_sample_count=1,
            avgqu_sz_with_util_sample_count=1,
            r_await_ms_with_util_sample_count=1,
        )

        finding = _finding(a.analyze_all(snapshot), "R100")

        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertFalse(
            any(
                item.get("level") == "sustained"
                for item in finding.get("saturated_devices", [])
            )
        )

    def test_sparse_queue_cannot_borrow_dense_await_for_r100_high(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        metrics = snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]
        metrics.update(
            sample_count=5,
            util_sample_count=5,
            avgqu_sz_sample_count=1,
            r_await_ms_sample_count=5,
            avgqu_sz_with_util_sample_count=1,
            r_await_ms_with_util_sample_count=5,
        )

        finding = _finding(a.analyze_all(snapshot), "R100")

        self.assertEqual(finding["confidence"], "medium")
        self.assertFalse(
            any(
                item.get("level") == "sustained"
                for item in finding.get("saturated_devices", [])
            )
        )

    def test_disjoint_util_and_pressure_samples_cannot_reach_high(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["iostat"]["parsed"]["reports"] = 6
        metrics = snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]
        metrics.update(
            sample_count=6,
            util_sample_count=3,
            avgqu_sz_sample_count=3,
            r_await_ms_sample_count=3,
            avgqu_sz_with_util_sample_count=0,
            r_await_ms_with_util_sample_count=0,
        )

        finding = _finding(a.analyze_all(snapshot), "R100")

        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertFalse(
            any(
                item.get("level") == "sustained"
                for item in finding.get("saturated_devices", [])
            )
        )

    def test_missing_cooccurrence_counts_cannot_reach_r100_high(self):
        for fixture_name in ("rc-r100-bandwidth.json", "rc-r500-mte2-handoff.json"):
            with self.subTest(fixture=fixture_name):
                snapshot, profile = _fixture(fixture_name)
                metrics = next(iter(snapshot["iostat"]["parsed"]["disks"].values()))
                for key in list(metrics):
                    if key.endswith("_with_util_sample_count"):
                        metrics.pop(key)

                result = a.analyze_all(snapshot, profile)
                self.assertNotEqual(_finding(result, "R100")["confidence"], "high")
                if profile is not None:
                    self.assertNotEqual(_finding(result, "R500")["confidence"], "high")

    def test_field_sample_count_cannot_exceed_total_reports(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        metrics = snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]
        metrics.update(sample_count=3, util_sample_count=4)

        normalized, errors = a.normalize_and_validate(snapshot)

        self.assertEqual(normalized["iostat"]["status"], "parse_failed")
        self.assertTrue(any("greater than sample_count" in item for item in errors))

    def test_device_sample_count_cannot_exceed_iostat_reports(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot["iostat"]["parsed"]["reports"] = 1

        result = a.analyze_all(snapshot)

        finding = _finding(result, "R100")
        self.assertNotEqual(finding["confidence"], "high")
        self.assertTrue(
            any(
                "sample_count greater than parsed.reports" in item
                for item in result["validation_errors"]
            )
        )

    def test_cooccurrence_count_cannot_exceed_component_counts(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        metrics = snapshot["iostat"]["parsed"]["disks"]["nvme0n1"]
        metrics.update(
            util_sample_count=3,
            avgqu_sz_sample_count=3,
            avgqu_sz_with_util_sample_count=4,
        )

        normalized, errors = a.normalize_and_validate(snapshot)

        self.assertEqual(normalized["iostat"]["status"], "parse_failed")
        self.assertTrue(any("co-occurrence count" in item for item in errors))

    def test_iso_short_iostat_window_caps_r100_at_medium(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot.pop("duration_seconds", None)
        snapshot["window"]["end"] = "2026-07-01T12:00:03+08:00"
        snapshot["iostat"]["ended_at"] = "2026-07-01T12:00:03+08:00"

        finding = _finding(a.analyze_all(snapshot), "R100")

        self.assertEqual(a._snapshot_duration(snapshot), 3.0)
        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertIn("短于 10s", finding["summary"])

    def test_short_diskstats_pressure_window_caps_r100_at_medium(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        snapshot.pop("duration_seconds", None)
        snapshot["window"]["end"] = "2026-07-01T12:00:03+08:00"
        snapshot["iostat"] = {"source": "iostat", "status": "missing"}
        start = a._parse_iso(snapshot["window"]["start"])
        self.assertIsNotNone(start)
        snapshot["diskstats_sample"] = [
            {
                "sample_index": 0,
                "timestamp": start,
                "disks": {
                    "sda": {
                        "reads_completed": 0,
                        "writes_completed": 0,
                        "sectors_read": 0,
                        "sectors_written": 0,
                        "time_reading_ms": 0,
                        "time_writing_ms": 0,
                        "time_io_ms": 0,
                        "weighted_time_io_ms": 0,
                    }
                },
            },
            {
                "sample_index": 1,
                "timestamp": start + 3,
                "disks": {
                    "sda": {
                        "reads_completed": 600,
                        "writes_completed": 0,
                        "sectors_read": 120000,
                        "sectors_written": 0,
                        "time_reading_ms": 18000,
                        "time_writing_ms": 0,
                        "time_io_ms": 3000,
                        "weighted_time_io_ms": 12000,
                    }
                },
            },
        ]

        finding = _finding(a.analyze_all(snapshot), "R100")

        self.assertEqual(finding["confidence"], "medium")
        self.assertEqual(finding["severity"], "medium")
        self.assertIn("短于 10s", finding["summary"])

    def test_long_diskstats_pressure_cannot_certify_r100_high(self):
        for duration in (10, 30):
            with self.subTest(duration=duration):
                finding = _finding(
                    a.analyze_all(_diskstats_snapshot(duration, pressured=True)), "R100"
                )

                self.assertEqual(finding["confidence"], "medium")
                self.assertEqual(finding["severity"], "medium")
                self.assertFalse(
                    any(
                        item.get("level") == "sustained"
                        for item in finding.get("saturated_devices", [])
                    )
                )

    def test_long_diskstats_health_cannot_certify_r100_high(self):
        for duration in (10, 30):
            with self.subTest(duration=duration):
                finding = _finding(
                    a.analyze_all(_diskstats_snapshot(duration, pressured=False)),
                    "R100",
                )

                self.assertEqual(finding["confidence"], "medium")
                self.assertEqual(finding["severity"], "info")
                self.assertFalse(
                    finding["assessed_devices"][0]["health_evidence_dense"]
                )

    def test_r300_small_io_candidate_lists_confirmation_evidence(self):
        snapshot, _ = _fixture("rc-r300-small-file.json")
        finding = _finding(a.analyze_all(snapshot), "R300")
        self.assertEqual(finding["confidence"], "medium")
        self.assertTrue(finding["missing_evidence"])
        self.assertTrue(
            any(
                "元数据" in item or "syscall" in item
                for item in finding["missing_evidence"]
            )
        )

    def test_r300_diskstats_fallback_reports_actual_evidence_source(self):
        snapshot = _diskstats_snapshot(10, pressured=False)
        counters = snapshot["diskstats_sample"][1]["disks"]["sda"]
        reads = 6_000 * 10
        counters.update(
            {
                "reads_completed": reads,
                "sectors_read": reads * 8,
                "time_reading_ms": reads,
            }
        )

        finding = _finding(a.analyze_all(snapshot), "R300")

        self.assertEqual(finding["confidence"], "medium")
        self.assertIn("diskstats_delta.disks（小 IO 特征）", finding["evidence_fields"])
        self.assertNotIn("iostat.disks（小 IO 特征）", finding["evidence_fields"])

    def test_r500_medium_conduction_does_not_claim_missing_host_overlap(self):
        snapshot, profile = _fixture("rc-r500-conduction.json")
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["profile_host_overlap_rules"], ["R100"])
        self.assertFalse(
            any("足量公共交集" in item for item in finding["missing_evidence"])
        )
        self.assertTrue(
            any("conduction_evidence" in item for item in finding["missing_evidence"])
        )

    def test_r500_low_device_free_does_not_infer_compute_masking(self):
        snapshot, profile = _fixture("rc-r500-downgrade.json")
        finding = _finding(a.analyze_all(snapshot, profile), "R500")
        self.assertEqual(finding["confidence"], "medium")
        self.assertNotIn("计算掩盖", finding["summary"])
        self.assertIn("未观察到明显设备空泡", finding["summary"])
        self.assertIn("不能据此确认", finding["summary"])
        self.assertFalse(finding.get("priority_downgrade"))
        self.assertTrue(
            any("artifact verifier" in item for item in finding["missing_evidence"])
        )

    def test_explicit_invalid_profile_returns_nonzero(self):
        snapshot, _ = _fixture("rc-r100-bandwidth.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.json"
            profile_path = Path(temp_dir) / "profile.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            profile_path.write_text("{not-json", encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr):
                rc = a.main([str(snapshot_path), "--profile", str(profile_path)])
        self.assertNotEqual(rc, 0)
        self.assertIn("profile 解析失败", stderr.getvalue())

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
