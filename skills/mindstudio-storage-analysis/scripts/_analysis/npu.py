#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#    http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------
"""Target projection and NPU correlation rule R500."""

from __future__ import annotations

import copy
from collections import defaultdict
import re

from .common import (
    _ORDER,
    _canonical_dev,
    _finding,
    _finding_evidence_interval,
    _parsed,
    _profile_host_overlap_rules,
    _profile_interval,
    _profile_window_matches_snapshot,
    _provider,
    _provider_interval,
    _status,
    intervals_have_common_overlap,
)

from .contract import (
    _certified_profile_metrics,
    _strict_json_int,
    _target_pid_scope,
    validate_analysis_request,
)

from .local import (
    analyze_r100,
)

from .path_scope import (
    _is_data_relevant_path,
)

from .network import (
    _nfs_identity,
    _norm_fstype_group,
    _required_nfs_identities,
    analyze_r200,
    analyze_r300,
)

from .contention import (
    _mapping_observation_interval,
    analyze_r400,
)


def _is_confirmed_host_issue(finding: dict) -> bool:
    """该根因桶是否构成已确认的 Host IO 压力（用于 R500 传导入口）。

    只接受每条规则的明确确认字段，不接受通用 medium/medium 兜底；
    否则 R300 的 small_io 候选（medium）会被 R500 升级成"已传导"高置信误判。
      R100 → saturated_devices（且 level 含 sustained/likely/transient 才算观测到压力）
      R200 → confirmed_mounts
      R300 → metadata_slow_mounts（small_io_devices 候选不算）
      R400 → device_pid_conflicts
    """
    if finding.get("rule_id") not in ("R100", "R200", "R300", "R400"):
        return False
    rid = finding["rule_id"]
    if rid == "R100":
        sd = finding.get("saturated_devices") or []
        return (
            finding.get("confidence") == "high"
            and finding.get("evidence_window_valid") is True
            and any(isinstance(d, dict) and d.get("level") == "sustained" for d in sd)
        )
    if rid == "R200":
        return (
            finding.get("confidence") == "high"
            and finding.get("evidence_window_valid") is True
            and bool(finding.get("confirmed_mounts"))
        )
    if rid == "R300":
        return (
            finding.get("confidence") == "high"
            and finding.get("evidence_window_valid") is True
            and bool(finding.get("metadata_slow_mounts"))
        )
    if rid == "R400":
        return (
            finding.get("confidence") == "high"
            and finding.get("evidence_window_valid") is True
            and bool(finding.get("device_pid_conflicts"))
        )
    return False


def _target_device_context(snapshot: dict) -> tuple[set[str], bool]:
    """Return target devices only from repeated, identity-bound mapping evidence."""
    provider = _provider(snapshot, "process_io_map")
    parsed = _parsed(provider)
    provider_interval = _provider_interval(snapshot, "process_io_map")
    if (
        _status(provider) != "ok"
        or not isinstance(parsed, dict)
        or provider_interval is None
        or bool(parsed.get("partial"))
    ):
        return set(), False
    target = snapshot.get("target")
    if not isinstance(target, dict):
        return set(), False
    target_path = target.get("path")
    raw_target_pid = target.get("pid")
    target_pid = (
        raw_target_pid
        if isinstance(raw_target_pid, int) and not isinstance(raw_target_pid, bool) and raw_target_pid > 0
        else None
    )
    if target_pid is None and not (isinstance(target_path, str) and target_path):
        return set(), False
    try:
        observation_samples = _strict_json_int(parsed.get("observation_samples"), positive=True)
    except ValueError:
        return set(), False
    if observation_samples < 2:
        return set(), False
    allowed_pids = _target_pid_scope(snapshot) if target_pid is not None else None
    devices: set[str] = set()
    strong = True
    for mapping in parsed.get("mappings", []) or []:
        if not isinstance(mapping, dict):
            continue
        pid = mapping.get("pid")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or (allowed_pids is not None and pid not in allowed_pids)
        ):
            continue
        if not _is_data_relevant_path(mapping.get("path"), target_path):
            continue
        try:
            observation_count = _strict_json_int(mapping.get("observation_count"))
        except ValueError:
            observation_count = 0
        mapping_interval = _mapping_observation_interval(mapping, provider_interval)
        boot_id = mapping.get("boot_id")
        starttime = mapping.get("pid_starttime_ticks")
        mapping_strong = bool(
            mapping.get("device_resolution") == "sysfs"
            and 2 <= observation_count <= observation_samples
            and mapping_interval is not None
            and mapping_interval[1] - mapping_interval[0] >= 1.0
            and isinstance(boot_id, str)
            and re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                boot_id,
            )
            and isinstance(starttime, int)
            and not isinstance(starttime, bool)
            and starttime >= 0
        )
        canonical = _canonical_dev(str(mapping.get("canonical_device") or ""))
        topology = [canonical] + [
            _canonical_dev(str(device)) for device in (mapping.get("backing_devices") or []) if isinstance(device, str)
        ]
        devices.update(device for device in topology if device)
        if not mapping_strong:
            strong = False
    return devices, bool(devices) and strong


def _target_device_mapping_intervals(
    snapshot: dict,
) -> dict[str, list[tuple[float, float]]]:
    """Return actual repeated target-mapping intervals, keyed by device topology."""
    provider = _provider(snapshot, "process_io_map")
    parsed = _parsed(provider)
    provider_interval = _provider_interval(snapshot, "process_io_map")
    target = snapshot.get("target")
    if (
        _status(provider) != "ok"
        or not isinstance(parsed, dict)
        or provider_interval is None
        or not isinstance(target, dict)
    ):
        return {}
    target_path = target.get("path")
    target_pid_scope = _target_pid_scope(snapshot)
    try:
        observations = _strict_json_int(parsed.get("observation_samples"), positive=True)
    except ValueError:
        return {}
    intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for mapping in parsed.get("mappings", []) or []:
        if not isinstance(mapping, dict) or not _is_data_relevant_path(mapping.get("path"), target_path):
            continue
        pid = mapping.get("pid")
        if target_pid_scope is not None and pid not in target_pid_scope:
            continue
        try:
            count = _strict_json_int(mapping.get("observation_count"))
        except ValueError:
            continue
        interval = _mapping_observation_interval(mapping, provider_interval)
        if (
            mapping.get("device_resolution") != "sysfs"
            or count < 2
            or count > observations
            or interval is None
            or interval[1] - interval[0] < 1.0
        ):
            continue
        devices = [_canonical_dev(str(mapping.get("canonical_device") or ""))]
        devices.extend(
            _canonical_dev(str(device)) for device in (mapping.get("backing_devices") or []) if isinstance(device, str)
        )
        for device in devices:
            if device:
                intervals[device].append(interval)
    return dict(intervals)


def _target_binding_is_certified(snapshot: dict) -> bool:
    """Require an explicit target with strong block or current NFS identity evidence."""
    target = snapshot.get("target")
    if not isinstance(target, dict) or not (target.get("pid") is not None or bool(target.get("path"))):
        return False
    _devices, block_identity_strong = _target_device_context(snapshot)
    nfs_mounts = [
        mount
        for mount in (snapshot.get("mounts") or [])
        if isinstance(mount, dict) and _norm_fstype_group(mount.get("fstype")) == "nfs"
    ]
    nfs_identities, _scope = _required_nfs_identities(snapshot, nfs_mounts)
    nfs_identity_strong = bool(nfs_identities) and _provider_interval(snapshot, "mounts_provider") is not None
    return block_identity_strong or nfs_identity_strong


def _project_host_assessments_to_target(snapshot: dict, assessments: list[dict]) -> list[dict]:
    """有目标设备映射时，只允许目标 workload 的设备证据进入 R500。"""
    target_devices, target_identity_strong = _target_device_context(snapshot)
    target_mapping_intervals = _target_device_mapping_intervals(snapshot)
    nfs_mounts_for_target = [
        m
        for m in (snapshot.get("mounts") or [])
        if isinstance(m, dict) and _norm_fstype_group(m.get("fstype")) == "nfs"
    ]
    target_nfs_identities, target_nfs_scope = _required_nfs_identities(snapshot, nfs_mounts_for_target)
    target_nfs_known = bool(target_nfs_identities) or target_nfs_scope.endswith("_non_nfs")
    target = snapshot.get("target")
    target_requested = isinstance(target, dict) and (target.get("pid") is not None or bool(target.get("path")))
    if target_requested and not target_devices and not target_nfs_known:
        projected: list[dict] = []
        for finding in assessments:
            item = copy.deepcopy(finding)
            item["confidence"] = "none"
            item["severity"] = "info"
            item["evidence_window_valid"] = False
            for key in (
                "saturated_devices",
                "confirmed_mounts",
                "metadata_slow_mounts",
                "device_pid_conflicts",
            ):
                item.pop(key, None)
            item["summary"] = "目标 workload 的设备或挂载身份未解析，不能沿用全机 Host IO 结论。"
            item.setdefault("missing_evidence", []).append("目标 PID/路径到设备或 NFS 挂载的可靠映射")
            projected.append(item)
        return projected
    if not target_devices and not target_nfs_known:
        return assessments
    target_pid_scope = _target_pid_scope(snapshot)
    projected: list[dict] = []
    for finding in assessments:
        if finding.get("rule_id") == "R200" and target_nfs_known:
            item = copy.deepcopy(finding)
            confirmed = [
                mount
                for mount in (item.get("confirmed_mounts") or [])
                if isinstance(mount, dict) and _nfs_identity(mount, source_key="source") in target_nfs_identities
            ]
            item["confirmed_mounts"] = confirmed
            if target_nfs_scope.endswith("_non_nfs"):
                item["performance_window_evaluated"] = True
                item["evidence_window_valid"] = True
            if not confirmed and finding.get("confirmed_mounts"):
                item["confidence"] = "high" if item.get("evidence_window_valid") else "none"
                item["severity"] = "info"
                item["summary"] = "R200 evidence was outside target workload NFS scope."
            projected.append(item)
            continue
        if finding.get("rule_id") == "R300" and target_nfs_known:
            item = copy.deepcopy(finding)
            slow = [
                mount
                for mount in (item.get("metadata_slow_mounts") or [])
                if isinstance(mount, dict) and _nfs_identity(mount, source_key="source") in target_nfs_identities
            ]
            item["metadata_slow_mounts"] = slow
            if not slow and finding.get("metadata_slow_mounts"):
                item["confidence"] = "none"
                item["severity"] = "info"
                item["evidence_window_valid"] = False
                item["summary"] = "R300 metadata evidence was outside target workload NFS scope."
            projected.append(item)
            continue
        if finding.get("rule_id") == "R400":
            item = copy.deepcopy(finding)
            raw_conflicts = item.get("device_pid_conflicts")
            raw_intervals = item.get("device_evidence_intervals")
            if not isinstance(raw_conflicts, dict):
                raw_conflicts = {}
            if not isinstance(raw_intervals, dict):
                raw_intervals = {}
            conflicts = {
                _canonical_dev(str(device)): pids
                for device, pids in raw_conflicts.items()
                if _canonical_dev(str(device)) in target_devices
                and isinstance(pids, list)
                and (target_pid_scope is None or any(pid in target_pid_scope for pid in pids))
            }
            intervals: dict[str, list[float]] = {}
            for device, interval in raw_intervals.items():
                canonical = _canonical_dev(str(device))
                parsed_interval = _finding_evidence_interval({"evidence_interval": interval})
                if canonical in conflicts and parsed_interval is not None:
                    intervals[canonical] = list(parsed_interval)
            item["device_pid_conflicts"] = conflicts
            item["device_evidence_intervals"] = intervals
            if conflicts and target_identity_strong and intervals:
                representative = max(
                    intervals.values(),
                    key=lambda interval: float(interval[1]) - float(interval[0]),
                )
                item["evidence_interval"] = list(representative)
            else:
                item.pop("evidence_interval", None)
                item["confidence"] = "none"
                item["severity"] = "info"
                item["evidence_window_valid"] = False
                item["summary"] = "R400 争抢证据未绑定到目标 workload 的设备和 PID 树，不能进入 R500 传导链。"
                item.setdefault("missing_evidence", []).append("目标 PID/进程树参与目标设备争抢的强身份同窗证据")
            projected.append(item)
            continue
        if finding.get("rule_id") != "R100":
            projected.append(finding)
            continue
        item = copy.deepcopy(finding)
        host_interval = _finding_evidence_interval(item)

        def _target_overlap(device: dict) -> tuple[float, float] | None:
            if host_interval is None:
                return None
            for mapping_interval in target_mapping_intervals.get(_canonical_dev(str(device.get("device") or "")), []):
                if intervals_have_common_overlap(host_interval, mapping_interval):
                    return (
                        max(host_interval[0], mapping_interval[0]),
                        min(host_interval[1], mapping_interval[1]),
                    )
            return None

        assessed = [
            device
            for device in (item.get("assessed_devices") or [])
            if isinstance(device, dict)
            and device.get("device") in target_devices
            and _target_overlap(device) is not None
        ]
        saturated = [
            device
            for device in (item.get("saturated_devices") or [])
            if isinstance(device, dict)
            and device.get("device") in target_devices
            and _target_overlap(device) is not None
        ]
        overlap_intervals = [overlap for device in assessed if (overlap := _target_overlap(device)) is not None]
        target_health_complete = bool(assessed) and all(
            device.get("health_evidence_complete") is True for device in assessed
        )
        item["assessed_devices"] = assessed
        item["saturated_devices"] = saturated
        item["evidence_window_valid"] = bool(
            item.get("evidence_window_valid")
            and target_identity_strong
            and target_health_complete
            and bool(overlap_intervals)
        )
        if overlap_intervals:
            item["evidence_interval"] = list(max(overlap_intervals, key=lambda interval: interval[1] - interval[0]))
        else:
            item.pop("evidence_interval", None)
        if saturated:
            item["confidence"] = "high" if any(device.get("level") == "sustained" for device in saturated) else "medium"
            item["severity"] = "high" if item["confidence"] == "high" else "medium"
        elif assessed:
            item["severity"] = "info"
            if not target_health_complete:
                item["confidence"] = "low"
                item["summary"] = (
                    "目标 workload 映射设备的 util/queue/await 字段覆盖不足，不能高置信排除 Host IO 压力。"
                )
                item.setdefault("missing_evidence", []).append("目标设备完整的 util + queue/await 指标")
            elif finding.get("confidence") == "high":
                item["confidence"] = "high"
                item["summary"] = "目标 workload 映射设备在有效窗口内未检测到 IO 饱和。"
            else:
                item["confidence"] = str(finding.get("confidence")) if finding.get("confidence") in _ORDER else "none"
                item["summary"] = (
                    "目标 workload 映射设备在当前窗口内未检测到 IO 饱和，但原始证据不足以高置信排除偶发 Host IO 压力。"
                )
        else:
            item["confidence"] = "none"
            item["severity"] = "info"
            item["summary"] = "iostat 未覆盖目标 workload 映射设备，Host IO 状态未知。"
            item.setdefault("missing_evidence", []).append(f"目标设备 iostat 指标：{sorted(target_devices)}")
        projected.append(item)
    return projected


def _host_io_ruled_out(findings: list[dict]) -> bool:
    """是否有足量窗口证据明确排除设备级 Host IO 压力。"""
    if any(_is_confirmed_host_issue(f) for f in findings if isinstance(f, dict)):
        return False
    r100 = next(
        (f for f in findings if isinstance(f, dict) and f.get("rule_id") == "R100"),
        None,
    )
    r200 = next(
        (f for f in findings if isinstance(f, dict) and f.get("rule_id") == "R200"),
        None,
    )
    r100_clear = bool(
        r100
        and r100.get("confidence") == "high"
        and r100.get("severity") == "info"
        and r100.get("evidence_window_valid") is True
        and not r100.get("saturated_devices")
    )
    # 有网络挂载时，缺 mountstats delta 不能由“本地盘健康”推导 Host IO 正常。
    r200_clear = bool(r200 and not r200.get("confirmed_mounts"))
    if r200_clear and r200.get("network_mounts"):
        r200_clear = bool(
            r200.get("performance_window_evaluated") is True and r200.get("evidence_window_valid") is True
        )
    elif r200_clear:
        r200_clear = r200.get("performance_window_evaluated") is True
    return r100_clear and r200_clear


def _profile_overlaps_finding(profile: dict, finding: dict | None) -> bool:
    """Require the profile to overlap a finding's actual dynamic evidence window."""
    if not isinstance(finding, dict):
        return False
    return intervals_have_common_overlap(_profile_interval(profile), _finding_evidence_interval(finding))


def _host_io_ruled_out_in_profile_window(findings: list[dict], profile: dict) -> bool:
    """Return whether negative Host IO evidence applies to this profile window."""
    if not _host_io_ruled_out(findings):
        return False
    r100 = next(
        (f for f in findings if isinstance(f, dict) and f.get("rule_id") == "R100"),
        None,
    )
    if not _profile_overlaps_finding(profile, r100):
        return False
    r200 = next(
        (f for f in findings if isinstance(f, dict) and f.get("rule_id") == "R200"),
        None,
    )
    if not isinstance(r200, dict):
        return False
    scope = str(r200.get("nfs_metric_required_scope") or "")
    relevant_network_mount = bool(r200.get("network_mounts")) and not scope.endswith("_non_nfs")
    return not relevant_network_mount or _profile_overlaps_finding(profile, r200)


def analyze_r500_with_host(snapshot: dict, profile: dict, host_findings: list[dict] | bool) -> dict:
    """R500 public API; Host findings are always re-derived from the snapshot.

    ``host_findings`` is retained for call compatibility but is never trusted. A caller
    cannot certify R500 by supplying a hand-written R100-R400 finding.

    确定性 analyzer 不重建或验证 profiler artifact；任何 JSON-only R500 结论
    置信度封顶 medium。可信 artifact verifier 实现前，不声称已认证的传导链。
    """
    # Public callers may invoke this rule directly. Normalize the snapshot/profile and
    # recompute every Host rule so supplied findings cannot forge certifying evidence.
    snapshot, profile, _validation_errors, _profile_errors, fatal = validate_analysis_request(snapshot, profile)
    finding = _finding(
        "R500",
        "medium",
        "none",
        "",
        next_checks=[
            "采集 profiler 的 device Free/GPU idle 比例与 step 空泡段",
            "做对照实验：本地缓存/降低 IO 并发，观察空泡是否同步下降",
        ],
    )
    if fatal is not None:
        finding.update(
            confidence="none",
            severity="info",
            summary=f"Snapshot 输入不可分析：{fatal}",
            missing_evidence=["合法且资源受限的 IO Snapshot JSON"],
        )
        return finding
    del host_findings
    actual_r100 = analyze_r100(snapshot)
    assessments = [
        actual_r100,
        analyze_r200(snapshot),
        analyze_r300(snapshot),
        analyze_r400(snapshot, actual_r100),
    ]
    assessments = _project_host_assessments_to_target(snapshot, assessments)
    target_binding_certified = _target_binding_is_certified(snapshot)
    finding["target_binding_certified"] = target_binding_certified
    confirmed_hosts = [f for f in assessments if _is_confirmed_host_issue(f)]
    has_host_io_issue = bool(confirmed_hosts)
    host_ruled_out = _host_io_ruled_out(assessments)
    host_ruled_out_same_window = _host_io_ruled_out_in_profile_window(assessments, profile)
    host_rules = [f.get("rule_id", "") for f in confirmed_hosts]
    profile_host_rules = _profile_host_overlap_rules(profile, confirmed_hosts)
    profile_snapshot_overlap = _profile_window_matches_snapshot(snapshot, profile)
    host_ruled_out_same_window = host_ruled_out_same_window and profile_snapshot_overlap
    if not profile_snapshot_overlap:
        profile_host_rules = []
    host_profile_overlap = bool(profile_host_rules)

    mte2 = profile.get("mte2_ratio")
    device_free_pct = profile.get("device_free_percent")
    certified_metrics = _certified_profile_metrics(profile)
    finding["certified_profile_metrics"] = certified_metrics
    mte2_certified = "mte2_ratio" in certified_metrics
    device_free_certified = "device_free_percent" in certified_metrics
    # JSON can describe overlap or an experiment, but cannot prove that the referenced
    # artifacts were inspected. Keep supplied context visible without certifying it.
    conduction = profile.get("conduction_evidence")
    unverified_conduction_evidence: list[str] = []
    if isinstance(conduction, dict):
        if conduction.get("io_npu_overlap_observed") is True:
            unverified_conduction_evidence.append("timeline_overlap")
        if isinstance(conduction.get("controlled_experiment"), dict):
            unverified_conduction_evidence.append("controlled_experiment")
    finding["certified_conduction_evidence"] = []
    if unverified_conduction_evidence:
        finding["unverified_conduction_evidence"] = unverified_conduction_evidence
    if has_host_io_issue and _profile_interval(profile) is not None:
        finding["profile_host_overlap_rules"] = profile_host_rules

    # 反例：高 mte2 + Host IO 正常 → 转交计算分析。
    if mte2 is not None and mte2 >= 0.3 and not has_host_io_issue:
        if host_ruled_out_same_window and mte2_certified and target_binding_certified:
            confidence = "medium"
            host_statement = "有效窗口内未发现设备级 Host IO 压力"
            missing: list[str] = []
        elif host_ruled_out_same_window:
            confidence = "medium"
            gaps = []
            missing = []
            if not mte2_certified:
                gaps.append("mte2_ratio 的 profiler 来源未认证")
                missing.append(
                    "profile.profile_window.scope + profile.provenance.mte2_ratio（认证 profiler database 来源）"
                )
            if not target_binding_certified:
                gaps.append("目标 workload 身份未认证")
            host_statement = f"Host IO 负向证据同窗，但{'、'.join(gaps)}"
        elif host_ruled_out:
            confidence = "low"
            host_statement = "Host IO 负向证据与 profiler 不同窗，不能确认 profiler 窗口内正常"
            missing = ["profile.profile_window 与 Host IO 负向 evidence_interval 的足量公共交集"]
        else:
            confidence = "low"
            host_statement = "Host IO 证据不足，尚不能确认正常或异常"
            missing = [
                "R100 有效设备窗口（iostat 或递增 diskstats）",
                "必要时补充 R200/R300/R400 证据",
            ]
        mte2_provenance_requirement = (
            "profile.profile_window.scope + profile.provenance.mte2_ratio（认证 profiler database 来源）"
        )
        if not mte2_certified and mte2_provenance_requirement not in missing:
            missing.append(mte2_provenance_requirement)
        if not target_binding_certified:
            missing.append("显式 target.pid/path 及其强身份设备或当前 NFS 挂载映射")
        finding.update(
            confidence=confidence,
            severity="info",
            summary=(
                f"mte2_ratio={mte2} 偏高；{host_statement}。MTE2 是 AI Core 内部数据搬运，"
                f"不代表 Host 存储供给不足，应转交 ascend-computation-analysis 分析算子数据搬运。"
            ),
            handoff="ascend-computation-analysis",
            evidence_fields=(
                [
                    "profile.mte2_ratio",
                    "profile.profile_window.scope",
                    "profile.provenance.mte2_ratio",
                ]
                if mte2_certified
                else ["profile.mte2_ratio"]
            ),
            missing_evidence=missing,
        )
        return finding

    host_desc = (
        f"Host IO 压力链成立（根因：{', '.join(host_rules)}）"
        if host_rules
        else (
            "Host IO 压力链成立"
            if has_host_io_issue
            else ("Host IO 压力已排除" if host_ruled_out else "Host IO 状态未知")
        )
    )

    if device_free_pct is None:
        finding.update(
            confidence="none",
            severity="info",
            summary=f"{host_desc}，但缺少 profiler 数据，无法验证设备侧传导链，置信度降低。",
            missing_evidence=[
                "profile.device_free_percent",
                "profile.dataloader_wait",
                "profile.step_idle_ratio",
                "profile.conduction_evidence",
            ],
        )
        return finding

    # 没有已确认 Host IO 根因时，R500 不得输出 severity>=medium 的正向传导 finding。
    # device Free 高更可能来自 CPU 预处理/调度/通信/同步，应转交对应 skill，而非推荐 IO 缓存。
    if not has_host_io_issue:
        if host_ruled_out_same_window and device_free_certified and target_binding_certified:
            summary = (
                f"有效窗口内未发现设备级 Host IO 压力，device Free={device_free_pct}% 的空泡"
                f"更可能来自 CPU 预处理/调度/通信/同步，转交对应 skill。"
            )
            missing = []
            confidence = "medium"
        elif host_ruled_out_same_window:
            gaps = []
            missing = []
            if not device_free_certified:
                gaps.append("profiler scope/来源未认证")
                missing.append(
                    "profile.profile_window.scope + profile.provenance.device_free_percent（认证 profiler timeline/DB 来源）"
                )
            if not target_binding_certified:
                gaps.append("目标 workload 身份未认证")
            summary = (
                f"Host IO 负向证据与 device Free={device_free_pct}% 同窗，但"
                f"{'、'.join(gaps)}，不能高置信排除存储；先补齐认证。"
            )
            confidence = "medium"
        elif host_ruled_out:
            summary = (
                f"Host IO 负向证据与 device Free={device_free_pct}% 的 profiler 窗口不同窗，"
                "不能据此排除存储；先补同窗采集，再检查 CPU/调度/通信。"
            )
            missing = ["profile.profile_window 与 Host IO 负向 evidence_interval 的足量公共交集"]
            confidence = "low"
        else:
            summary = (
                f"Host IO 证据不足，不能把 device Free={device_free_pct}% 归因于或排除存储；"
                f"先补采集，再并行检查 CPU/调度/通信。"
            )
            missing = [
                "R100 有效设备窗口（iostat 或递增 diskstats）",
                "R200/R300/R400 与 workload 同窗证据",
            ]
            confidence = "none"
        device_free_provenance_requirement = (
            "profile.profile_window.scope + profile.provenance.device_free_percent（认证 profiler timeline/DB 来源）"
        )
        if not device_free_certified and device_free_provenance_requirement not in missing:
            missing.append(device_free_provenance_requirement)
        if not target_binding_certified:
            missing.append("显式 target.pid/path 及其强身份设备或当前 NFS 挂载映射")
        finding.update(
            confidence=confidence,
            severity="info",
            summary=summary,
            evidence_fields=(
                [
                    "profile.device_free_percent",
                    "profile.profile_window.scope",
                    "profile.provenance.device_free_percent",
                ]
                if device_free_certified
                else ["profile.device_free_percent"]
            ),
            missing_evidence=missing,
            handoff="mindstudio-cpu-binding / ascend-schedule-analysis / ascend-communication-analysis",
            recommended_next_checks=[
                "检查 CPU 预处理是否占满（pidstat -u）→ mindstudio-cpu-binding",
                "检查调度/下发 Host Bound（step_trace_time）→ ascend-schedule-analysis",
                "检查通信 allreduce/HCCL → ascend-communication-analysis",
            ],
        )
        return finding

    if device_free_pct >= 10 and has_host_io_issue:
        missing = []
        limitations = []
        evidence_fields = ["profile.device_free_percent"]
        if unverified_conduction_evidence:
            missing.append("可信 profiler artifact verifier（核验 timeline/实验工件内容）")
            limitations.append("传导证据已提供但未经可信工件核验")
            evidence_fields.append("profile.conduction_evidence")
        else:
            missing.extend(
                [
                    "profile.conduction_evidence.io_npu_overlap_observed（Host IO 异常区间与 device Free/step 空泡的同窗相关性）",
                    "profile.conduction_evidence.controlled_experiment（本地缓存/降并发后空泡是否同步下降）",
                ]
            )
            limitations.append("未提供同窗相关性/对照实验证据")
        if not host_profile_overlap:
            missing.append("profile.profile_window 与至少一个已确认 Host finding.evidence_interval 的足量公共交集")
        if not device_free_certified:
            missing.append(
                "profile.profile_window.scope + profile.provenance.device_free_percent（认证 profiler timeline/DB 来源）"
            )
            limitations.append("profiler scope/来源未认证")
        if not target_binding_certified:
            missing.append("显式 target.pid/path 及其强身份设备或当前 NFS 挂载映射")
            limitations.append("目标 workload 身份未认证")
        limitation_text = "、".join(limitations) or "认证证据不完整"
        finding.update(
            confidence="medium",
            severity="medium",
            summary=(
                f"{host_desc}，device Free={device_free_pct}%，IO 压力可能传导到设备侧空泡，"
                f"但{limitation_text}，置信度封顶 medium（不声称已传导）。"
            ),
            evidence_fields=evidence_fields,
            missing_evidence=missing,
        )
    elif device_free_pct < 5 and has_host_io_issue:
        if host_profile_overlap and device_free_certified and target_binding_certified:
            finding.update(
                confidence="medium",
                severity="info",
                summary=(
                    f"{host_desc}，JSON-only profile 报告 device Free={device_free_pct}%，"
                    "未观察到明显设备空泡；但未经可信工件核验，"
                    "不能据此确认 R500 传导链未成立或降低存储问题优先级。"
                ),
                evidence_fields=[
                    "profile.device_free_percent",
                    "profile.profile_window.scope",
                    "profile.provenance.device_free_percent",
                ],
                missing_evidence=["可信 profiler artifact verifier（核验 timeline 中的设备空泡区间）"],
            )
        elif host_profile_overlap:
            missing = []
            limitations = []
            if not device_free_certified:
                missing.append(
                    "profile.profile_window.scope + profile.provenance.device_free_percent（认证 profiler timeline/DB 来源）"
                )
                limitations.append("profiler scope/来源未认证")
            if not target_binding_certified:
                missing.append("显式 target.pid/path 及其强身份设备或当前 NFS 挂载映射")
                limitations.append("目标 workload 身份未认证")
            finding.update(
                confidence="medium",
                severity="medium",
                summary=(
                    f"{host_desc}，device Free={device_free_pct}% 与 Host IO 证据同窗，"
                    f"但{'、'.join(limitations)}，不能据此降低存储问题优先级。"
                ),
                evidence_fields=["profile.device_free_percent"],
                missing_evidence=missing,
            )
        else:
            finding.update(
                confidence="medium",
                severity="medium",
                summary=(
                    f"{host_desc}，但 device Free={device_free_pct}% 与 Host IO 证据不同窗，"
                    "不能据此降低存储问题优先级。"
                ),
                evidence_fields=["profile.device_free_percent"],
                missing_evidence=["profile.profile_window 与已确认 Host finding.evidence_interval 的足量公共交集"],
            )
    else:
        finding.update(
            confidence="medium",
            summary=f"{host_desc}，device Free={device_free_pct}%，需结合具体空泡段判断。",
        )
    return finding
