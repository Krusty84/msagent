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
"""Network storage and remote-file rules R200/R300."""

from __future__ import annotations

import math
from typing import Any

from .common import (
    _f,
    _finding,
    _parsed,
    _provider,
    _provider_interval,
    _status,
    intervals_overlap_or_are_adjacent,
)

from .contract import (
    _target_pid_scope,
)

from .local import (
    _collect_disks,
)

from .path_scope import (
    _canonicalize_path,
    _is_data_relevant_path,
)

_IOPS_HIGH = 5000.0  # 小 IO IOPS 参考（R300 small-IO 候选用）


_GLUSTER_SMALL_READ_SYSCALLS_PER_SECOND = 500.0


def _norm_nfs_source(s: Any) -> str:
    """规范化 NFS source（host:export）：host 小写，path 去 trailing slash。"""
    if not isinstance(s, str) or not s:
        return ""
    if ":" in s:
        host, _, path = s.partition(":")
        normalized_path = path.rstrip("/") or ("/" if path.startswith("/") else "")
        return f"{host.strip().lower()}:{normalized_path}"
    return s.strip().lower()


def _norm_fstype_group(ft: Any) -> str:
    """将 nfs/nfs4 视为同一兼容组。"""
    ft = str(ft or "").strip().lower()
    return "nfs" if ft in ("nfs", "nfs4") else ft


def _nfs_identity(item: dict, source_key: str = "device") -> tuple[str, str, str]:
    """Return normalized (source, mount_point, fstype) identity for an NFS mount/metric."""
    return (
        _norm_nfs_source(item.get(source_key)),
        _canonicalize_path(str(item.get("mount_point") or "")),
        _norm_fstype_group(item.get("fstype")),
    )


def _nfs_identity_dict(identity: tuple[str, str, str]) -> dict:
    source, mount_point, fstype = identity
    return {"source": source, "mount_point": mount_point, "fstype": fstype}


def _path_under_mount(path: str, mount_point: str) -> bool:
    p = _canonicalize_path(path)
    mp = _canonicalize_path(mount_point)
    if not p or not mp:
        return False
    return p == mp or p.startswith(mp.rstrip("/") + "/")


def _required_nfs_identities(
    snapshot: dict, nfs_mounts: list[dict]
) -> tuple[set[tuple[str, str, str]], str]:
    """Return NFS identities that must be covered before Host NFS can be ruled out.

    If a target path or process_io_map identifies the workload's NFS mount, require that
    target scope. Otherwise require every current NFS mount, because a healthy metric from
    one mount says nothing about another same-window NFS mount.
    """
    current = {_nfs_identity(m) for m in nfs_mounts if isinstance(m, dict)}
    current = {ident for ident in current if ident[1] and ident[2] == "nfs"}

    target = snapshot.get("target")
    target_path = target.get("path") if isinstance(target, dict) else None
    specific_target = isinstance(target_path, str) and _canonicalize_path(
        target_path
    ) not in ("", "/", ".")
    if specific_target:
        all_mounts = [
            mount
            for mount in (snapshot.get("mounts") or [])
            if isinstance(mount, dict)
            and _path_under_mount(target_path, str(mount.get("mount_point") or ""))
        ]
        if all_mounts:
            # Resolve the effective mount from all filesystems first. A deeper local
            # bind/overmount must shadow an NFS parent. For equal mount points, the
            # later /proc/mounts entry represents the newer stacked mount.
            _, chosen = max(
                enumerate(all_mounts),
                key=lambda item: (
                    len(_canonicalize_path(str(item[1].get("mount_point") or ""))),
                    item[0],
                ),
            )
            if _norm_fstype_group(chosen.get("fstype")) == "nfs":
                ident = _nfs_identity(chosen)
                if ident in current:
                    return {ident}, "target_path"
        return set(), "target_path_non_nfs"

    if not current:
        return set(), "none"

    provider = _provider(snapshot, "process_io_map")
    parsed = _parsed(provider)
    mapped: set[tuple[str, str, str]] = set()
    relevant_mappings = 0
    target_pid_scope = _target_pid_scope(snapshot)
    if _status(provider) == "ok" and isinstance(parsed, dict):
        for mapping in parsed.get("mappings", []) or []:
            if not isinstance(mapping, dict):
                continue
            if (
                target_pid_scope is not None
                and mapping.get("pid") not in target_pid_scope
            ):
                continue
            if not _is_data_relevant_path(mapping.get("path"), None):
                continue
            relevant_mappings += 1
            if _norm_fstype_group(mapping.get("fstype")) != "nfs":
                continue
            ident = _nfs_identity(mapping, source_key="source")
            if ident in current:
                mapped.add(ident)
    if mapped:
        return mapped, "target_process_io_map"
    if relevant_mappings:
        return set(), "target_process_io_map_non_nfs"
    if target_pid_scope is not None:
        # An explicit PID is a hard workload boundary. Missing, empty, or unrelated
        # process mappings cannot be replaced by every NFS mount visible on the host.
        return set(), "target_process_io_map_unresolved"

    return current, "all_current_nfs_mounts"


def _bind_nfs_metrics(
    metrics: list[dict], nfs_mounts: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """按 (source, mount_point, fstype) 身份绑定 NFS metric 到当前挂载。

    返回 (strong, weak, unmatched)：
      - strong：source+mount_point+fstype 完全匹配某当前 NFS 挂载（nfs/nfs4 兼容）→ 可 high。
      - weak：metric 无 source，仅 mount_point+fstype 匹配 → 不得单独 high。
      - unmatched：mount_point 不在任何当前 NFS 挂载中 → 忽略，避免旧/混入 metric 拼接。
    """
    # 当前 NFS 挂载身份集合（nfs/nfs4 兼容）
    identities = {_nfs_identity(m) for m in nfs_mounts if isinstance(m, dict)}
    mp_set = {mp for (_, mp, _) in identities}
    strong, weak, unmatched = [], [], []
    for mm in metrics:
        if not isinstance(mm, dict):
            continue
        mp = _canonicalize_path(str(mm.get("mount_point") or ""))
        ft = _norm_fstype_group(mm.get("fstype"))
        src = _norm_nfs_source(mm.get("source"))
        if mp not in mp_set:
            unmatched.append(mm)
            continue
        if src:
            # source 存在 → 严格要求三元身份完全匹配；仅 nfs/nfs4 由
            # _norm_fstype_group 显式归为同一兼容组。不得用同 source/path
            # 绕过 fstype，否则旧 namespace/CIFS metric 会被拼成 NFS 强证据。
            if any(s == src and m == mp and f == ft for (s, m, f) in identities):
                strong.append(mm)
            else:
                unmatched.append(mm)
        else:
            # source 缺失 → 弱匹配（仅 mount_point）
            weak.append(mm)
    return strong, weak, unmatched


def _is_glusterfs_mount(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and str(item.get("fstype") or "").lower() == "fuse.glusterfs"
    )


def _gluster_identity(item: dict, source_key: str = "device") -> tuple[str, str, str]:
    return (
        str(item.get(source_key) or "").strip(),
        _canonicalize_path(str(item.get("mount_point") or "")),
        str(item.get("fstype") or "").lower(),
    )


def _required_gluster_mounts(
    snapshot: dict, gluster_mounts: list[dict]
) -> tuple[list[dict], str]:
    """Bind GlusterFS evidence to an explicit target path or target PID tree."""
    target = snapshot.get("target") or {}
    target_path = target.get("path") if isinstance(target, dict) else None
    if isinstance(target_path, str) and target_path:
        all_matches = [
            mount
            for mount in (snapshot.get("mounts") or [])
            if isinstance(mount, dict)
            and _path_under_mount(target_path, str(mount.get("mount_point") or ""))
        ]
        if not all_matches:
            return [], "target_path_non_glusterfs"
        _, chosen = max(
            enumerate(all_matches),
            key=lambda item: (
                len(_canonicalize_path(str(item[1].get("mount_point") or ""))),
                item[0],
            ),
        )
        if not _is_glusterfs_mount(chosen):
            return [], "target_path_non_glusterfs"
        identity = _gluster_identity(chosen)
        current = {_gluster_identity(mount): mount for mount in gluster_mounts}
        return (
            ([current[identity]], "target_path_glusterfs")
            if identity in current
            else ([], "target_path_non_glusterfs")
        )

    target_pid = target.get("pid") if isinstance(target, dict) else None
    if (
        isinstance(target_pid, int)
        and not isinstance(target_pid, bool)
        and target_pid > 0
    ):
        process_map = _provider(snapshot, "process_io_map")
        parsed = _parsed(process_map) if _status(process_map) == "ok" else None
        if not isinstance(parsed, dict):
            return [], "target_process_io_map_unresolved"
        allowed_pids = _target_pid_scope(snapshot)
        if not allowed_pids:
            return [], "target_process_io_map_unresolved"
        current = {_gluster_identity(mount): mount for mount in gluster_mounts}
        selected: dict[tuple[str, str, str], dict] = {}
        relevant_mappings = 0
        for mapping in parsed.get("mappings", []) or []:
            if not isinstance(mapping, dict) or mapping.get("pid") not in allowed_pids:
                continue
            if not _is_data_relevant_path(mapping.get("path"), None):
                continue
            relevant_mappings += 1
            if not _is_glusterfs_mount(mapping):
                continue
            identity = _gluster_identity(mapping, source_key="source")
            if identity in current:
                selected[identity] = current[identity]
        if selected:
            return list(selected.values()), "target_process_io_map_glusterfs"
        if relevant_mappings:
            return [], "target_process_io_map_non_glusterfs"
        return [], "target_process_io_map_unresolved"

    return gluster_mounts, "all_current_glusterfs_mounts"


def _bind_gluster_metrics(
    metrics: list[dict], mounts: list[dict]
) -> tuple[list[dict], list[dict]]:
    required = {_gluster_identity(mount) for mount in mounts}
    matched: list[dict] = []
    unmatched: list[dict] = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        identity = _gluster_identity(metric, source_key="source")
        if identity in required:
            matched.append(metric)
        else:
            unmatched.append(metric)
    return matched, unmatched


def _gluster_activity(metric: dict, duration: float) -> dict[str, Any] | None:
    process_io = metric.get("process_io")
    if not isinstance(process_io, dict):
        return None
    values = [
        process_io.get("rchar", 0),
        process_io.get("read_bytes", 0),
        process_io.get("syscr", 0),
        process_io.get("stable_pid_count", 0),
    ]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        return None
    rchar = float(values[0])
    read_bytes = float(values[1])
    syscr = float(values[2])
    stable_pids = values[3]
    if rchar <= 0 and read_bytes <= 0 and syscr <= 0:
        return None
    seconds = max(0.001, duration)
    return {
        "mount_point": metric.get("mount_point"),
        "source": metric.get("source"),
        "stable_pid_count": stable_pids,
        "rchar_delta": round(rchar, 3),
        "read_bytes_delta": round(read_bytes, 3),
        "read_syscalls_delta": round(syscr, 3),
        "read_syscalls_per_second": round(syscr / seconds, 3),
        "avg_rchar_per_syscall": round(rchar / syscr, 3) if syscr else None,
        "scope_note": "target process-tree counters; not per-mount latency",
    }


def _analyze_gluster_r200(snapshot: dict, net_mounts: list[dict]) -> dict | None:
    """First-class GlusterFS FUSE branch; NFS remains an auxiliary branch."""
    gluster_mounts = [mount for mount in net_mounts if _is_glusterfs_mount(mount)]
    if not gluster_mounts:
        return None
    required, scope = _required_gluster_mounts(snapshot, gluster_mounts)
    if scope.endswith("_non_glusterfs"):
        return None

    finding = _finding(
        "R200",
        "info",
        "none",
        "",
        next_checks=[
            "采集同窗口 GlusterFS client/brick 延迟、错误与重试统计",
            "做本地盘与 GlusterFS 的同模型、同 batch、同 worker 对照实验",
        ],
        network_storage_primary="fuse.glusterfs",
        glusterfs_metric_required_scope=scope,
        performance_confirmed=False,
        performance_window_evaluated=False,
        activity_window_evaluated=False,
        client_performance_evaluated=False,
    )
    finding["evidence_fields"] = ["mounts.fstype"]
    finding["network_mounts"] = [
        {
            "device": mount.get("device"),
            "mount_point": mount.get("mount_point"),
            "fstype": mount.get("fstype"),
        }
        for mount in net_mounts
    ]
    finding["glusterfs_required_mounts"] = [
        {
            "source": mount.get("device"),
            "mount_point": mount.get("mount_point"),
            "fstype": mount.get("fstype"),
        }
        for mount in required
    ]
    if scope.endswith("_unresolved"):
        finding.update(
            confidence="none",
            summary=(
                "检测到 GlusterFS FUSE 网络存储，但显式目标 PID 无法绑定到当前挂载，"
                "不能把全机网络存储活动归因给目标 workload。"
            ),
            missing_evidence=["目标 PID/进程树到 GlusterFS 挂载的同窗路径映射"],
            performance_window_evaluated=False,
            evidence_window_valid=False,
        )
        return finding

    gluster_pr = _provider(snapshot, "glusterfs")
    parsed = _parsed(gluster_pr) if _status(gluster_pr) == "ok" else None
    metrics = parsed.get("mount_metrics", []) if isinstance(parsed, dict) else []
    matched, unmatched = _bind_gluster_metrics(metrics or [], required)
    windowed = [metric for metric in matched if metric.get("windowing") == "delta"]
    interval = _provider_interval(snapshot, "glusterfs")
    interval_valid = intervals_overlap_or_are_adjacent(
        _provider_interval(snapshot, "mounts_provider"), interval
    )
    duration = 0.0
    if interval is not None:
        duration = max(0.0, float(interval[1]) - float(interval[0]))
    activities = [
        activity
        for metric in windowed
        if (activity := _gluster_activity(metric, duration)) is not None
    ]
    # /proc process-tree deltas establish target activity only. They do not
    # evaluate Gluster client/brick latency, retries, or network performance.
    finding["activity_window_evaluated"] = bool(windowed) and interval_valid
    finding["performance_window_evaluated"] = False
    finding["evidence_window_valid"] = interval_valid
    if interval_valid and interval is not None:
        finding["evidence_interval"] = list(interval)
    if activities:
        finding.update(
            severity="medium",
            confidence="medium" if interval_valid else "low",
            summary=(
                f"目标数据路径位于 GlusterFS FUSE 主网络存储，窗口内检测到 "
                f"{len(activities)} 个目标挂载对应的进程树读取活动；"
                "但缺少 Gluster 客户端/brick 延迟与重试指标，当前只能确认活动和候选压力，"
                "不能确认网络传输瓶颈。"
            ),
            glusterfs_activity=activities,
            missing_evidence=[
                "同窗口 GlusterFS client/brick 操作延迟、错误和重试统计",
                "本地盘与 GlusterFS 的受控吞吐/数据等待对照",
            ],
        )
        finding["evidence_fields"].append("glusterfs.mount_metrics.process_io")
    else:
        finding.update(
            severity="info",
            confidence="low",
            summary=(
                "目标数据路径位于 GlusterFS FUSE 主网络存储，但当前窗口缺少可绑定的"
                "目标进程读取活动或客户端性能指标，不能确认网络存储瓶颈。"
            ),
            missing_evidence=[
                "glusterfs.mount_metrics 的目标进程树同窗读取增量",
                "同窗口 GlusterFS client/brick 操作延迟、错误和重试统计",
            ],
        )
    if unmatched:
        finding.setdefault("handoff_notes", []).append(
            f"{len(unmatched)} 个 GlusterFS metric 与当前目标挂载身份不匹配，已忽略。"
        )
    finding.setdefault("handoff_notes", []).append(
        "进程 /proc/<pid>/io 是活动线索，不是 per-mount RTT 或元数据延迟，不能单独升级为 high。"
    )
    return finding


def analyze_r200(snapshot: dict) -> dict:
    """R200 网络存储 / 挂载延迟。

    拆两层：(a) 识别为网络挂载（仅类型，非瓶颈）；
            (b) 确认瓶颈（必须有 RTT/execute/retrans/major-timeout 性能证据）。
    仅 (a) 成立而 (b) 缺证据时，confidence=low 并列入 missing_evidence。

    覆盖范围：GlusterFS FUSE 是首选网络存储路径，支持目标挂载与目标进程树
    活动的同窗绑定；NFS 保留 mountstats RTT/execute/retrans 的高置信确认。
    CIFS/Lustre/GPFS/BeeGFS/Ceph 继续识别并转交专用 provider。
    """
    finding = _finding(
        "R200",
        "high",
        "none",
        "",
        next_checks=[
            "采集 /proc/self/mountstats 的 per-mount RTT/execute/retrans（NFS）",
            "做本地盘 vs 网络挂载的同负载对照实验",
        ],
    )
    mounts_provider = _provider(snapshot, "mounts_provider")
    mounts_status = _status(mounts_provider)
    if mounts_status not in {"ok", "empty"}:
        finding.update(
            confidence="none",
            severity="info",
            summary=(
                "挂载信息采集失败或缺少来源状态，无法判断当前 workload 是否使用网络存储。"
            ),
            evidence_fields=["mounts_provider.status"],
            missing_evidence=["成功采集的 mounts_provider 与 mounts 列表"],
            performance_window_evaluated=False,
            evidence_window_valid=False,
        )
        return finding
    if mounts_status == "empty":
        finding.update(
            confidence="none",
            severity="info",
            summary=("挂载列表采集为空，无法确认当前 workload 未使用网络存储。"),
            evidence_fields=["mounts_provider.status"],
            missing_evidence=["非空且与 Snapshot 同窗的 mounts_provider/mounts 列表"],
            performance_window_evaluated=False,
            evidence_window_valid=False,
        )
        return finding
    mounts_interval_valid = _provider_interval(snapshot, "mounts_provider") is not None

    mounts = snapshot.get("mounts", []) or []
    net_mounts = [
        m
        for m in mounts
        if isinstance(m, dict)
        and (
            str(m.get("fstype", "")).lower()
            in {"nfs", "nfs4", "cifs", "lustre", "gpfs", "beegfs", "ceph"}
            or str(m.get("fstype", "")).lower().startswith("fuse.")
        )
    ]

    if not net_mounts:
        finding["severity"] = "info"
        finding["evidence_fields"] = ["mounts_provider.status", "mounts"]
        finding["performance_window_evaluated"] = mounts_interval_valid
        finding["evidence_window_valid"] = mounts_interval_valid
        if mounts_interval_valid:
            finding["confidence"] = "high"
            finding["summary"] = "未识别到网络挂载（nfs/cifs/lustre/gpfs/fuse）。"
        else:
            finding["confidence"] = "none"
            finding["summary"] = (
                "挂载列表缺少当前采集窗口的时间来源，不能确认 workload 未使用网络存储。"
            )
            finding["missing_evidence"] = [
                "mounts_provider started_at/ended_at overlapping snapshot.window"
            ]
        return finding

    gluster_finding = _analyze_gluster_r200(snapshot, net_mounts)
    if gluster_finding is not None:
        return gluster_finding

    # GlusterFS 目标已在上方优先处理；这里保留 NFS 与其他辅助网络存储路径。
    nfs_mounts = [m for m in net_mounts if _norm_fstype_group(m.get("fstype")) == "nfs"]
    non_nfs_mounts = [m for m in net_mounts if m not in nfs_mounts]

    finding["evidence_fields"] = ["mounts.fstype"]
    finding["network_mounts"] = [
        {
            "device": m.get("device"),
            "mount_point": m.get("mount_point"),
            "fstype": m.get("fstype"),
        }
        for m in net_mounts
    ]
    if non_nfs_mounts:
        finding["non_nfs_identified"] = [
            {"mount_point": m.get("mount_point"), "fstype": m.get("fstype")}
            for m in non_nfs_mounts
        ]

    # 性能证据：nfs.mount_metrics 的窗内 avg_rtt/avg_execute/retrans
    nfs_pr = _provider(snapshot, "nfs")
    metrics = []
    if (
        mounts_status in {"ok", "empty"}
        and _status(nfs_pr) == "ok"
        and isinstance(_parsed(nfs_pr), dict)
    ):
        metrics = (_parsed(nfs_pr) or {}).get("mount_metrics", []) or []

    # NFS 性能证据按 (source, mount_point, fstype) 身份绑定到当前挂载。
    #   strong（身份完全匹配）→ 可 high；weak（无 source 仅路径匹配）→ 不得单独 high；
    #   unmatched（路径不在当前挂载）→ 忽略，避免旧/混入 metric 拼接为因果链。
    strong, weak, unmatched_metrics = _bind_nfs_metrics(metrics, nfs_mounts)
    # 只有当前采样窗的差值才是性能证据。cumulative/缺失/非法窗口只保留为
    # 背景信息，不能把开机以来的历史累计值归因给当前 workload。
    all_windowed = [mm for mm in strong if mm.get("windowing") == "delta"]
    non_windowed = [mm for mm in strong if mm.get("windowing") != "delta"]
    required_identities, required_scope = _required_nfs_identities(snapshot, nfs_mounts)
    target_nfs_irrelevant = required_scope.endswith("_non_nfs")
    target_nfs_unresolved = required_scope.endswith("_unresolved")
    if required_identities:
        windowed = [
            mm
            for mm in all_windowed
            if _nfs_identity(mm, source_key="source") in required_identities
        ]
    elif target_nfs_irrelevant or target_nfs_unresolved:
        windowed = []
    else:
        windowed = all_windowed
    metrics = windowed
    covered_identities = {
        _nfs_identity(mm, source_key="source")
        for mm in windowed
        if isinstance(mm, dict)
    }
    missing_identities = required_identities - covered_identities
    finding["nfs_metric_required_scope"] = required_scope
    finding["nfs_metric_required_mounts"] = [
        _nfs_identity_dict(ident) for ident in sorted(required_identities)
    ]
    finding["nfs_metric_covered_mounts"] = [
        _nfs_identity_dict(ident)
        for ident in sorted(covered_identities & required_identities)
    ]
    finding["nfs_metric_missing_mounts"] = [
        _nfs_identity_dict(ident) for ident in sorted(missing_identities)
    ]
    if target_nfs_unresolved:
        finding.update(
            confidence="none",
            severity="info",
            summary=(
                "显式 target.pid 未解析到当前设备或 NFS 挂载，不能把全机 NFS 指标归因给目标 workload。"
            ),
            missing_evidence=["目标 PID/进程树到当前 NFS 挂载或块设备的可靠同窗映射"],
            performance_window_evaluated=False,
            evidence_window_valid=False,
        )
        return finding
    finding["performance_window_evaluated"] = mounts_interval_valid and (
        target_nfs_irrelevant
        or (bool(required_identities) and not missing_identities and not non_nfs_mounts)
    )
    if target_nfs_irrelevant:
        finding["evidence_window_valid"] = mounts_interval_valid
        finding.setdefault("handoff_notes", []).append(
            "Target workload is not on an NFS mount; current NFS mounts are not used to rule target Host IO in or out."
        )
    elif windowed:
        nfs_interval = _provider_interval(snapshot, "nfs")
        finding["evidence_window_valid"] = intervals_overlap_or_are_adjacent(
            _provider_interval(snapshot, "mounts_provider"), nfs_interval
        )
        if finding["evidence_window_valid"] and nfs_interval is not None:
            finding["evidence_interval"] = list(nfs_interval)
    has_weak_only = bool(weak) and not strong
    if missing_identities:
        finding.setdefault("handoff_notes", []).append(
            f"{len(missing_identities)} 个当前相关 NFS 挂载缺少同窗 delta mountstats，"
            "不能用其他 NFS 挂载的健康指标排除 Host/NFS 侧问题。"
        )
    if unmatched_metrics:
        finding.setdefault("handoff_notes", []).append(
            f"{len(unmatched_metrics)} 个 NFS metric 的 mount_point 不在当前挂载列表中（或 source 不匹配），已忽略（避免拼接证据）。"
        )
    if weak:
        finding.setdefault("handoff_notes", []).append(
            f"{len(weak)} 个 NFS metric 缺 source，仅路径弱匹配，不参与 high 判定。"
        )
    if non_windowed:
        finding.setdefault("handoff_notes", []).append(
            f"{len(non_windowed)} 个 NFS metric 不是有效 delta 窗口，已排除出 high 判定；请重新进行两次同窗采样。"
        )

    confirmed = []
    # 性能判据使用重传比率 + 最小样本 + 延迟阈值，不以单次重传判 high。
    # 这些阈值是参考线而非通用真理，应配合对照实验与设备基线。
    _MIN_OPS = 200.0  # 最小窗内操作样本量（低于此统计意义不足）
    _RTT_HIGH_MS = 50.0  # 平均 RTT 高（健康 LAN NFS 通常 <10ms）
    _EXEC_HIGH_MS = 100.0  # 平均 execute 高
    _RETRANS_RATIO_HIGH = 0.01  # 重传率 ≥1%（且 ops 充足）
    _MAJOR_TIMEOUT_ANY = 1  # 任意 major timeout 即强信号
    invalid_counter_metrics = 0
    for mm in metrics:
        # 兼容新键（avg_rtt_ms/avg_execute_ms/retrans_ratio）与历史键
        rtt = mm.get("avg_rtt_ms", mm.get("rtt")) or 0
        execute = mm.get("avg_execute_ms", mm.get("execute")) or 0
        retrans = mm.get("retrans") or 0
        ops = mm.get("ops") or 0
        # transmissions 缺失/为 0 时无法算比率（不伪造分母，避免假 100% 重传）
        tx_raw = mm.get("transmissions")
        major_raw = mm.get("major_timeouts")
        major = major_raw or 0
        try:
            rtt = float(rtt)
            execute = float(execute)
            retrans = float(retrans)
            ops = float(ops)
            tx = float(tx_raw) if tx_raw is not None else 0.0
            major = float(major)
        except (TypeError, ValueError, OverflowError):
            rtt = execute = retrans = ops = tx = major = 0.0
        # 与 nfs-utils/nfsiostat 一致：重传次数 / 原始操作请求数。
        integral_request_counters = all(
            math.isfinite(value) and value.is_integer() for value in (ops, tx, retrans)
        )
        counter_consistent = (
            tx_raw is not None
            and integral_request_counters
            and ops >= 0
            and retrans >= 0
            and tx >= ops
            and math.isclose(retrans, tx - ops, rel_tol=1e-6, abs_tol=1e-6)
        )
        major_timeout_consistent = (
            major_raw is not None
            and counter_consistent
            and math.isfinite(major)
            and major.is_integer()
            and 0 <= major <= ops
        )
        if not counter_consistent or (
            major_raw is not None and not major_timeout_consistent
        ):
            invalid_counter_metrics += 1
        retrans_ratio = (retrans / ops) if ops > 0 and counter_consistent else 0.0
        sufficient_ops = ops >= _MIN_OPS
        latency_bad = rtt >= _RTT_HIGH_MS or execute >= _EXEC_HIGH_MS
        retrans_bad = (
            sufficient_ops
            and counter_consistent
            and retrans_ratio >= _RETRANS_RATIO_HIGH
        )
        # 确认判据：延迟高（需充足样本）或重传率达标（需充足样本）或
        # 与有效请求 delta 一致的 major timeout。不能由 ops=0 的孤立计数伪造。
        major_timeout_bad = major_timeout_consistent and major >= _MAJOR_TIMEOUT_ANY
        confirmed_flag = (
            (latency_bad and sufficient_ops) or retrans_bad or major_timeout_bad
        )
        if confirmed_flag:
            confirmed.append(
                {
                    **mm,
                    "avg_rtt_ms": rtt,
                    "avg_execute_ms": execute,
                    "retrans": retrans,
                    "ops": ops,
                    "retrans_ratio": retrans_ratio,
                    "major_timeouts": major,
                }
            )

    if invalid_counter_metrics:
        finding.setdefault("handoff_notes", []).append(
            f"{invalid_counter_metrics} 个 NFS metric 的请求/重传/major-timeout "
            "计数缺失、不一致或非整数，相关性能证据已忽略。"
        )

    if confirmed:
        nfs_interval = _provider_interval(snapshot, "nfs")
        nfs_interval_valid = intervals_overlap_or_are_adjacent(
            _provider_interval(snapshot, "mounts_provider"),
            nfs_interval,
        )
        finding["evidence_window_valid"] = nfs_interval_valid
        if nfs_interval_valid and nfs_interval is not None:
            finding["evidence_interval"] = list(nfs_interval)
        finding["confidence"] = "high" if nfs_interval_valid else "medium"
        finding["severity"] = "high" if nfs_interval_valid else "medium"
        finding["summary"] = (
            f"识别到 {len(nfs_mounts)} 个 NFS 挂载，且有 {len(confirmed)} 个出现性能异常"
            f"（窗内 RTT/execute 偏高、重传率≥1% 或 major timeout）。"
        )
        finding["evidence_fields"].append("nfs.mount_metrics")
        finding["confirmed_mounts"] = confirmed
        if not nfs_interval_valid:
            finding.setdefault("missing_evidence", []).append(
                "mounts_provider and nfs windows overlapping or adjacent within snapshot.window"
            )
            finding.setdefault("handoff_notes", []).append(
                "NFS metric claims delta window but provider time interval is missing or stale; capped at medium."
            )
    else:
        finding["confidence"] = "low"
        finding["severity"] = "medium"
        finding["summary"] = (
            f"识别到 {len(nfs_mounts)} 个 NFS 挂载"
            + (f"、{len(non_nfs_mounts)} 个非 NFS 网络挂载" if non_nfs_mounts else "")
            + "，但缺少达标的 NFS 性能证据"
            "（需充足样本下的高 RTT/execute、≥1% 重传率或 major timeout），"
            "仅能确认挂载类型，不能确认网络存储瓶颈。"
        )
        finding["missing_evidence"] = [
            "nfs.mount_metrics（/proc/self/mountstats 解析的窗内 RTT/execute/retrans_ratio）",
            "nfsiostat per-mount 延迟",
            "本地盘 vs 网络挂载对照实验",
        ]
        if not mounts_interval_valid:
            finding["missing_evidence"].append(
                "mounts_provider started_at/ended_at overlapping snapshot.window"
            )
        if has_weak_only:
            # source 缺失的 metric 只能弱匹配，需补 mountstats 设备字段，
            # 便于强身份绑定后再次确认；置信度仍不 high。
            finding["confidence"] = "medium"
            finding.setdefault("handoff_notes", []).append(
                "存在缺 source 的 NFS metric（仅路径弱匹配），无法做 (source,mount_point,fstype) "
                "身份强绑定；建议确认 mountstats 设备字段后重新分析。"
            )
    # 非 NFS 网络存储仅支持识别与人工指导。
    if non_nfs_mounts:
        nn = ", ".join(sorted({str(m.get("fstype")) for m in non_nfs_mounts}))
        finding.setdefault("handoff_notes", []).append(
            f"非 NFS 网络存储（{nn}）仅识别类型，自动性能确认未实现："
            f"CIFS 用 cifsiostat/`/proc/fs/cifs/Stats`；Lustre 用 `lctl get_param osc.*.stats`；"
            f"GPFS/BeeGFS 用各自客户端统计。需人工采集后判定。"
        )
    return finding


def _gluster_small_read_candidates(snapshot: dict) -> list[dict[str, Any]]:
    """Return target-scoped small-read candidates without claiming metadata latency."""
    gluster_pr = _provider(snapshot, "glusterfs")
    parsed = _parsed(gluster_pr) if _status(gluster_pr) == "ok" else None
    if not isinstance(parsed, dict):
        return []
    current_mounts = [
        mount for mount in (snapshot.get("mounts") or []) if _is_glusterfs_mount(mount)
    ]
    required, scope = _required_gluster_mounts(snapshot, current_mounts)
    if (
        not required
        or scope.endswith("_unresolved")
        or scope.endswith("_non_glusterfs")
    ):
        return []
    metrics = parsed.get("mount_metrics") or []
    matched, _unmatched = _bind_gluster_metrics(metrics, required)
    interval = _provider_interval(snapshot, "glusterfs")
    if not intervals_overlap_or_are_adjacent(
        _provider_interval(snapshot, "mounts_provider"), interval
    ):
        return []
    if interval is None:
        return []
    duration = max(0.001, float(interval[1]) - float(interval[0]))
    candidates: list[dict[str, Any]] = []
    for metric in matched:
        if metric.get("windowing") != "delta":
            continue
        activity = _gluster_activity(metric, duration)
        if activity is None:
            continue
        syscall_rate = _f(activity.get("read_syscalls_per_second"))
        average_size = _f(activity.get("avg_rchar_per_syscall"))
        if (
            syscall_rate >= _GLUSTER_SMALL_READ_SYSCALLS_PER_SECOND
            and 0 < average_size < 16 * 1024
        ):
            candidates.append(
                {
                    "mount_point": activity.get("mount_point"),
                    "source": activity.get("source"),
                    "read_syscalls_per_second": round(syscall_rate, 3),
                    "avg_rchar_per_syscall": round(average_size, 3),
                    "evidence_scope": "target process tree; not per-mount metadata latency",
                }
            )
    return candidates


def analyze_r300(snapshot: dict) -> dict:
    """R300 远程文件访问 / 元数据 / 小文件开销。

    证据强度：
      - **强（可确认）**：NFS mountstats 中元数据 op（GETATTR/LOOKUP/READDIR/...）
        窗内平均 RTT/execute 偏高——直接反映 open/stat/lookup 的远程访问耗时。
      - 中（候选）：目标 GlusterFS 进程树的小读取 syscall，或 iostat/diskstats
        delta 小 IO 特征（高 IOPS + 低平均 IO 大小）。
      - 背景（不单独产生根因）：df inode 使用率（容量信号，非延迟证据）。
    """
    finding = _finding(
        "R300",
        "info",
        "none",
        "",
        next_checks=[
            "统计数据集文件数量与平均文件大小",
            "第二次访问是否变快（判断 cache 命中）",
            "strace 抽样 openat/stat/getdents 频率（需确认，开销较高）",
        ],
    )

    # 强证据：远程元数据 op 延迟（GETATTR/LOOKUP/READDIR/...）
    nfs_pr = _provider(snapshot, "nfs")
    mounts_status = _status(_provider(snapshot, "mounts_provider"))
    meta_slow = []
    if (
        mounts_status in {"ok", "empty"}
        and _status(nfs_pr) == "ok"
        and isinstance(_parsed(nfs_pr), dict)
    ):
        # 元数据证据与 R200 共用身份绑定逻辑。
        nfs_mounts_for_bind = [
            m
            for m in (snapshot.get("mounts") or [])
            if isinstance(m, dict) and _norm_fstype_group(m.get("fstype")) == "nfs"
        ]
        all_mm = (_parsed(nfs_pr) or {}).get("mount_metrics", []) or []
        strong_mm, _weak_mm, _unmatched = _bind_nfs_metrics(all_mm, nfs_mounts_for_bind)
        required_identities, required_scope = _required_nfs_identities(
            snapshot, nfs_mounts_for_bind
        )
        target_nfs_irrelevant = required_scope.endswith("_non_nfs")
        finding["nfs_metadata_required_scope"] = required_scope
        finding["nfs_metadata_required_mounts"] = [
            _nfs_identity_dict(ident) for ident in sorted(required_identities)
        ]
        if required_identities:
            strong_mm = [
                mm
                for mm in strong_mm
                if _nfs_identity(mm, source_key="source") in required_identities
            ]
        elif target_nfs_irrelevant:
            strong_mm = []
        for mm in strong_mm:
            if mm.get("windowing") != "delta":
                continue
            m_ops = mm.get("metadata_ops") or 0
            try:
                m_ops = float(m_ops)
                avg_rtt = mm.get("avg_metadata_rtt_ms")
                avg_exec = mm.get("avg_metadata_execute_ms")
                m_rtt = (
                    float(avg_rtt)
                    if avg_rtt is not None
                    else float(mm.get("metadata_sum_rtt_ms") or 0) / m_ops
                    if m_ops > 0
                    else 0.0
                )
                m_exec = (
                    float(avg_exec)
                    if avg_exec is not None
                    else float(mm.get("metadata_sum_execute_ms") or 0) / m_ops
                    if m_ops > 0
                    else 0.0
                )
            except (TypeError, ValueError, OverflowError):
                m_ops = m_rtt = m_exec = 0.0
            # 元数据延迟参考线：单次 getattr/lookup > 10ms 在 LAN NFS 上已偏高。
            if m_ops >= 50 and (m_rtt >= 10 or m_exec >= 20):
                meta_slow.append(
                    {
                        "mount_point": mm.get("mount_point"),
                        "source": mm.get("source"),
                        "fstype": mm.get("fstype"),
                        "metadata_ops": m_ops,
                        "avg_metadata_rtt_ms": m_rtt,
                        "avg_metadata_execute_ms": m_exec,
                    }
                )

    # 中证据：目标 GlusterFS 进程树与设备级小 IO 特征
    gluster_small_reads = _gluster_small_read_candidates(snapshot)
    disks, disk_source = _collect_disks(snapshot)
    small_io_disks = []
    for d in disks:
        r_per_s = _f(d.get("r_per_s"))
        rkB = _f(d.get("rkB_per_s"))
        avg_io_kb = (rkB / r_per_s) if r_per_s > 0 else 0
        if r_per_s >= _IOPS_HIGH and avg_io_kb < 16:
            small_io_disks.append(
                {
                    "device": d.get("name"),
                    "r_per_s": round(r_per_s, 1),
                    "avg_io_kb": round(avg_io_kb, 2),
                }
            )

    # 背景信息：inode 使用率（容量信号，非延迟证据，不单独产生根因）
    df_pr = _provider(snapshot, "df")
    high_inode = []
    if _status(df_pr) == "ok" and isinstance(_parsed(df_pr), dict):
        for fs in (_parsed(df_pr) or {}).get("filesystems", []):
            pct = fs.get("iuse_percent", "")
            try:
                pct_n = float(pct.rstrip("%")) if isinstance(pct, str) else float(pct)
                if pct_n >= 80:
                    high_inode.append(
                        {"mount": fs.get("mounted_on"), "iuse_percent": pct}
                    )
            except (ValueError, TypeError, OverflowError):
                continue

    finding["evidence_fields"] = []
    if meta_slow:
        finding["evidence_fields"].append("nfs.mount_metrics（元数据 op 延迟）")
    if gluster_small_reads:
        finding["evidence_fields"].append(
            "glusterfs.mount_metrics.process_io（目标进程树小读取候选）"
        )
    if small_io_disks:
        finding["evidence_fields"].append(f"{disk_source}.disks（小 IO 特征）")
    if high_inode:
        finding["evidence_fields"].append("df.filesystems（inode 使用·背景）")

    parts: list[str] = []
    if meta_slow:
        nfs_interval = _provider_interval(snapshot, "nfs")
        nfs_interval_valid = intervals_overlap_or_are_adjacent(
            _provider_interval(snapshot, "mounts_provider"),
            nfs_interval,
        )
        finding["evidence_window_valid"] = nfs_interval_valid
        if nfs_interval_valid and nfs_interval is not None:
            finding["evidence_interval"] = list(nfs_interval)
        finding["confidence"] = "high" if nfs_interval_valid else "medium"
        finding["severity"] = "high" if nfs_interval_valid else "medium"
        parts.append(
            f"{len(meta_slow)} 个网络挂载的元数据 op（GETATTR/LOOKUP/READDIR）窗内延迟偏高"
        )
        finding["metadata_slow_mounts"] = meta_slow
        if not nfs_interval_valid:
            finding.setdefault("missing_evidence", []).append(
                "mounts_provider and nfs windows overlapping or adjacent within snapshot.window"
            )
            finding.setdefault("handoff_notes", []).append(
                "NFS metadata metric claims delta window but provider time interval is missing or stale; capped at medium."
            )
    if small_io_disks:
        if finding["confidence"] == "none":
            finding["confidence"] = "medium"
            finding["severity"] = "medium"
        parts.append(
            f"{len(small_io_disks)} 个设备呈现小文件特征（高 IOPS、低平均 IO 大小）"
        )
        finding["small_io_devices"] = small_io_disks
    if gluster_small_reads:
        if finding["confidence"] == "none":
            finding["confidence"] = "medium"
            finding["severity"] = "medium"
        parts.append(
            f"{len(gluster_small_reads)} 个 GlusterFS 目标挂载呈现高频小读取候选"
        )
        finding["glusterfs_small_read_candidates"] = gluster_small_reads
    if high_inode:
        # inode 仅作背景信息输出，不改变 confidence（容量信号非延迟证据）。
        finding["high_inode_fs"] = high_inode
        parts.append(f"{len(high_inode)} 个文件系统 inode 使用率 >= 80%（背景信息）")

    if not meta_slow:
        # 小 IO 与 inode 都只是候选/背景信号，不能替代元数据延迟或 syscall
        # 观测。始终指出将候选升级为已确认根因还缺什么。
        finding["missing_evidence"] = [
            "GlusterFS client/brick 元数据操作延迟，或 NFS mountstats 的 GETATTR/LOOKUP/READDIR 窗内延迟",
            "openat/stat/getdents syscall 频率或延迟",
            "page cache 命中率或第二次访问延迟对照",
        ]

    if parts:
        finding["summary"] = "；".join(parts) + "。"
    else:
        finding["confidence"] = "low"
        finding["summary"] = (
            "缺少远程文件访问/元数据开销的直接证据（元数据 op 延迟、syscall 频率、cache 命中）。"
        )
    return finding
