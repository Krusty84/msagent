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
"""Mount, capacity, NFS, GlusterFS, and memory collection."""

from __future__ import annotations

import os
import re
import time
from typing import Any


from .common import (
    PSEUDO_FS,
    ProviderResult,
    STATUS_CMD_FAILED,
    STATUS_EMPTY,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    STATUS_PERMISSION,
    STATUS_UNSUPPORTED,
    _MAX_PROCESS_TREE_PIDS,
    _decode_mountinfo_octal,
    _have_cmd,
    _mount_namespace_key,
    _now_iso,
    _read_file,
    _run,
)

from .process import (
    _pid_starttime_ticks,
    _process_tree,
)


def collect_mounts(pid: int | None = None) -> ProviderResult:
    """目标 PID（默认 self）视角的 mounts，排除伪文件系统。"""
    started = _now_iso()
    proc_id = str(pid) if pid is not None else "self"
    content, err, status_int = _read_file(f"/proc/{proc_id}/mounts")
    if status_int == 1:
        return ProviderResult(
            source="mounts",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    if status_int == 2:
        return ProviderResult(
            source="mounts",
            status=STATUS_PERMISSION,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    if status_int != 0:
        return ProviderResult(
            source="mounts",
            status=STATUS_CMD_FAILED,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    mounts: list[dict[str, Any]] = []
    for line in content.splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        fstype = f[2]
        if fstype in PSEUDO_FS:
            continue
        mounts.append(
            {
                "device": _decode_mountinfo_octal(f[0]),
                "mount_point": _decode_mountinfo_octal(f[1]),
                "fstype": fstype,
                "options": _decode_mountinfo_octal(f[3]),
            }
        )
    status = STATUS_OK if mounts else STATUS_EMPTY
    return ProviderResult(
        source="mounts",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        parsed=mounts,
        raw=content,
    )


def collect_df(pid: int | None = None) -> ProviderResult:
    """df -hP（空间）+ df -iP（inode）分开采集并按挂载点合并。"""
    started = _now_iso()
    if pid is not None and _mount_namespace_key(pid) != _mount_namespace_key(None):
        return ProviderResult(
            source="df",
            status=STATUS_UNSUPPORTED,
            started_at=started,
            ended_at=_now_iso(),
            error="target PID mount namespace/root differs; self df output would be misleading",
        )
    if not _have_cmd("df"):
        return ProviderResult(
            source="df", status=STATUS_MISSING, started_at=started, ended_at=_now_iso()
        )
    # 失联 NFS 上 statfs 可能长期阻塞；每个 df 必须有界返回。
    ec1, out1, err1 = _run(["df", "-hP"], timeout=15)
    ec2, out2, err2 = _run(["df", "-iP"], timeout=15)
    if ec1 != 0 or ec2 != 0:
        return ProviderResult(
            source="df",
            status=STATUS_CMD_FAILED,
            started_at=started,
            ended_at=_now_iso(),
            exit_code=ec1 if ec1 != 0 else ec2,
            stderr=err1 + err2,
            error="one or more df commands failed",
            raw=out1 + "\n--- inodes ---\n" + out2,
            parsed={
                "filesystems": _parse_df(out1, inode=False)
                + _parse_df(out2, inode=True),
                "partial": [
                    message
                    for code, message in ((ec1, err1), (ec2, err2))
                    if code != 0 and message
                ],
            },
        )
    space = _parse_df(out1, inode=False)
    inodes = _parse_df(out2, inode=True)
    # 按 mounted_on 合并
    by_mp: dict[str, dict[str, Any]] = {}
    for row in space:
        by_mp[row["mounted_on"]] = row
    for row in inodes:
        mp = row["mounted_on"]
        if mp in by_mp:
            by_mp[mp]["inodes"] = row.get("inodes")
            by_mp[mp]["iuse_percent"] = row.get("iuse_percent")
        else:
            by_mp[mp] = row
    filesystems = list(by_mp.values())
    status = STATUS_OK if filesystems else STATUS_EMPTY
    return ProviderResult(
        source="df",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        exit_code=ec1,
        stderr=(err1 + err2) if (ec1 or ec2) else "",
        raw=out1 + "\n--- inodes ---\n" + out2,
        parsed={
            "filesystems": filesystems,
            "partial": [
                message
                for code, message in ((ec1, err1), (ec2, err2))
                if code != 0 and message
            ],
        },
    )


def _parse_df(text: str, *, inode: bool) -> list[dict[str, Any]]:
    """解析 df -P 输出。inode=True 时解析 -iP。"""
    rows: list[dict[str, Any]] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for line in lines[1:]:
        f = line.split()
        if len(f) < 6:
            continue
        src = f[0]
        if src in PSEUDO_FS:
            continue
        # POSIX -P 固定前 5 列，挂载点是剩余整段；不能只取最后一个 token。
        mp = " ".join(f[5:])
        if inode:
            rows.append(
                {
                    "filesystem": src,
                    "inodes": f[1],
                    "iused": f[2],
                    "iavail": f[3],
                    "iuse_percent": f[4],
                    "mounted_on": mp,
                }
            )
        else:
            rows.append(
                {
                    "filesystem": src,
                    "size": f[1],
                    "used": f[2],
                    "avail": f[3],
                    "use_percent": f[4],
                    "mounted_on": mp,
                }
            )
    return rows


def _parse_rpc_nfs(content: str) -> dict[str, float]:
    """解析 /proc/net/rpc/nfs 的 rpc 行（客户端累计：calls / retrans）。

    形如：rpc <calls> <retrans> <authrefrefresh> ... 。返回 {calls, retrans}。
    """
    out = {"calls": 0.0, "retrans": 0.0}
    for line in content.splitlines():
        f = line.split()
        if len(f) >= 3 and f[0] == "rpc":
            try:
                out["calls"] = float(f[1])
                out["retrans"] = float(f[2])
            except ValueError:
                pass
            break
    return out


def collect_nfs(duration: float, pid: int | None = None) -> ProviderResult:
    """NFS 客户端统计，**窗内两次采样求差**（mountstats/rpc 均为累计值）。

    数据源：
      - /proc/self/mountstats：per-mount per-op 累计统计（RTT/execute/ops/transmissions），
        两次采样差值得到本次 workload 窗口的 avg_rtt_ms/avg_execute_ms/retrans/ops。
      - /proc/net/rpc/nfs：客户端累计 calls/retrans，两次采样差值。
      - nfsiostat：可选单次补充（原始文本，非窗内）。
    无 NFS 挂载或文件不存在时标 unsupported/missing，绝不输出"正常"。
    """
    started = _now_iso()
    # 先看是否有 NFS 挂载
    mounts_pr = collect_mounts(pid)
    if mounts_pr.status not in (STATUS_OK, STATUS_EMPTY):
        return ProviderResult(
            source="nfs",
            status=mounts_pr.status,
            started_at=started,
            ended_at=_now_iso(),
            error=f"mount discovery failed: {mounts_pr.error or mounts_pr.status}",
        )
    has_nfs = False
    if isinstance(mounts_pr.parsed, list):
        has_nfs = any(
            str(m.get("fstype", "")).lower() in {"nfs", "nfs4"}
            for m in mounts_pr.parsed
        )
    if not has_nfs:
        return ProviderResult(
            source="nfs",
            status=STATUS_UNSUPPORTED,
            started_at=started,
            ended_at=_now_iso(),
            error="no nfs mount found",
        )

    # t0/t1 timestamps delimit the cumulative-counter evidence itself. Optional
    # post-processing such as nfsiostat must not inflate this causal window.
    evidence_started = _now_iso()
    # t0 采样
    proc_id = str(pid) if pid is not None else "self"
    mountstats_path = f"/proc/{proc_id}/mountstats"
    rpc_path = f"/proc/{proc_id}/net/rpc/nfs"
    ms0_content, ms0_err, ms0_st = _read_file(mountstats_path)
    rpc0_content, rpc0_err, rpc0_st = _read_file(rpc_path)
    ms0 = _parse_mountstats(ms0_content) if ms0_st == 0 else {}
    rpc0 = (
        _parse_rpc_nfs(rpc0_content) if rpc0_st == 0 else {"calls": 0.0, "retrans": 0.0}
    )

    time.sleep(max(0.1, duration))

    # t1 采样
    ms1_content, ms1_err, ms1_st = _read_file(mountstats_path)
    rpc1_content, rpc1_err, rpc1_st = _read_file(rpc_path)
    ms1 = _parse_mountstats(ms1_content) if ms1_st == 0 else {}
    rpc1 = (
        _parse_rpc_nfs(rpc1_content) if rpc1_st == 0 else {"calls": 0.0, "retrans": 0.0}
    )
    evidence_ended = _now_iso()

    parsed: dict[str, Any] = {}
    raw_parts: list[str] = []
    nfs_status = STATUS_OK

    mount_metrics: list[dict[str, Any]] = []
    if ms0_st == 0 and ms1_st == 0:
        samples_complete = all(
            sample
            and all(
                int(metric.get("_parsed_per_op_rows", 0)) > 0
                for metric in sample.values()
            )
            for sample in (ms0, ms1)
        )
        if not samples_complete:
            parsed["mountstats_error"] = (
                "NFS is mounted but one or both mountstats samples contain no "
                "parseable NFS per-op rows"
            )
            parsed["mount_metrics"] = []
            nfs_status = STATUS_PARSE_FAILED
        else:
            mount_metrics = _diff_mount_metrics(ms0, ms1)
            parsed["mount_metrics"] = mount_metrics
        raw_parts.append(f"--- {mountstats_path} (t1) ---\n" + ms1_content)
    else:
        parsed["mountstats_error"] = (
            f"t0(status={ms0_st}): {ms0_err or 'unavailable'}; t1(status={ms1_st}): {ms1_err or 'unavailable'}"
        )
        nfs_status = STATUS_PERMISSION if 2 in (ms0_st, ms1_st) else STATUS_MISSING

    if rpc0_st == 0 and rpc1_st == 0:
        parsed["client_calls_delta"] = round(max(0.0, rpc1["calls"] - rpc0["calls"]), 3)
        parsed["client_retrans_delta"] = round(
            max(0.0, rpc1["retrans"] - rpc0["retrans"]), 3
        )
    else:
        parsed["client_stats_error"] = (
            f"t0(status={rpc0_st}): {rpc0_err or 'unavailable'}; t1(status={rpc1_st}): {rpc1_err or 'unavailable'}"
        )

    # nfsiostat 可选补充（不强依赖，单次快照）
    if pid is None and _have_cmd("nfsiostat"):
        ec, out, err = _run(["nfsiostat"], timeout=15)
        if ec == 0 and out.strip():
            raw_parts.append("--- nfsiostat ---\n" + out)
            parsed["nfsiostat_raw"] = out
        else:
            parsed["nfsiostat_error"] = err or f"exit {ec}"

    if not mount_metrics and nfs_status == STATUS_OK:
        nfs_status = STATUS_EMPTY if rpc0_st != 0 else STATUS_OK

    return ProviderResult(
        source="nfs",
        status=nfs_status,
        started_at=evidence_started,
        ended_at=evidence_ended,
        raw="\n".join(raw_parts),
        parsed={
            **parsed,
            "target_pid": pid,
            "mount_namespace": _mount_namespace_key(pid),
        },
        error=str(parsed.get("mountstats_error") or ""),
    )


_PROC_IO_COUNTERS = (
    "rchar",
    "wchar",
    "syscr",
    "syscw",
    "read_bytes",
    "write_bytes",
    "cancelled_write_bytes",
)


def _is_glusterfs_fuse(fstype: Any) -> bool:
    """Return whether a mount is the supported GlusterFS FUSE client type."""
    return str(fstype or "").lower() == "fuse.glusterfs"


def _path_under_mount_collector(path: str, mount_point: str) -> bool:
    """Boundary-safe path-to-mount check for target-scoped GlusterFS evidence."""
    normalized_path = os.path.normpath(path)
    normalized_mount = os.path.normpath(mount_point)
    return normalized_path == normalized_mount or normalized_path.startswith(
        normalized_mount.rstrip("/") + "/"
    )


def _read_proc_io_counters(pid: int) -> tuple[dict[str, int] | None, str]:
    """Read the kernel's per-process IO counters without reading process environment/data."""
    content, error, status = _read_file(f"/proc/{pid}/io")
    if status != 0:
        return None, error or f"/proc/{pid}/io status {status}"
    counters: dict[str, int] = {}
    for line in content.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key not in _PROC_IO_COUNTERS:
            continue
        try:
            parsed = int(value.strip())
        except ValueError:
            continue
        if parsed >= 0:
            counters[key] = parsed
    return counters, ""


def _sample_process_io_tree(
    pid: int | None,
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """Sample one target process tree endpoint with stable PID identities."""
    if pid is None:
        return {}, []
    if not os.path.isdir(f"/proc/{pid}"):
        return {}, [f"pid {pid} is no longer visible in /proc"]
    tree, truncated = _process_tree(pid)
    errors: list[str] = []
    if truncated:
        errors.append(
            f"process tree reached {_MAX_PROCESS_TREE_PIDS} PID limit; GlusterFS IO coverage is partial"
        )
    samples: dict[int, dict[str, Any]] = {}
    for entry in tree:
        process_id = entry.get("pid")
        if not isinstance(process_id, int) or process_id <= 0:
            continue
        starttime = _pid_starttime_ticks(process_id)
        counters, error = _read_proc_io_counters(process_id)
        if counters is None:
            errors.append(f"pid {process_id}: {error}")
            continue
        samples[process_id] = {
            "pid_starttime_ticks": starttime,
            "role": entry.get("role", "unknown"),
            "counters": counters,
        }
    return samples, errors


def _delta_process_io_tree(
    before: dict[int, dict[str, Any]], after: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    """Return deltas only for process identities stable across both endpoints."""
    totals = {key: 0 for key in _PROC_IO_COUNTERS}
    sampled_pids: list[int] = []
    identity_changed: list[int] = []
    for process_id, first in before.items():
        second = after.get(process_id)
        if not isinstance(second, dict):
            continue
        if first.get("pid_starttime_ticks") != second.get("pid_starttime_ticks"):
            identity_changed.append(process_id)
            continue
        first_counters = first.get("counters") or {}
        second_counters = second.get("counters") or {}
        sampled_pids.append(process_id)
        for key in _PROC_IO_COUNTERS:
            try:
                delta = int(second_counters.get(key, 0)) - int(
                    first_counters.get(key, 0)
                )
            except (TypeError, ValueError):
                continue
            totals[key] += max(0, delta)
    syscr = totals["syscr"]
    totals.update(
        {
            "pids_sampled": sorted(sampled_pids),
            "stable_pid_count": len(sampled_pids),
            "identity_changed_pids": sorted(identity_changed),
            "avg_rchar_per_syscall": round(totals["rchar"] / syscr, 3)
            if syscr
            else None,
        }
    )
    return totals


def collect_glusterfs(
    duration: float, pid: int | None = None, path: str | None = None
) -> ProviderResult:
    """Collect target-scoped GlusterFS FUSE identity and process IO activity.

    Ordinary ``fuse.glusterfs`` mounts do not expose NFS-style per-mount RTT or
    metadata-latency counters to this runtime. Record only target mount identity
    and stable target-process read/syscall deltas. Those deltas are activity or
    small-read candidate evidence, not transport-latency evidence.
    """
    if pid is None and path is None:
        return ProviderResult(
            source="glusterfs",
            status=STATUS_EMPTY,
            error="target PID or path is required for target-scoped GlusterFS evidence",
        )
    started = _now_iso()
    mounts_pr = collect_mounts(pid)
    if mounts_pr.status not in (STATUS_OK, STATUS_EMPTY):
        return ProviderResult(
            source="glusterfs",
            status=mounts_pr.status,
            started_at=started,
            ended_at=_now_iso(),
            error=f"mount discovery failed: {mounts_pr.error or mounts_pr.status}",
        )
    all_mounts = mounts_pr.parsed if isinstance(mounts_pr.parsed, list) else []
    gluster_mounts = [
        mount
        for mount in all_mounts
        if isinstance(mount, dict) and _is_glusterfs_fuse(mount.get("fstype"))
    ]
    if not gluster_mounts:
        return ProviderResult(
            source="glusterfs",
            status=STATUS_UNSUPPORTED,
            started_at=started,
            ended_at=_now_iso(),
            error="no fuse.glusterfs mount found",
        )

    target_mounts = gluster_mounts
    scope = "all_current_glusterfs_mounts"
    if path:
        all_matches = [
            mount
            for mount in all_mounts
            if isinstance(mount, dict)
            and _path_under_mount_collector(path, str(mount.get("mount_point") or ""))
        ]
        if all_matches:
            _, chosen = max(
                enumerate(all_matches),
                key=lambda item: (
                    len(os.path.normpath(str(item[1].get("mount_point") or ""))),
                    item[0],
                ),
            )
        else:
            chosen = None
        if chosen is not None and _is_glusterfs_fuse(chosen.get("fstype")):
            target_mounts = [chosen]
            scope = "target_path_glusterfs"
        else:
            target_mounts = []
            scope = "target_path_non_glusterfs"

    evidence_started = _now_iso()
    first, first_errors = _sample_process_io_tree(pid)
    time.sleep(max(0.1, duration))
    second, second_errors = _sample_process_io_tree(pid)
    evidence_ended = _now_iso()
    process_io = _delta_process_io_tree(first, second) if pid is not None else None

    metrics = [
        {
            "mount_point": mount.get("mount_point"),
            "source": mount.get("device"),
            "fstype": mount.get("fstype"),
            "windowing": "delta",
            "target_scoped": scope == "target_path_glusterfs",
            "scope": scope,
            "process_io": process_io,
            "client_latency_available": False,
        }
        for mount in target_mounts
    ]
    errors = [*first_errors, *second_errors]
    return ProviderResult(
        source="glusterfs",
        status=STATUS_OK if target_mounts else STATUS_EMPTY,
        started_at=evidence_started,
        ended_at=evidence_ended,
        parsed={
            "mount_metrics": metrics,
            "target_pid": pid,
            "target_path": path,
            "target_scope": scope,
            "client_latency_available": False,
            "client_latency_unavailable_reason": (
                "fuse.glusterfs does not expose NFS-style per-mount RTT/execute/retrans "
                "counters in this runtime; supply Gluster client/brick statistics for transport attribution"
            ),
            "process_io_note": (
                "process_io is a stable target-process-tree delta and is not a per-mount or per-file latency metric"
            ),
            "partial": errors,
        },
        error="; ".join(errors),
    )


_PEROP_SECTION_MARKERS = ("per-op", "per-op:")


_METADATA_OPS = (
    "GETATTR",
    "SETATTR",
    "LOOKUP",
    "READLINK",
    "READDIR",
    "READDIRPLUS",
    "ACCESS",
    "FSSTAT",
    "FSINFO",
    "PATHCONF",
    "CREATE",
    "MKDIR",
    "SYMLINK",
    "MKNOD",
    "REMOVE",
    "RMDIR",
    "RENAME",
    "LINK",
    "OPEN",
    "OPEN_CONFIRM",
    "OPEN_NOATTR",
    "CLOSE",
    "LOCK",
    "LOCKT",
    "LOCKU",
    "DELEGRETURN",
    "LAYOUTGET",
    "LAYOUTRETURN",
)


_DATA_OPS = ("READ", "WRITE", "READDATA", "WRITEDATA")


def _new_mount_acc(mp: str, source: str, fstype: str) -> dict[str, Any]:
    return {
        "mount_point": mp,
        "source": source,
        "fstype": fstype,
        "_parsed_per_op_rows": 0,
        # 全量累计（所有 op 求和）
        "ops": 0,
        "transmissions": 0,
        "major_timeouts": 0,
        "sum_rtt_ms": 0.0,
        "sum_execute_ms": 0.0,
        "bytes_read": 0,
        "bytes_write": 0,
        # 元数据 op 子集累计（R300 远程访问耗时证据）
        "metadata_ops": 0,
        "metadata_sum_rtt_ms": 0.0,
        "metadata_sum_execute_ms": 0.0,
        # 数据 op 子集累计（R200 带宽/重传证据）
        "data_ops": 0,
        "data_transmissions": 0,
        "data_sum_rtt_ms": 0.0,
        "data_sum_execute_ms": 0.0,
    }


MountIdentity = tuple[str, str, str]


def _parse_mountstats(text: str) -> dict[MountIdentity, dict[str, Any]]:
    """解析 /proc/self/mountstats，按 (source, mount_point, fstype) 保存累计统计。

    头行格式（关键字位置固定，见 prometheus/procfs parseMount）：
        device <src> mounted on <mp> with fstype <type> statvers=<ver>
        字段：[0]device [1]src [2]mounted [3]on [4]mp [5]with [6]fstype [7]type [8]statvers
    per-op 段起始：内核实际输出 "per-op statistics"（无冒号），兼容 "per-op:"/"per-op"。
    per-op 数据行（>=9 列）：
        <OP>: <requests> <transmissions> <major_timeouts> <bytes_sent> <bytes_recv>
              <queue_ms> <rtt_ms> <execute_ms> [<errors>]
        RTT/execute 为**累计毫秒**（statvers 1.1，现代内核）；重传 = transmissions - requests。
    保留全量 + 元数据子集（GETATTR/LOOKUP/READDIR/...）+ 数据子集（READ/WRITE）三类累计。
    累计值不反映本次 workload 窗口，需配合两次采样差值（见 _diff_mount_metrics）。
    """
    mounts: dict[MountIdentity, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    in_per_op = False
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            in_per_op = False
            continue
        # 设备头行（关键字位置校验，避免误匹配数据行）
        if (
            len(fields) >= 8
            and fields[0] == "device"
            and fields[2] == "mounted"
            and fields[3] == "on"
            and fields[5] == "with"
            and fields[6] == "fstype"
        ):
            fstype = fields[7]
            if fstype.lower() not in {"nfs", "nfs4"}:
                current = None
                in_per_op = False
                continue
            mp = _decode_mountinfo_octal(fields[4])
            source = _decode_mountinfo_octal(fields[1])
            identity = (source, mp, fstype)
            current = mounts.setdefault(identity, _new_mount_acc(mp, source, fstype))
            in_per_op = False
            continue
        if current is None:
            continue
        # per-op 段起始标记（兼容内核 "per-op statistics" / "per-op:" / "per-op"）
        if fields[0] in _PEROP_SECTION_MARKERS or (
            len(fields) >= 2 and fields[0] == "per-op" and fields[1] == "statistics"
        ):
            in_per_op = True
            continue
        if fields[0] == "bytes:" and len(fields) >= 6:
            try:
                current["bytes_read"] = int(fields[1])
                current["bytes_write"] = int(fields[2])
            except ValueError:
                pass
            continue
        if in_per_op and fields[0].endswith(":"):
            # per-op 数据行：OP: req tx major bs br queue rtt exec [err]
            nums: list[float] = []
            ok = True
            for tok in fields[1:]:
                try:
                    nums.append(float(tok))
                except ValueError:
                    ok = False
                    break
            if not ok or len(nums) < 8:
                continue
            op = fields[0].upper().rstrip(":")
            req, tx, major, _bs, _br, _queue, rtt, execute = nums[:8]
            current["_parsed_per_op_rows"] += 1
            current["ops"] += req
            current["transmissions"] += tx
            current["major_timeouts"] += major
            current["sum_rtt_ms"] += rtt
            current["sum_execute_ms"] += execute
            if op in _METADATA_OPS:
                current["metadata_ops"] += req
                current["metadata_sum_rtt_ms"] += rtt
                current["metadata_sum_execute_ms"] += execute
            elif op in _DATA_OPS:
                current["data_ops"] += req
                current["data_transmissions"] += tx
                current["data_sum_rtt_ms"] += rtt
                current["data_sum_execute_ms"] += execute
    return mounts


def _diff_mount_metrics(
    prev: dict[MountIdentity, dict], cur: dict[MountIdentity, dict]
) -> list[dict[str, Any]]:
    """对两次累计 mountstats 求窗内差值，返回 per-mount 窗内性能指标列表。

    单次调用（prev 为空）时退化为累计值（仅 mount_point/source/fstype 可信，性能值标注 cumulative）。
    """
    out: list[dict[str, Any]] = []
    for identity, cur_m in cur.items():
        prev_m = prev.get(identity, {})
        same_identity = bool(prev_m) and (
            str(prev_m.get("source") or "") == str(cur_m.get("source") or "")
            and str(prev_m.get("fstype") or "") == str(cur_m.get("fstype") or "")
        )
        if not same_identity:
            prev_m = {}

        counter_keys = (
            "ops",
            "transmissions",
            "major_timeouts",
            "sum_rtt_ms",
            "sum_execute_ms",
            "metadata_ops",
            "metadata_sum_rtt_ms",
            "metadata_sum_execute_ms",
            "data_ops",
            "data_transmissions",
            "data_sum_rtt_ms",
            "data_sum_execute_ms",
            "bytes_read",
            "bytes_write",
        )
        counter_reset = False
        if prev_m:
            try:
                counter_reset = any(
                    float(cur_m.get(key, 0)) < float(prev_m.get(key, 0))
                    for key in counter_keys
                )
            except (TypeError, ValueError):
                counter_reset = True
        windowed = (
            "counter_reset" if counter_reset else ("delta" if prev_m else "cumulative")
        )

        def d(key):
            if counter_reset:
                return 0.0
            try:
                return float(cur_m.get(key, 0)) - float(prev_m.get(key, 0))
            except (TypeError, ValueError):
                return 0.0

        ops = d("ops")
        tx = d("transmissions")
        retrans = max(0.0, tx - ops)
        sum_rtt = d("sum_rtt_ms")
        sum_exec = d("sum_execute_ms")
        avg_rtt = (sum_rtt / ops) if ops > 0 else 0.0
        avg_exec = (sum_exec / ops) if ops > 0 else 0.0
        # 元数据子集（GETATTR/LOOKUP/READDIR/...）—— R300 远程访问耗时证据
        m_ops = d("metadata_ops")
        m_rtt = d("metadata_sum_rtt_ms")
        m_exec = d("metadata_sum_execute_ms")
        avg_meta_rtt = (m_rtt / m_ops) if m_ops > 0 else 0.0
        avg_meta_exec = (m_exec / m_ops) if m_ops > 0 else 0.0
        # 数据子集（READ/WRITE）—— R200 带宽/重传证据
        d_ops = d("data_ops")
        d_tx = d("data_transmissions")
        d_retrans = max(0.0, d_tx - d_ops)
        d_rtt = d("data_sum_rtt_ms")
        d_exec = d("data_sum_execute_ms")
        avg_data_rtt = (d_rtt / d_ops) if d_ops > 0 else 0.0
        avg_data_exec = (d_exec / d_ops) if d_ops > 0 else 0.0
        out.append(
            {
                "mount_point": cur_m.get("mount_point"),
                "source": cur_m.get("source"),
                "fstype": cur_m.get("fstype"),
                "windowing": windowed,
                "ops": round(ops, 3),
                "transmissions": round(tx, 3),
                "retrans": round(retrans, 3),
                "retrans_ratio": round(retrans / ops, 6) if ops > 0 else 0.0,
                "major_timeouts": round(d("major_timeouts"), 3),
                "sum_rtt_ms": round(sum_rtt, 3),
                "sum_execute_ms": round(sum_exec, 3),
                "avg_rtt_ms": round(avg_rtt, 3),
                "avg_execute_ms": round(avg_exec, 3),
                # 元数据子集
                "metadata_ops": round(m_ops, 3),
                "metadata_sum_rtt_ms": round(m_rtt, 3),
                "avg_metadata_rtt_ms": round(avg_meta_rtt, 3),
                "avg_metadata_execute_ms": round(avg_meta_exec, 3),
                # 数据子集
                "data_ops": round(d_ops, 3),
                "data_transmissions": round(d_tx, 3),
                "data_retrans": round(d_retrans, 3),
                "data_retrans_ratio": (
                    round(d_retrans / d_ops, 6) if d_ops > 0 else 0.0
                ),
                "avg_data_rtt_ms": round(avg_data_rtt, 3),
                "avg_data_execute_ms": round(avg_data_exec, 3),
                "bytes_read_delta": round(d("bytes_read"), 3),
                "bytes_write_delta": round(d("bytes_write"), 3),
            }
        )
    return out


def collect_memory() -> ProviderResult:
    """/proc/meminfo。"""
    started = _now_iso()
    content, err, st = _read_file("/proc/meminfo")
    if st == 1:
        return ProviderResult(
            source="memory",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    if st == 2:
        return ProviderResult(
            source="memory",
            status=STATUS_PERMISSION,
            started_at=started,
            ended_at=_now_iso(),
            error=err,
        )
    parsed: dict[str, float] = {}
    for line in content.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            parsed[m.group(1).lower()] = float(m.group(2))
    status = STATUS_OK if parsed else STATUS_PARSE_FAILED
    return ProviderResult(
        source="memory",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        raw=content,
        parsed=parsed,
    )
