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
"""pidstat, process-tree, and process-to-storage mapping collection."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any


from .common import (
    ProviderResult,
    STATUS_CMD_FAILED,
    STATUS_EMPTY,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    STATUS_PERMISSION,
    STATUS_UNSUPPORTED,
    _FD_SCAN_BUDGET_SECONDS,
    _MAX_CHILDREN_READ_BYTES,
    _MAX_OPEN_FILE_RECORDS,
    _MAX_PROCESS_TREE_PIDS,
    _PROCESS_MAP_FD_BUDGET_SECONDS,
    _cached_mountinfo_table,
    _classify_cmd,
    _finite_mean,
    _have_cmd,
    _mapping_observation_key,
    _merge_mapping_observation,
    _now_iso,
    _read_file,
    _resolve_canonical_device,
    _resolve_path_to_mount,
    _run_with_env,
    _strict_finite_float,
    _strict_text_rate,
    _to_float,
    parse_interval,
)


def collect_pidstat(duration: float) -> ProviderResult:
    """pidstat -d 动态采样。优先 JSON（sysstat 12.7.7+），失败降级到按表头解析的文本路径。

    文本路径处理时间列（12/24h、AM/PM）、Average 行、iodelay 列。固定 LC_ALL=C。
    """
    started = _now_iso()
    if not _have_cmd("pidstat"):
        return ProviderResult(
            source="pidstat",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
        )
    # duration 必须是有限正整数秒；非法时标记 command_failed。
    eff = parse_interval(duration)
    if eff is None:
        return ProviderResult(
            source="pidstat",
            status=STATUS_CMD_FAILED,
            started_at=started,
            ended_at=_now_iso(),
            error=f"invalid duration {duration!r} (need finite int seconds >=1)",
        )
    count = eff  # pidstat count = duration（不加 1），与请求窗口对齐。
    env = {**os.environ, "LC_ALL": "C"}
    parsed: dict[str, Any] | None = None
    source_fmt = ""
    raw_out = ""
    exit_code = 0
    err = ""
    winning_window: tuple[str, str] | None = None

    def _timed_run(cmd: list[str]) -> tuple[int, str, str, str, str]:
        attempt_started = _now_iso()
        ec, out, attempt_error = _run_with_env(cmd, env=env, timeout=duration + 10)
        return ec, out, attempt_error, attempt_started, _now_iso()

    # 优先 JSON
    ec_j, out_j, err_j, start_j, end_j = _timed_run(["pidstat", "-d", "-o", "JSON", "1", str(count)])
    raw_out, exit_code, err = out_j, ec_j, err_j
    if ec_j == 0 and out_j.strip().startswith("{"):
        parsed = _parse_pidstat_json(out_j)
        if parsed is not None:
            source_fmt = "json"
            raw_out, exit_code, err = out_j, ec_j, err_j
            winning_window = (start_j, end_j)
    # A JSON parser failure occurred after a complete sampling period. Do not run a
    # second full text sample; only an immediate unsupported-option rejection may
    # use the compatibility fallback.
    if (
        parsed is None
        and ec_j != 0
        and any(
            token in f"{out_j}\n{err_j}".lower()
            for token in ("invalid option", "unrecognized", "illegal option", "usage:")
        )
    ):
        ec_t, out_t, err_t, start_t, end_t = _timed_run(["pidstat", "-d", "1", str(count)])
        exit_code, err, raw_out = ec_t, err_t, out_t
        status = _classify_cmd("pidstat", ec_t, out_t, err_t)
        if status == STATUS_OK:
            parsed = _parse_pidstat_text(out_t)
            source_fmt = "text"
            if parsed is not None:
                winning_window = (start_t, end_t)
    if parsed is None:
        status = _classify_cmd("pidstat", exit_code, raw_out, err)
        if status == STATUS_OK:
            status = STATUS_PARSE_FAILED
    else:
        status = STATUS_OK if parsed.get("processes") else STATUS_EMPTY
    parsed_out = parsed if parsed is not None else None
    if parsed_out is not None:
        parsed_out["source_format"] = source_fmt
    return ProviderResult(
        source="pidstat",
        status=status,
        started_at=winning_window[0] if winning_window else started,
        ended_at=winning_window[1] if winning_window else _now_iso(),
        exit_code=exit_code,
        stderr=err,
        raw=raw_out,
        parsed=parsed_out,
    )


def _parse_pidstat_json(text: str) -> dict[str, Any] | None:
    """解析 pidstat -o JSON 输出，收集全部 task snapshots 并按 PID 聚合。

    Modern schema uses `statistics[].io` with `PID`/`UID`/`cmd`; older builds may
    use `statistics[].task` with lowercase keys. Accept both explicitly.
    每个 PID 的速率字段取窗口平均（避免仅最后瞬时决定 R400 活跃判定）。
    """
    # 非字符串输入不能交给 json.loads，需提前返回。
    if not isinstance(text, str):
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    # 拒绝会让后续 doc.get 失败的 null 或 list 顶层值。
    if not isinstance(doc, dict):
        return None
    # 逐层检查嵌套值的类型。
    sysstat = doc.get("sysstat")
    if not isinstance(sysstat, dict):
        return None
    hosts = sysstat.get("hosts") or []
    if not isinstance(hosts, list) or not hosts:
        return None
    h0 = hosts[0]
    if not isinstance(h0, dict):
        return None
    stats = h0.get("statistics") or []
    if not isinstance(stats, list) or not stats:
        return None
    per_pid: dict[int, list[dict[str, Any]]] = {}
    recognized = False
    for snap in stats:
        if not isinstance(snap, dict):
            return None
        tasks = None
        for key in ("io", "task"):
            if key in snap:
                recognized = True
                tasks = snap.get(key)
                break
        if tasks is None:
            return None
        if not isinstance(tasks, list):
            return None
        for t in tasks:
            if not isinstance(t, dict):
                return None
            raw_pid = t.get("PID", t.get("pid"))
            if isinstance(raw_pid, bool):
                return None
            if isinstance(raw_pid, int):
                pid = raw_pid
            elif isinstance(raw_pid, str) and len(raw_pid) <= 10 and re.fullmatch(r"[0-9]+", raw_pid):
                try:
                    pid = int(raw_pid)
                except ValueError:
                    return None
            else:
                # Reject floats (including infinity from JSON `1e309`) rather than
                # truncating or raising OverflowError from int().
                return None
            if pid <= 0 or pid > 2_147_483_647:
                return None
            if "kB_rd/s" not in t or "kB_wr/s" not in t:
                return None
            metric_values = {
                "kB_rd/s": t["kB_rd/s"],
                "kB_wr/s": t["kB_wr/s"],
                "kB_ccwr/s": t.get("kB_ccwr/s", 0),
            }
            if any(_strict_finite_float(value) is None for value in metric_values.values()):
                return None
            per_pid.setdefault(pid, []).append(
                {
                    "uid": t.get("UID", t.get("uid", "")),
                    **metric_values,
                    "command": t.get("cmd", t.get("command", t.get("Command", ""))),
                }
            )
    if not recognized:
        return None
    procs = []
    for pid, snaps in per_pid.items():
        aggregated = _aggregate_pidstat_samples(pid, snaps)
        if aggregated is None:
            return None
        procs.append(aggregated)
    return {"processes": procs, "reports": len(stats)}


def _aggregate_pidstat_samples(pid: int, snaps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """把同一 PID 的多份 pidstat 快照聚合为窗口平均。"""
    n = len(snaps)
    if n == 0:
        return None
    first = snaps[0]
    read_mean = _finite_mean([_to_float(s.get("kB_rd/s")) for s in snaps])
    write_mean = _finite_mean([_to_float(s.get("kB_wr/s")) for s in snaps])
    cancel_mean = _finite_mean([_to_float(s.get("kB_ccwr/s")) for s in snaps])
    if read_mean is None or write_mean is None or cancel_mean is None:
        return None
    return {
        "pid": pid,
        "uid": str(first.get("uid", "")),
        "kbr_per_s": round(read_mean, 4),
        "kbw_per_s": round(write_mean, 4),
        "kbccwd_per_s": round(cancel_mean, 4),
        "command": str(first.get("command", "")),
        "sample_count": n,
        "active_sample_count": sum(
            1 for sample in snaps if _to_float(sample.get("kB_rd/s")) >= 100 or _to_float(sample.get("kB_wr/s")) >= 100
        ),
    }


def _parse_pidstat_text(text: str) -> dict[str, Any] | None:
    """解析 pidstat -d 文本输出，按表头索引，处理时间列/AM-PM/Average 行/iodelay。

    真实输出形如：
      12:00:00   UID PID kB_rd/s kB_wr/s kB_ccwr/s iodelay  Command
      12:00:01  1000 1234 2048.00 0.00 0.00 0 python
      Average:  1000 1234 2048.00 0.00 0.00 0 python
    收集全部非 Average 报告并按 PID 聚合为窗口平均。
    """
    blocks = re.split(r"\n\s*\n", text.strip())
    samples: list[tuple[list[str], list[str]]] = []
    for blk in blocks:
        lines = [ln for ln in blk.splitlines() if ln.strip()]
        if not lines:
            continue
        header_line = next((ln for ln in lines if "UID" in ln and "PID" in ln), None)
        if header_line is None:
            if lines[0].lstrip().lower().startswith("linux "):
                continue
            return None
        if header_line:
            if header_line.lstrip().lower().startswith("average:"):
                continue
            header = header_line.split()
            rows = [ln for ln in lines if ln != header_line]
            samples.append((header, rows))
    procs: list[dict[str, Any]] = []
    if not samples:
        return None

    # 按 PID 收集所有快照
    per_pid: dict[int, list[dict[str, Any]]] = {}
    for header, rows in samples:
        idx = {col: i for i, col in enumerate(header)}

        def col_of(*names: str) -> int | None:
            for name in names:
                if name in idx:
                    return idx[name]
            return None

        pid_i = col_of("PID")
        uid_i = col_of("UID")
        kbr_i = col_of("kB_rd/s")
        kbw_i = col_of("kB_wr/s")
        kbcc_i = col_of("kB_ccwr/s")
        command_i = col_of("Command", "cmd")
        if pid_i is None or kbr_i is None or kbw_i is None:
            return None
        for row in rows:
            f = row.split()
            if f and f[0].lower().startswith("average"):
                continue
            if len(f) <= pid_i:
                return None
            raw_pid = f[pid_i]
            if len(raw_pid) > 10 or re.fullmatch(r"[0-9]+", raw_pid) is None:
                return None
            pid = int(raw_pid)
            if pid <= 0 or pid > 2_147_483_647:
                return None
            cmd_start = command_i if command_i is not None else len(f)
            rates = (
                _strict_text_rate(f, kbr_i),
                _strict_text_rate(f, kbw_i),
                _strict_text_rate(f, kbcc_i),
            )
            if any(rate is None for rate in rates):
                return None
            per_pid.setdefault(pid, []).append(
                {
                    "uid": f[uid_i] if uid_i is not None and uid_i < len(f) else "",
                    "kbr_per_s": rates[0],
                    "kbw_per_s": rates[1],
                    "kbccwd_per_s": rates[2],
                    "command": " ".join(f[cmd_start:]) if cmd_start < len(f) else "",
                }
            )

    for pid, snaps in per_pid.items():
        n = len(snaps)
        first = snaps[0]
        read_mean = _finite_mean([s["kbr_per_s"] for s in snaps])
        write_mean = _finite_mean([s["kbw_per_s"] for s in snaps])
        cancel_mean = _finite_mean([s["kbccwd_per_s"] for s in snaps])
        if read_mean is None or write_mean is None or cancel_mean is None:
            return None
        procs.append(
            {
                "pid": pid,
                "uid": first.get("uid", ""),
                "kbr_per_s": round(read_mean, 4),
                "kbw_per_s": round(write_mean, 4),
                "kbccwd_per_s": round(cancel_mean, 4),
                "command": first.get("command", ""),
                "sample_count": n,
                "active_sample_count": sum(
                    1 for sample in snaps if sample["kbr_per_s"] >= 100 or sample["kbw_per_s"] >= 100
                ),
            }
        )
    return {"processes": procs, "reports": len(samples)}


def _read_boot_id() -> str | None:
    """Return the current kernel boot identity used to disambiguate PID reuse."""
    content, _, status = _read_file("/proc/sys/kernel/random/boot_id")
    value = content.strip()
    return value if status == 0 and value else None


def _pid_starttime_ticks(pid: int) -> int | None:
    """Read field 22 from /proc/<pid>/stat without splitting the parenthesized comm."""
    content, _, status = _read_file(f"/proc/{pid}/stat")
    if status != 0:
        return None
    closing = content.rfind(")")
    if closing < 0:
        return None
    fields = content[closing + 1 :].split()
    try:
        # fields[0] is stat field 3 (state), so field 22 is index 19 here.
        value = int(fields[19])
    except (IndexError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _children_of(pid: int) -> list[int]:
    """读 /proc/<pid>/task/<tid>/children 获取直接子进程（内核需 CONFIG_PROC_CHILDREN）。

    不可用时返回空，由调用方退化。
    """
    try:
        with open(f"/proc/{pid}/task/{pid}/children", encoding="utf-8") as f:
            content = f.read(_MAX_CHILDREN_READ_BYTES)
            return [int(x) for x in content.split()[:_MAX_PROCESS_TREE_PIDS] if x.isdigit()]
    except (OSError, ValueError):
        return []


def _process_tree(root: int) -> tuple[list[dict[str, Any]], bool]:
    """从 root 递归收集进程树（BFS），记录后代的直接 parent_pid。

    只读取指定 root 的 children 链，不扫全机 /proc；返回值第二项表示是否达到
    PID 上限。避免 busy production host 上按路径采集时产生无界进程扫描。
    """
    tree: list[dict[str, Any]] = [{"pid": root, "role": "root"}]
    seen: set[int] = {root}
    queue: list[int] = [root]
    truncated = False
    while queue:
        cur = queue.pop(0)
        kids = sorted(set(_children_of(cur)))
        for k in kids:
            if k not in seen:
                if len(tree) >= _MAX_PROCESS_TREE_PIDS:
                    truncated = True
                    queue.clear()
                    break
                seen.add(k)
                tree.append({"pid": k, "role": "descendant", "parent_pid": cur})
                queue.append(k)
    return tree, truncated


def _fd_mnt_id(pid: int, fd: str) -> int | None:
    """读取 fdinfo.mnt_id，用于在 overmount/旧 FD 场景精确绑定 mountinfo。"""
    try:
        with open(f"/proc/{pid}/fdinfo/{fd}", encoding="utf-8") as info:
            for line in info:
                if line.startswith("mnt_id:"):
                    return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None


def _opened_file_records(
    pid: int, target_path: str | None = None, deadline: float | None = None
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Return bounded FD records, prioritizing an explicit target path when present."""
    target_records: list[dict[str, Any]] = []
    other_records: list[dict[str, Any]] = []
    denied = False
    truncated = False
    fd_dir = f"/proc/{pid}/fd"
    deadline = min(
        time.monotonic() + _FD_SCAN_BUDGET_SECONDS,
        deadline if deadline is not None else float("inf"),
    )

    def _append(record: dict[str, Any]) -> bool:
        nonlocal truncated
        is_target = bool(target_path and _is_data_relevant_path_collector(record["path"], target_path))
        bucket = target_records if is_target else other_records
        if len(bucket) >= _MAX_OPEN_FILE_RECORDS:
            truncated = True
            return False
        bucket.append(record)
        return True

    if time.monotonic() >= deadline:
        return [], False, True
    try:
        with os.scandir(fd_dir) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    truncated = True
                    break
                fd = entry.name
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                    if target.startswith("/"):
                        kept = _append(
                            {
                                "path": target,
                                "fd": int(fd) if fd.isdigit() else fd,
                                "mnt_id": _fd_mnt_id(pid, fd),
                                "path_source": "fd",
                            }
                        )
                        if not target_path and not kept:
                            break
                        if target_path and len(target_records) >= _MAX_OPEN_FILE_RECORDS:
                            truncated = True
                            break
                except PermissionError:
                    denied = True
                except OSError:
                    continue
    except PermissionError:
        return [], True
    except OSError:
        pass
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        if cwd.startswith("/"):
            _append({"path": cwd, "fd": None, "mnt_id": None, "path_source": "cwd"})
    except PermissionError:
        denied = True
    except OSError:
        pass
    records = target_records + other_records
    if len(records) > _MAX_OPEN_FILE_RECORDS:
        truncated = True
        records = records[:_MAX_OPEN_FILE_RECORDS]
    return records, denied, truncated


_MOUNTINFO_LINE_RE = re.compile(r"^\d+\s+\d+\s+\d+:\d+\s+\S+\s+(\S+)\s+")


def collect_process_io_map(
    pid: int | None,
    path: str | None,
    duration: float = 0.0,
    _fd_deadline: float | None = None,
) -> ProviderResult:
    """建立 PID → path → mount → device 映射（用于 R400）。

    映射策略：
      - --pid 扩展为进程树（root + 后代，记录 pid_tree 与 role）。
      - --path 只用于已指定 PID 的数据路径过滤；不会反查扫描全机 /proc。
      - 路径→挂载用 mountinfo 最长前缀匹配（首选，支持空格/Unicode），findmnt -T 交叉校验。
      - 每个 PID 截取去重后的 FD（上限 256/进程），截断记 partial。
      - 每条映射标注 path_relevant（数据相关 vs 共享库/日志）。
    R400 映射必须带 --pid；无 pid/path 时返回 unsupported（不臆造映射）。
    访问目标进程受限时标 permission_denied。
    """
    started = _now_iso()
    if not pid and not path:
        return ProviderResult(
            source="process_io_map",
            status=STATUS_UNSUPPORTED,
            started_at=started,
            ended_at=_now_iso(),
        )
    if path and not pid:
        return ProviderResult(
            source="process_io_map",
            status=STATUS_UNSUPPORTED,
            started_at=started,
            ended_at=_now_iso(),
            error=(
                "--path without --pid does not scan arbitrary /proc processes; "
                "pass --pid to collect R400 PID-to-device mappings"
            ),
        )
    if pid and not os.path.isdir(f"/proc/{pid}"):
        return ProviderResult(
            source="process_io_map",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
            error=f"pid {pid} does not exist or is no longer visible in /proc",
        )

    pid_tree: list[dict[str, Any]] = []
    tree_truncated = False
    if pid:
        pid_tree, tree_truncated = _process_tree(pid)
    pids = [e["pid"] for e in pid_tree]

    mapping: list[dict[str, Any]] = []
    mountinfo_cache: dict[str, list[dict[str, Any]]] = {}
    canonical_device_cache: dict[str, dict[str, Any]] = {}
    boot_id = _read_boot_id()
    errors: list[str] = []
    partial: list[str] = []
    if tree_truncated:
        partial.append(f"process tree reached {_MAX_PROCESS_TREE_PIDS} PID limit; mapping coverage is partial")
    denied_pids: list[int] = []
    mountinfo_failed_pids: list[int] = []
    mountinfo_budget_pids: list[int] = []
    fd_cap = _MAX_OPEN_FILE_RECORDS
    fd_deadline = _fd_deadline or (time.monotonic() + _PROCESS_MAP_FD_BUDGET_SECONDS)
    for entry in pid_tree:
        if time.monotonic() >= fd_deadline:
            partial.append(
                f"process-map FD scan exceeded {_PROCESS_MAP_FD_BUDGET_SECONDS:g}s global budget; mapping coverage is partial"
            )
            break
        p = entry["pid"]
        identity_before = _pid_starttime_ticks(p)
        entry["boot_id"] = boot_id
        entry["pid_starttime_ticks"] = identity_before
        mapping_start = len(mapping)
        opened, denied, fd_scan_truncated = _opened_file_records(p, target_path=path, deadline=fd_deadline)
        if denied and not opened:
            denied_pids.append(p)
            errors.append(f"pid {p}: permission denied reading /proc/{p}/fd")
            continue  # 该 PID 无法读取 FD，跳过（不臆造映射）
        if denied:
            partial.append(f"pid {p}: 部分 FD/cwd 无权限，保留已成功读取的映射")
        if fd_scan_truncated:
            partial.append(
                f"pid {p}: /proc/{p}/fd scan capped by {fd_cap} records or "
                f"{_FD_SCAN_BUDGET_SECONDS:g}s; mapping coverage is partial"
            )
        # 过滤为常规文件路径并去重；截断保护
        targets = [record for record in opened if record["path"].startswith("/")]
        # 数据/target 相关路径优先，避免大量系统库 FD 按字典序占满 cap。
        target_prefix = (path.rstrip("/") + "/") if path else ""
        targets = sorted(
            targets,
            key=lambda record: (
                not (
                    (path and (record["path"] == path or record["path"].startswith(target_prefix)))
                    or _is_data_relevant_path_collector(record["path"])
                ),
                record["path"],
            ),
        )
        deduped: dict[tuple[Any, Any], dict[str, Any]] = {}
        for record in targets:
            deduped[(record["path"], record.get("mnt_id"))] = record
        targets = list(deduped.values())
        if len(targets) > fd_cap:
            partial.append(f"pid {p}: {len(targets)} FD targets > {fd_cap}，截断")
            targets = targets[:fd_cap]
        mount_table, mount_namespace, mountinfo_budget_exhausted = _cached_mountinfo_table(p, mountinfo_cache)
        if targets and not mount_table:
            if mountinfo_budget_exhausted:
                mountinfo_budget_pids.append(p)
                partial.append(f"pid {p}: mountinfo namespace cache budget exhausted; mapping coverage is partial")
                continue
            mountinfo_failed_pids.append(p)
            errors.append(f"pid {p}: 无法读取目标 mount namespace 的 mountinfo")
            partial.append(f"pid {p}: mount namespace {mount_namespace} 不可解析，已跳过路径→设备确认")
            continue
        for record in targets:
            t = record["path"]
            mi = _resolve_path_to_mount(
                t,
                pid=p,
                mnt_id=record.get("mnt_id"),
                mountinfo_cache=mountinfo_cache,
            )
            if not mi:
                continue
            # 把挂载源 major:minor 解析成 iostat 整盘名。
            cand = _resolve_canonical_device(mi.get("major_minor", ""), mi["source"], canonical_device_cache)
            if cand.get("device_resolution") == "heuristic-unresolved-mapper" and (
                "device resolution: heuristic (no /sys)" not in partial
            ):
                partial.append("device resolution: heuristic (no /sys)")
            observed_at = _now_iso()
            mapping.append(
                {
                    "pid": p,
                    "boot_id": boot_id,
                    "pid_starttime_ticks": identity_before,
                    "role": entry.get("role", "root"),
                    "path": t,
                    "mount_point": mi["mount_point"],
                    "source": mi["source"],
                    "fstype": mi["fstype"],
                    "canonical_device": cand["canonical_device"],
                    "major_minor": cand.get("major_minor", ""),
                    "backing_devices": cand.get("backing_devices", []),
                    "device_resolution": cand.get("device_resolution", "unknown"),
                    "mount_namespace": mi.get("mount_namespace", mount_namespace),
                    "mount_id": mi.get("mount_id"),
                    "mount_binding": mi.get("mount_binding", "path_prefix"),
                    "fd": record.get("fd"),
                    "path_source": record.get("path_source"),
                    "path_relevant": _is_data_relevant_path_collector(t, path),
                    "first_seen": observed_at,
                    "last_seen": observed_at,
                    "observation_count": 1,
                }
            )
        identity_after = _pid_starttime_ticks(p)
        if identity_after != identity_before:
            del mapping[mapping_start:]
            entry["identity_changed_during_observation"] = True
            partial.append(f"pid {p}: process identity changed during mapping observation; mappings dropped")
    observation_samples = 1
    if duration > 0:
        # 与 iostat/pidstat 同窗做 t0/t1 两次轻量观察，捕获快速 open/read/close
        # workload；合并时保留每条路径首次/末次观测时间。
        time.sleep(max(0.1, duration))
        # Each endpoint needs its own bounded FD budget. Reusing the first endpoint's
        # deadline after the workload interval would make repeated mappings impossible.
        second = collect_process_io_map(pid, path, 0.0)
        observation_samples = 2
        second_parsed = second.parsed if isinstance(second.parsed, dict) else {}
        merged: dict[tuple[Any, ...], dict[str, Any]] = {_mapping_observation_key(item): item for item in mapping}
        for item in second_parsed.get("mappings", []) or []:
            if not isinstance(item, dict):
                continue
            key = _mapping_observation_key(item)
            if key in merged:
                merged[key] = _merge_mapping_observation(merged[key], item, second.ended_at)
            else:
                merged[key] = item
        mapping = list(merged.values())
        if second.error:
            errors.append(second.error)
        partial.extend(second_parsed.get("partial", []) or [])
        denied_pids.extend(
            entry.get("pid")
            for entry in second_parsed.get("pid_tree", []) or []
            if isinstance(entry, dict) and second.status == STATUS_PERMISSION and isinstance(entry.get("pid"), int)
        )
    # 全部目标 PID 被拒时返回 permission_denied；部分成功时返回 ok 并记录 denied。
    all_denied = bool(denied_pids) and not mapping and len(denied_pids) == len(pids)
    if all_denied:
        status = STATUS_PERMISSION
    elif mountinfo_failed_pids and not mapping and not mountinfo_budget_pids:
        status = STATUS_PERMISSION
    elif mapping:
        status = STATUS_OK
        if denied_pids:
            partial.append(f"permission denied PIDs: {denied_pids}")
    elif denied_pids:
        status = STATUS_PERMISSION
    else:
        status = STATUS_EMPTY
    return ProviderResult(
        source="process_io_map",
        status=status,
        started_at=started,
        ended_at=_now_iso(),
        error="; ".join(errors),
        parsed={
            "mappings": mapping,
            "pid_count": len(pids),
            "pid_tree": pid_tree,
            "partial": partial,
            "denied_pid_count": len(denied_pids),
            "observation_samples": observation_samples,
            "mountinfo_failed_pids": sorted(set(mountinfo_failed_pids)),
        },
    )


def _is_data_relevant_path_collector(path: str | None, target_path: str | None = None) -> bool:
    if not path:
        return False
    normalized = os.path.normpath(path)
    low = normalized.lower()
    if low.endswith((".so", ".so.", ".pyc", ".pyo", ".pyd")):
        return False
    if "/site-packages/" in low or "/dist-packages/" in low:
        return False

    if isinstance(target_path, str):
        target = os.path.normpath(target_path)
        if target not in ("", "/", "."):
            return normalized == target or normalized.startswith(target.rstrip("/") + "/")

    prefixes = (
        "/usr",
        "/lib",
        "/lib64",
        "/opt",
        "/var/log",
        "/var/lib",
        "/proc",
        "/sys",
        "/dev",
        "/boot",
        "/etc",
        "/run",
        "/snap",
        "/conda",
        "/python",
        "/bin",
        "/sbin",
    )
    if any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in prefixes):
        return False
    return True
