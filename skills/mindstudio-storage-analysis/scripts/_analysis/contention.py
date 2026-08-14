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
"""Cross-process IO contention rule R400."""

from __future__ import annotations

from collections import defaultdict
import re

from .common import (
    _canonical_dev,
    _finding,
    _format_device_pid_map,
    _parse_iso,
    _parsed,
    _provider,
    _provider_interval,
    _status,
    intervals_have_common_overlap,
)

from .contract import (
    _strict_json_int,
)

from .local import (
    _MIN_SAMPLES_HIGH,
    analyze_r100,
)

from .path_scope import (
    _is_data_relevant_path,
)

_NON_CONTENTION_FSTYPES = {
    "autofs",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "ramfs",
    "securityfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}


_NON_CONTENTION_DEVICES = {
    "autofs",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "ramfs",
    "securityfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}


def _is_contention_storage_mapping(mapping: dict, devices: set[str]) -> bool:
    """Return whether a process mapping can represent storage contention.

    R400 should not count tmpfs/proc/sysfs/overlay/container helper mounts as shared
    storage devices. Keep mappings with a real block/network storage identity, and
    only skip virtual mounts when they have no real backing device.
    """
    fstype = str(mapping.get("fstype") or "").lower()
    if fstype.startswith("fuse.") and fstype != "fusectl":
        return True
    if fstype in _NON_CONTENTION_FSTYPES:
        return bool(devices - _NON_CONTENTION_DEVICES)
    source = str(mapping.get("source") or "").lower()
    if source in _NON_CONTENTION_DEVICES:
        return bool(devices - _NON_CONTENTION_DEVICES)
    return bool(devices)


def _active_io_pids(procs: list[dict], total_reports: int) -> set[int]:
    """Return PIDs with sustained IO in a majority of pidstat reports."""
    if not isinstance(total_reports, int) or isinstance(total_reports, bool) or total_reports < _MIN_SAMPLES_HIGH:
        return set()
    out: set[int] = set()
    for p in procs:
        if not isinstance(p, dict):
            continue
        try:
            sample_count = _strict_json_int(p.get("sample_count"))
            active_sample_count = _strict_json_int(p.get("active_sample_count"))
            pid = _strict_json_int(p.get("pid"), positive=True)
        except ValueError:
            continue
        counts_valid = (
            0 <= active_sample_count <= sample_count <= total_reports
            and active_sample_count >= _MIN_SAMPLES_HIGH
            and active_sample_count * 2 > total_reports
        )
        if counts_valid:
            out.add(pid)
    return out


def _mapping_observation_interval(
    mapping: dict, provider_interval: tuple[float, float] | None
) -> tuple[float, float] | None:
    """Return a mapping's validated first/last observation interval."""
    if provider_interval is None:
        return None
    start = _parse_iso(mapping.get("first_seen"))
    end = _parse_iso(mapping.get("last_seen"))
    if start is None or end is None or end <= start:
        return None
    if start < provider_interval[0] - 2.0 or end > provider_interval[1] + 2.0:
        return None
    clipped = max(start, provider_interval[0]), min(end, provider_interval[1])
    return clipped if clipped[1] > clipped[0] else None


def _common_mapping_interval(entries: list[dict], pids: set[int]) -> tuple[float, float] | None:
    """Find a time segment where every candidate PID had a mapping observation."""
    if not pids:
        return None
    groups: list[list[tuple[float, float]]] = []
    for pid in sorted(pids):
        intervals = [
            entry["mapping_interval"]
            for entry in entries
            if entry.get("pid") == pid
            and entry.get("mapping_observation_count", 0) >= 2
            and entry.get("mapping_interval") is not None
        ]
        if not intervals:
            return None
        groups.append(intervals)

    common = groups[0]
    for group in groups[1:]:
        intersections: list[tuple[float, float]] = []
        for left in common:
            for right in group:
                overlap = max(left[0], right[0]), min(left[1], right[1])
                if overlap[1] > overlap[0]:
                    intersections.append(overlap)
        if not intersections:
            return None
        common = intersections
    return max(common, key=lambda interval: interval[1] - interval[0])


def analyze_r400(snapshot: dict, r100_finding: dict | None = None) -> dict:
    """R400 多 rank / 多 worker / 多实例 IO 干扰。

    高置信度需**同时**满足以下条件，避免共享根盘/打开 FD 误报：
      1. ≥2 个不同 PID 映射到同一设备；
      2. 至少一条映射路径"数据相关"（排除共享库/日志/解释器/系统文件）；
      3. 这些 PID 在 pidstat 中有活跃 IO；
      4. 该设备在 R100 中饱和；
      5. 每个 PID 的同一条强身份映射实际观测至少两次，且映射区间同窗。
    任一缺失则降级为 medium/low/none，并在 missing_evidence 说明缺哪项。
    """
    if r100_finding is None:
        r100_finding = analyze_r100(snapshot)

    finding = _finding(
        "R400",
        "info",
        "none",
        "",
        next_checks=[
            "提供 --pid 或 --path，建立 rank/worker → PID → 设备映射",
            "做单卡 vs 多卡对照，观察加卡后是否变慢",
            "每 rank 用独立 shard，观察争抢是否消失",
        ],
    )

    pmap_pr = _provider(snapshot, "process_io_map")
    pmap_parsed: dict = {}
    mappings: list[dict] = []
    if _status(pmap_pr) == "ok" and isinstance(_parsed(pmap_pr), dict):
        pmap_parsed = _parsed(pmap_pr) or {}
        mappings = pmap_parsed.get("mappings", []) or []
    raw_pmap_partial = pmap_parsed.get("partial")
    pmap_partial = (
        raw_pmap_partial
        if isinstance(raw_pmap_partial, list)
        else ["process_io_map partial coverage is malformed"]
        if raw_pmap_partial
        else []
    )
    try:
        pmap_observation_samples = _strict_json_int(pmap_parsed.get("observation_samples"), positive=True)
    except ValueError:
        pmap_observation_samples = 0
    pmap_interval = _provider_interval(snapshot, "process_io_map")

    pidstat_pr = _provider(snapshot, "pidstat")
    procs: list[dict] = []
    if _status(pidstat_pr) == "ok" and isinstance(_parsed(pidstat_pr), dict):
        pidstat_parsed = _parsed(pidstat_pr) or {}
        procs = pidstat_parsed.get("processes", []) or []
    else:
        pidstat_parsed = {}
    try:
        pidstat_reports = _strict_json_int(pidstat_parsed.get("reports"), positive=True)
    except ValueError:
        pidstat_reports = 0
    active_pids = _active_io_pids(procs, pidstat_reports)

    # 饱和设备集合（用 canonical 设备名，与 mapping 的 canonical_device 对齐）
    saturated_devs: set[str] = set()
    if r100_finding and r100_finding.get("confidence") == "high":
        for s in r100_finding.get("saturated_devices", []) or []:
            if not isinstance(s, dict) or s.get("level") != "sustained":
                continue
            dev = s.get("device")
            if dev:
                saturated_devs.add(_canonical_dev(str(dev)))

    if not mappings:
        # 弱推断：多进程有 IO 活动但无映射
        if len(active_pids) >= 2:
            finding["confidence"] = "low"
            finding["severity"] = "medium"
            finding["evidence_fields"] = ["pidstat.processes"]
            finding["summary"] = (
                f"检测到 {len(active_pids)} 个进程有活跃 IO，但缺少 PID→设备映射，"
                f"无法确认是否抢同一设备（需 --pid/--path 建立映射）。"
            )
            finding["missing_evidence"] = ["process_io_map.mappings（PID→设备映射）"]
        else:
            finding["confidence"] = "none"
            finding["summary"] = "缺少进程级 IO 与 PID→设备映射证据。"
            finding["missing_evidence"] = [
                "pidstat.processes",
                "process_io_map.mappings",
            ]
        return finding

    # 按 canonical_device 聚合（/dev/sda1→sda，/dev/mapper/*→dm-* 等）。

    dev_to_entries: dict[str, list[dict]] = defaultdict(list)
    weak_identity_warning = False
    skipped_virtual_mounts = 0
    target_obj = snapshot.get("target")
    target_path = target_obj.get("path") if isinstance(target_obj, dict) else None
    for m in mappings:
        canonical = m.get("canonical_device")
        if not isinstance(canonical, str):
            canonical = _canonical_dev(
                m.get("source")
                if isinstance(m.get("source"), str)
                else (m.get("device") if isinstance(m.get("device"), str) else "")
            )
        backing_raw = m.get("backing_devices")
        backing = (
            [_canonical_dev(device) for device in backing_raw if isinstance(device, str)]
            if isinstance(backing_raw, list)
            else []
        )
        topology = {device for device in [canonical, *backing] if device}
        # iostat 可能报告 dm/LVM 逻辑设备，也可能报告底层盘。两层都保留为候选，
        # 后续饱和判定仍严格绑定当前候选名，不能把一个 topology 成员的压力传播给另一个。
        contention_devices = topology
        if not contention_devices:
            # 无法归一到真实设备（如 source 为空/nfs 等非块设备），不计入争抢候选
            continue
        if not _is_contention_storage_mapping(m, contention_devices):
            skipped_virtual_mounts += 1
            continue
        resolution = str(m.get("device_resolution") or "")
        if resolution != "sysfs":
            weak_identity_warning = True
        pid = m.get("pid")
        try:
            pid_i = _strict_json_int(pid, positive=True)
        except ValueError:
            pid_i = None
        path_relevant = _is_data_relevant_path(m.get("path"), target_path=target_path)
        try:
            mapping_observation_count = _strict_json_int(m.get("observation_count"))
        except ValueError:
            mapping_observation_count = 0
        boot_id = m.get("boot_id")
        raw_starttime = m.get("pid_starttime_ticks")
        process_identity_strong = bool(
            isinstance(boot_id, str)
            and re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                boot_id,
            )
            and isinstance(raw_starttime, int)
            and not isinstance(raw_starttime, bool)
            and raw_starttime >= 0
        )
        # 显式 target.path 是 R400 的作用域边界。collector 的 path_relevant 仅作原始
        # 观测，analyzer 必须重算并在候选聚合前过滤，避免无关 cwd/FD 形成噪声冲突。
        if isinstance(target_path, str) and target_path and not path_relevant:
            continue
        entry = {
            "pid": pid_i,
            "path": m.get("path"),
            "source": m.get("source"),
            "path_relevant": path_relevant,
            "active_io": (pid_i in active_pids) if pid_i is not None else None,
            "identity_strong": resolution == "sysfs",
            "process_identity_strong": process_identity_strong,
            "topology": sorted(topology),
            "mapping_identity": canonical,
            "mapping_interval": _mapping_observation_interval(m, pmap_interval),
            "mapping_observation_count": max(0, mapping_observation_count),
        }
        for device in contention_devices:
            device_entry = dict(entry)
            # 饱和证据必须绑定当前候选设备。某个私有 backing 或逻辑设备
            # 饱和，不能传播到同一 topology 中另一个健康的共享 backing。
            device_entry["device_saturated"] = device in saturated_devs
            dev_to_entries[device].append(device_entry)

    # 候选冲突：同 canonical 设备 ≥2 个不同 PID
    candidate_conflicts = {
        dev: entries
        for dev, entries in dev_to_entries.items()
        if len({e["pid"] for e in entries if e["pid"] is not None}) >= 2
    }

    if not candidate_conflicts:
        finding["confidence"] = "low"
        finding["severity"] = "info"
        finding["evidence_fields"] = ["process_io_map.mappings"]
        finding["summary"] = "已建立 PID→设备映射，但未观察到至少两个 PID 同时访问同一设备的证据。"
        finding["missing_evidence"] = ["至少两个目标 workload PID 的同设备映射"]
        if len(active_pids) < 2:
            finding["missing_evidence"].append("至少两个目标 workload PID 的同窗 pidstat 活跃 IO 样本")
        if skipped_virtual_mounts:
            finding["note"] = f"已忽略 {skipped_virtual_mounts} 条 tmpfs/overlay/proc/sysfs 等非持久伪文件系统映射。"
        return finding

    # 逐设备评估证据完整性；data_relevant 与 active_io 必须绑定到同一 PID。
    confirmed: dict[str, list] = {}
    causal_candidates: dict[str, list] = {}
    weak: dict[str, list] = {}
    mapping_overlap_by_device: dict[str, tuple[float, float]] = {}
    missing_reasons: list[str] = []
    pid_devices: dict[int, set[str]] = defaultdict(set)
    for dev, entries in dev_to_entries.items():
        for entry in entries:
            if entry["pid"] is not None and entry["path_relevant"]:
                pid_devices[entry["pid"]].add(entry.get("mapping_identity") or dev)
    for dev, entries in candidate_conflicts.items():
        pids = sorted({e["pid"] for e in entries if e["pid"] is not None})
        saturated = any(e["device_saturated"] for e in entries)
        # 关键：每个 PID 必须自己同时满足 path_relevant AND active_io，
        # 不能把 PID-A 的数据路径和 PID-B 的活跃 IO 拼接成因果链。
        strong_pids = {e["pid"] for e in entries if e["pid"] is not None and e["path_relevant"] and e["active_io"]}
        identity_pids = {e["pid"] for e in entries if e["pid"] in strong_pids and e["identity_strong"]}
        process_identity_pids = {e["pid"] for e in entries if e["pid"] in strong_pids and e["process_identity_strong"]}
        temporally_bound_entries = [
            e
            for e in entries
            if e["pid"] in strong_pids
            and e["path_relevant"]
            and e["active_io"]
            and e["identity_strong"]
            and e["process_identity_strong"]
            and 2 <= e["mapping_observation_count"] <= pmap_observation_samples
            and e["mapping_interval"] is not None
        ]
        temporally_bound_pids = {e["pid"] for e in temporally_bound_entries if e["pid"] is not None}
        mapping_overlap = _common_mapping_interval(temporally_bound_entries, temporally_bound_pids)
        temporal_mapping_ok = bool(
            pmap_observation_samples >= 2
            and len(temporally_bound_pids) >= 2
            and mapping_overlap is not None
            and mapping_overlap[1] - mapping_overlap[0] >= 1.0
        )
        if len(strong_pids) >= 2 and saturated:
            causal_candidates[dev] = sorted(strong_pids)
            unambiguous_pids = {pid for pid in temporally_bound_pids if len(pid_devices.get(pid, set())) == 1}
            if unambiguous_pids == temporally_bound_pids and temporal_mapping_ok:
                confirmed[dev] = sorted(temporally_bound_pids)
                mapping_overlap_by_device[dev] = mapping_overlap
            else:
                weak[dev] = pids
                if identity_pids != strong_pids:
                    missing_reasons.append(
                        f"{dev}: PID→设备身份缺少 sysfs 精确解析，不能用 heuristic/unknown 身份确认争抢"
                    )
                if process_identity_pids != strong_pids:
                    missing_reasons.append(f"{dev}: PID 身份缺少 boot_id + starttime 绑定，不能排除 PID 复用")
                if unambiguous_pids != temporally_bound_pids:
                    missing_reasons.append(
                        f"{dev}: pidstat 是每 PID 聚合 IO，PID 同时映射多个数据设备，无法归因到该设备"
                    )
                if not temporal_mapping_ok:
                    missing_reasons.append(f"{dev}: 每 PID 映射需实际观测至少两次，且观测区间须有至少 1s 公共交集")
        else:
            weak[dev] = pids
            if len(strong_pids) < 2:
                # 区分缺哪项，便于报告
                data_pids = {e["pid"] for e in entries if e["pid"] is not None and e["path_relevant"]}
                active_pid_set = {e["pid"] for e in entries if e["pid"] is not None and e["active_io"]}
                if len(data_pids) < 2:
                    missing_reasons.append(f"{dev}: 数据相关路径的 PID 不足（{sorted(data_pids)}）")
                if len(active_pid_set) < 2:
                    missing_reasons.append(f"{dev}: 有活跃 IO 的 PID 不足（{sorted(active_pid_set)}）")
                if data_pids and active_pid_set and not (data_pids & active_pid_set):
                    missing_reasons.append(f"{dev}: 数据路径与活跃 IO 分属不同 PID，不能拼接为同进程争抢")
            if not saturated:
                missing_reasons.append(f"{dev}: 设备未饱和（R100 未命中，争抢无实际影响）")

    if confirmed:
        if pmap_partial:
            finding["candidate_device_pid_conflicts"] = causal_candidates
            finding["confidence"] = "medium"
            finding["severity"] = "medium"
            finding["evidence_fields"] = [
                "process_io_map.mappings",
                "process_io_map.partial",
                "pidstat.processes",
                "R100.saturated_devices",
            ]
            finding["summary"] = (
                f"检测到 {len(confirmed)} 个多进程数据 IO 争抢候选，但 process_io_map 覆盖不完整，不能确认高置信争抢。"
            )
            finding["missing_evidence"] = ["完整 PID/FD 覆盖的 process_io_map（无 partial 截断或权限缺口）"]
            return finding
        # high 结论要求 iostat/pidstat/process_io_map 三者都有合法时间窗，且
        # 存在正长度公共交集。缺失/非法时间不是“无法证伪即同窗”，而是证据不足。
        r100_win_raw = r100_finding.get("evidence_interval") if r100_finding else None
        r100_win = tuple(r100_win_raw) if isinstance(r100_win_raw, (list, tuple)) and len(r100_win_raw) == 2 else None
        pidstat_win = _provider_interval(snapshot, "pidstat")
        pmap_win = pmap_interval
        device_evidence_intervals: dict[str, tuple[float, float]] = {}
        for dev in confirmed:
            mapping_win = mapping_overlap_by_device.get(dev)
            windows = (r100_win, pidstat_win, pmap_win, mapping_win)
            if not intervals_have_common_overlap(*windows):
                continue
            device_evidence_intervals[dev] = (
                max(float(window[0]) for window in windows if window is not None),
                min(float(window[1]) for window in windows if window is not None),
            )
        window_confirmed = {dev: pids for dev, pids in confirmed.items() if dev in device_evidence_intervals}
        finding["candidate_device_pid_conflicts"] = causal_candidates
        finding["evidence_fields"] = [
            "process_io_map.mappings",
            "pidstat.processes",
            "R100.saturated_devices",
        ]
        if window_confirmed:
            representative = max(
                device_evidence_intervals.values(),
                key=lambda interval: interval[1] - interval[0],
            )
            finding["evidence_interval"] = list(representative)
            finding["device_evidence_intervals"] = {
                dev: list(device_evidence_intervals[dev]) for dev in window_confirmed
            }
            finding["device_pid_conflicts"] = window_confirmed
            finding["evidence_window_valid"] = True
            finding["confidence"] = "high"
            finding["severity"] = "high"
            finding["summary"] = (
                f"检测到 {len(window_confirmed)} 个设备被多个进程同时、活跃地争抢数据 IO"
                f"（每 PID 均含同窗映射+数据路径+活跃 IO 且设备饱和）：" + _format_device_pid_map(window_confirmed)
            )
        else:
            finding["evidence_window_valid"] = False
            finding["confidence"] = "medium"
            finding["severity"] = "medium"
            finding["summary"] = (
                f"检测到 {len(confirmed)} 个设备存在多进程数据 IO 争抢候选，但 R100/pidstat/"
                f"process_io_map 采集时间窗不重叠，不能确认为同一时间因果链。"
            )
            finding["missing_evidence"] = finding.get("missing_evidence", []) + [
                "R100 实际数据源/pidstat/process_io_map 足量同窗采集",
            ]
        if weak_identity_warning:
            finding["note"] = "部分设备用启发式归一（无 /sys），canonical 身份可能不准。"
        return finding

    # 有候选但证据不全 → medium/low
    finding["confidence"] = "low"
    finding["severity"] = "medium"
    finding["evidence_fields"] = ["process_io_map.mappings"]
    if weak_identity_warning:
        finding["evidence_fields"].append("process_io_map.device_resolution=heuristic")
    finding["summary"] = (
        f"检测到 {len(weak)} 个设备被多个进程访问，但争抢证据不完整，不能确认为 IO 干扰瓶颈："
        + _format_device_pid_map(weak)
    )
    finding["candidate_conflicts"] = weak
    finding["missing_evidence"] = sorted(set(missing_reasons))[:6]
    if skipped_virtual_mounts:
        finding["note"] = f"已忽略 {skipped_virtual_mounts} 条 tmpfs/overlay/proc/sysfs 等非持久伪文件系统映射。"
    return finding
