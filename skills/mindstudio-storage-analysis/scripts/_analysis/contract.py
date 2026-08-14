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
"""Snapshot and NPU profile validation and normalization."""

from __future__ import annotations

import copy
from collections import defaultdict
import re
from typing import Any

from .common import (
    _PROVIDER_NAMES,
    _parse_iso,
    _parsed,
    _profile_interval,
    _profile_window_matches_snapshot,
    _provider,
    _provider_interval,
    _snapshot_interval,
    _validate_schema_version,
)


def _strict_float(v: Any) -> float | None:
    """严格转 float：None/空串视为缺失（返回 None）；非数值字符串/NaN/Inf 抛 ValueError。

    用于校验外部 Snapshot/profile；"oops" 这类非法值必须被拒绝而非静默置 0。
    """
    if v is None or v == "":
        return None
    # bool 不是合法数值；float(True)=1.0 会静默改变语义。
    if isinstance(v, bool):
        raise ValueError("bool is not a valid float")
    try:
        f = float(v)  # "oops" → ValueError；"5"/5/5.0 → OK
    except OverflowError as exc:
        raise ValueError("float overflow") from exc
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("non-finite float")
    return f


def _strict_json_int(value: Any, *, positive: bool = False) -> int:
    """Accept only an actual JSON integer, never bool, float, or numeric text."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("must be a JSON integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"must be a {qualifier} JSON integer")
    return value


_MAX_JSON_FILE_BYTES = 64 * 1024 * 1024


_MAX_JSON_DEPTH = 64


_MAX_JSON_NODES = 1_000_000


def _validate_json_resources(value: Any) -> None:
    """Bound nesting and node count before deepcopy or rule traversal."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise ValueError(f"JSON exceeds {_MAX_JSON_NODES} nodes")
        if depth > _MAX_JSON_DEPTH:
            raise ValueError(f"JSON nesting exceeds {_MAX_JSON_DEPTH} levels")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


_IOSTAT_FIELD_SAMPLE_COUNTS = (
    "util_sample_count",
    "avgqu_sz_sample_count",
    "await_sample_count",
    "r_await_ms_sample_count",
    "w_await_ms_sample_count",
    "r_per_s_sample_count",
    "w_per_s_sample_count",
    "rkB_per_s_sample_count",
    "wkB_per_s_sample_count",
    "rrqm_per_s_sample_count",
    "wrqm_per_s_sample_count",
    "avgrq_sz_sample_count",
    "avgqu_sz_with_util_sample_count",
    "await_with_util_sample_count",
    "r_await_ms_with_util_sample_count",
    "w_await_ms_with_util_sample_count",
)


_IOSTAT_PAIRED_SAMPLE_BASES = {
    "avgqu_sz_with_util_sample_count": "avgqu_sz_sample_count",
    "await_with_util_sample_count": "await_sample_count",
    "r_await_ms_with_util_sample_count": "r_await_ms_sample_count",
    "w_await_ms_with_util_sample_count": "w_await_ms_sample_count",
}


_NUMERIC_FIELDS = {
    "iostat_disks": (
        "util_percent",
        "util_max",
        "util_p95",
        "r_await_ms",
        "w_await_ms",
        "avgqu_sz",
        "r_per_s",
        "w_per_s",
        "rkB_per_s",
        "wkB_per_s",
        "await",
        "rrqm_per_s",
        "wrqm_per_s",
        "avgrq_sz",
        "sample_count",
        *_IOSTAT_FIELD_SAMPLE_COUNTS,
    ),
    "nfs_metric": (
        "ops",
        "transmissions",
        "retrans",
        "retrans_ratio",
        "major_timeouts",
        "avg_rtt_ms",
        "avg_execute_ms",
        "sum_rtt_ms",
        "sum_execute_ms",
        "metadata_ops",
        "avg_metadata_rtt_ms",
        "avg_metadata_execute_ms",
        "metadata_sum_rtt_ms",
        "metadata_sum_execute_ms",
        "data_ops",
        "data_transmissions",
        "data_retrans",
        "data_retrans_ratio",
        "avg_data_rtt_ms",
        "avg_data_execute_ms",
        "data_sum_rtt_ms",
        "data_sum_execute_ms",
        "bytes_read_delta",
        "bytes_write_delta",
    ),
    "df_fs": ("iuse_percent",),
    "pidstat_proc": (
        "kbr_per_s",
        "kbw_per_s",
        "kbccwd_per_s",
        "sample_count",
        "active_sample_count",
    ),
    "diskstats": (
        "reads_completed",
        "reads_merged",
        "sectors_read",
        "time_reading_ms",
        "writes_completed",
        "writes_merged",
        "sectors_written",
        "time_writing_ms",
        "io_in_progress",
        "time_io_ms",
        "weighted_time_io_ms",
    ),
}


_PERCENT_FIELDS = {"util_percent", "util_max", "util_p95", "iuse_percent"}


_INTEGER_FIELDS = {
    "sample_count",
    "active_sample_count",
    *_IOSTAT_FIELD_SAMPLE_COUNTS,
}


_IOSTAT_EVIDENCE_FIELDS = {
    "util_percent",
    "util_max",
    "util_p95",
    "avgqu_sz",
    "await",
    "r_await_ms",
    "w_await_ms",
    "r_per_s",
    "w_per_s",
    "rkB_per_s",
    "wkB_per_s",
}


def _validate_numeric_dict(d: Any, fields: tuple[str, ...]) -> bool:
    """Validate finite, non-negative metrics and bounded percentages."""
    if not isinstance(d, dict):
        return False
    for key in fields:
        if key in d:
            try:
                value = _strict_float(d[key])
            except (ValueError, TypeError):
                return False
            if value is None:
                # 将空串统一为 null，避免下游把“缺失”再次直接 float() 而崩溃。
                d[key] = None
                continue
            if value < 0:
                return False
            if key in _PERCENT_FIELDS and value > 100:
                return False
            if key in _INTEGER_FIELDS and not value.is_integer():
                return False
    return True


_DEVICE_BASELINE_FIELDS = {"max_read_mbps", "max_write_mbps", "max_iops"}


def _normalize_device_baselines(snapshot: dict, errors: list[str]) -> None:
    """Normalize optional user-supplied device ceilings; invalid values never certify."""
    raw = snapshot.get("device_baselines")
    if raw is None:
        return
    if not isinstance(raw, dict):
        errors.append(
            f"device_baselines: not an object ({type(raw).__name__}), ignored"
        )
        snapshot["device_baselines"] = {}
        return
    cleaned: dict[str, dict[str, float]] = {}
    for device, baseline in raw.items():
        if not isinstance(device, str) or not device or not isinstance(baseline, dict):
            errors.append(f"device_baselines: invalid device entry {device!r}, ignored")
            continue
        unsupported = sorted(set(baseline) - _DEVICE_BASELINE_FIELDS, key=str)
        if unsupported:
            errors.append(
                f"device_baselines.{device}: unsupported field(s) {unsupported}, ignored"
            )
        normalized: dict[str, float] = {}
        for field in _DEVICE_BASELINE_FIELDS:
            if field not in baseline:
                continue
            try:
                value = _strict_float(baseline[field])
            except (TypeError, ValueError):
                value = None
            if value is None or value <= 0:
                errors.append(
                    f"device_baselines.{device}.{field}: must be a finite positive number, ignored"
                )
                continue
            normalized[field] = value
        if normalized:
            cleaned[device] = normalized
    snapshot["device_baselines"] = cleaned


_BOOT_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _process_map_identity_error(parsed: dict, mappings: list[dict]) -> str | None:
    """Reject process identities that cannot coexist in one host observation window."""
    boot_ids: set[str] = set()
    mapping_identities: dict[int, set[tuple[str, int]]] = {}

    def identity(entry: dict, label: str) -> tuple[int, str, int] | None:
        boot_id = entry.get("boot_id")
        starttime = entry.get("pid_starttime_ticks")
        if boot_id is None and starttime is None:
            return None
        try:
            pid = _strict_json_int(entry.get("pid"), positive=True)
        except ValueError as exc:
            raise ValueError(f"{label} has invalid PID identity") from exc
        if (
            not isinstance(boot_id, str)
            or _BOOT_ID_PATTERN.fullmatch(boot_id) is None
            or not isinstance(starttime, int)
            or isinstance(starttime, bool)
            or starttime < 0
        ):
            raise ValueError(f"{label} has malformed boot_id/pid_starttime_ticks")
        boot_ids.add(boot_id.lower())
        return pid, boot_id.lower(), starttime

    try:
        for index, mapping in enumerate(mappings):
            item = identity(mapping, f"mappings[{index}]")
            if item is None:
                continue
            pid, boot_id, starttime = item
            mapping_identities.setdefault(pid, set()).add((boot_id, starttime))
        if any(len(values) > 1 for values in mapping_identities.values()):
            return "one PID has inconsistent boot/starttime identities"

        pid_tree = parsed.get("pid_tree")
        if pid_tree is not None and not isinstance(pid_tree, list):
            return "pid_tree not list"
        for index, entry in enumerate(pid_tree or []):
            if not isinstance(entry, dict):
                return f"pid_tree[{index}] not object"
            item = identity(entry, f"pid_tree[{index}]")
            if item is None:
                continue
            pid, boot_id, starttime = item
            mapped = mapping_identities.get(pid)
            if mapped and (boot_id, starttime) not in mapped:
                return (
                    f"pid_tree[{index}] identity conflicts with mappings for PID {pid}"
                )
    except ValueError as exc:
        return str(exc)
    if len(boot_ids) > 1:
        return "multiple boot_id values in one process-map window"
    return None


def normalize_and_validate(snapshot: dict) -> tuple[dict, list[str]]:
    """统一输入契约入口，供全量与所有单规则 --mode 共用。

    校验并规范化外部 Snapshot：顶层 dict、各 provider parsed 的容器与数值字段、
    diskstats 深层（设备值/timestamp）、iostat sample_count、availability 元素。
    单个 provider 损坏 → 标 parse_failed + availability.errors，其他规则继续运行，绝不崩溃。
    返回 (规范化后的 snapshot, errors)。
    """
    if not isinstance(snapshot, dict):
        return {}, ["snapshot: not a dict"]
    # 深拷贝，避免就地修改调用方的 dict。
    snapshot = copy.deepcopy(snapshot)
    errors: list[str] = []
    legacy_availability = copy.deepcopy(snapshot.get("availability"))
    _normalize_device_baselines(snapshot, errors)

    # Canonicalize supported legacy provider state before any deep validation.
    # An explicit status, including an invalid one, always wins and is handled below.
    for provider_name in _PROVIDER_NAMES:
        provider = snapshot.get(provider_name)
        if not isinstance(provider, dict) or "status" in provider:
            continue
        legacy_available = provider.get("available")
        if legacy_available is True:
            provider["status"] = "ok"
        elif legacy_available is False:
            provider["status"] = "missing"

    # legacy iostat list → dict 必须在数值契约校验之前完成，
    # 否则 list 元素的 util/await/rate 非法值会绕过 _validate_numeric_dict。
    iostat_pr = snapshot.get("iostat")
    if isinstance(iostat_pr, dict) and iostat_pr.get("status") == "ok":
        ip = iostat_pr.get("parsed")
        if isinstance(ip, dict) and isinstance(ip.get("disks"), list):
            new_disks = {}
            for el in ip["disks"]:
                if isinstance(el, dict) and isinstance(el.get("name"), str):
                    new_disks[el["name"]] = {k: v for k, v in el.items() if k != "name"}
                else:
                    errors.append(
                        "iostat legacy list: non-dict/nameless element dropped"
                    )
            ip["disks"] = new_disks

    def _check_provider(
        name: str,
        disk_fields: tuple[str, ...] | None,
        elem_fields: tuple[str, ...] | None,
        list_key: str | None,
    ):
        pr = snapshot.get(name)
        if not isinstance(pr, dict) or pr.get("status") != "ok":
            return
        parsed = pr.get("parsed")
        if not isinstance(parsed, dict):
            snapshot[name] = {
                **pr,
                "status": "parse_failed",
                "parsed": None,
                "error": f"parsed not dict: {type(parsed).__name__}",
            }
            errors.append(f"{name}: parsed not dict")
            return
        # 校验数值字段
        try:
            pidstat_report_count: int | None = None
            if name == "pidstat":
                processes = parsed.get("processes")
                reports_required = isinstance(processes, list) and bool(processes)
                if "reports" in parsed or reports_required:
                    pidstat_report_count = _strict_json_int(
                        parsed.get("reports"), positive=True
                    )
            if disk_fields:
                report_count: float | None = None
                if name == "iostat" and "reports" in parsed:
                    report_count = _strict_float(parsed.get("reports"))
                    if (
                        report_count is None
                        or not report_count.is_integer()
                        or report_count <= 0
                    ):
                        raise ValueError("reports must be a positive integer")
                disks = parsed.get("disks")
                if disks is None:
                    parsed["disks"] = {}
                    disks = parsed["disks"]
                if not isinstance(disks, dict):
                    raise ValueError(f"disks not object ({type(disks).__name__})")
                for dname, metrics in disks.items():
                    if not isinstance(dname, str) or not dname:
                        raise ValueError(f"disk name is invalid: {dname!r}")
                    if not _validate_numeric_dict(metrics, disk_fields):
                        raise ValueError(f"disk {dname} has invalid metric")
                    if name == "iostat" and not any(
                        key in metrics and _strict_float(metrics[key]) is not None
                        for key in _IOSTAT_EVIDENCE_FIELDS
                    ):
                        raise ValueError(f"disk {dname} has no usable IO metric")
                    if name == "iostat":
                        field_counts = [
                            key for key in _IOSTAT_FIELD_SAMPLE_COUNTS if key in metrics
                        ]
                        if field_counts and "sample_count" not in metrics:
                            raise ValueError(
                                f"disk {dname} has per-field sample counts without sample_count"
                            )
                        total_count = _strict_float(metrics.get("sample_count"))
                        if field_counts and total_count is None:
                            raise ValueError(f"disk {dname} has invalid sample_count")
                        if total_count is not None:
                            if report_count is None:
                                raise ValueError(
                                    f"disk {dname} has sample_count without parsed.reports"
                                )
                            if total_count > report_count:
                                raise ValueError(
                                    f"disk {dname} has sample_count greater than parsed.reports"
                                )
                        for key in field_counts:
                            field_count = _strict_float(metrics[key])
                            if field_count is not None and field_count > total_count:
                                raise ValueError(
                                    f"disk {dname} has field sample count greater than sample_count"
                                )
                        for (
                            paired_key,
                            field_key,
                        ) in _IOSTAT_PAIRED_SAMPLE_BASES.items():
                            if paired_key not in metrics:
                                continue
                            if (
                                "util_sample_count" not in metrics
                                or field_key not in metrics
                            ):
                                raise ValueError(
                                    f"disk {dname} has {paired_key} without component counts"
                                )
                            paired_count = _strict_float(metrics[paired_key])
                            util_count = _strict_float(metrics["util_sample_count"])
                            metric_count = _strict_float(metrics[field_key])
                            if (
                                paired_count is None
                                or util_count is None
                                or metric_count is None
                            ):
                                raise ValueError(
                                    f"disk {dname} has invalid co-occurrence sample count"
                                )
                            if paired_count > min(util_count, metric_count):
                                raise ValueError(
                                    f"disk {dname} has co-occurrence count greater than component count"
                                )
            # list_key 存在但不是 list 时直接标记 parse_failed。
            # （覆盖 pidstat.processes / df.filesystems / nfs.mount_metrics，
            #  与 process_io_map.mappings 的保护对称，避免下游 .get 崩溃）。
            if elem_fields and list_key:
                seq = parsed.get(list_key)
                if seq is not None and not isinstance(seq, list):
                    raise ValueError(f"{list_key} not list ({type(seq).__name__})")
                # 显式 null 强制为空列表，避免下游遍历 None。
                if seq is None:
                    parsed[list_key] = []
                if isinstance(parsed.get(list_key), list):
                    for i, el in enumerate(parsed[list_key]):
                        if not _validate_numeric_dict(el, elem_fields):
                            raise ValueError(f"{list_key}[{i}] has invalid metric")
                        if name == "pidstat":
                            pid = _strict_json_int(el.get("pid"), positive=True)
                            sample_count = _strict_json_int(el.get("sample_count"))
                            active_sample_count = _strict_json_int(
                                el.get("active_sample_count")
                            )
                            if pidstat_report_count is None:
                                raise ValueError(
                                    f"{list_key}[{i}] requires positive parsed.reports"
                                )
                            if not (
                                active_sample_count
                                <= sample_count
                                <= pidstat_report_count
                            ):
                                raise ValueError(
                                    f"{list_key}[{i}] requires active_sample_count <= sample_count <= parsed.reports"
                                )
                            el["pid"] = pid
            # Network-provider mount_metrics must be a list. Deep values are
            # interpreted conservatively by each provider-specific rule.
            if name in {"nfs", "glusterfs"}:
                mm = parsed.get("mount_metrics")
                if mm is not None and not isinstance(mm, list):
                    raise ValueError("mount_metrics not list")
                if name == "glusterfs" and isinstance(mm, list):
                    for index, metric in enumerate(mm):
                        if not isinstance(metric, dict):
                            raise ValueError(f"mount_metrics[{index}] not object")
                        process_io = metric.get("process_io")
                        if process_io is None:
                            continue
                        if not isinstance(process_io, dict):
                            raise ValueError(
                                f"mount_metrics[{index}].process_io not object"
                            )
                        for field in (
                            "rchar",
                            "read_bytes",
                            "syscr",
                            "stable_pid_count",
                        ):
                            if field not in process_io:
                                continue
                            try:
                                _strict_json_int(process_io[field])
                            except ValueError as exc:
                                raise ValueError(
                                    f"mount_metrics[{index}].process_io.{field} "
                                    "must be a non-negative JSON integer"
                                ) from exc
        except (ValueError, TypeError) as exc:
            snapshot[name] = {
                **pr,
                "status": "parse_failed",
                "parsed": None,
                "error": str(exc),
            }
            errors.append(f"{name}: {exc}")

    _check_provider("iostat", _NUMERIC_FIELDS["iostat_disks"], None, None)
    _check_provider("nfs", None, _NUMERIC_FIELDS["nfs_metric"], "mount_metrics")
    _check_provider("glusterfs", None, None, None)
    # collector 从 `df -iP` 解析的 iuse_percent 形如 "92%"（带 %），
    # 会在下方 _strict_float 校验中被拒 → df 误判 parse_failed、inode 证据静默丢失。
    # 在校验前规范化：剥离 trailing "%" 并转 float。
    df_pr = snapshot.get("df")
    if isinstance(df_pr, dict) and df_pr.get("status") == "ok":
        df_parsed = df_pr.get("parsed")
        if isinstance(df_parsed, dict) and isinstance(
            df_parsed.get("filesystems"), list
        ):
            for fs in df_parsed["filesystems"]:
                if isinstance(fs, dict) and isinstance(fs.get("iuse_percent"), str):
                    raw = fs["iuse_percent"].strip()
                    try:
                        fs["iuse_percent"] = float(raw.rstrip("%"))
                    except ValueError:
                        pass  # 留给 _check_provider 标 parse_failed
    _check_provider("df", None, _NUMERIC_FIELDS["df_fs"], "filesystems")
    _check_provider("pidstat", None, _NUMERIC_FIELDS["pidstat_proc"], "processes")
    _check_provider("process_io_map", None, None, None)

    # process_io_map.mappings 必须是 dict 列表，关键观测计数必须是精确 JSON 整数。
    pmap = snapshot.get("process_io_map")
    if isinstance(pmap, dict) and pmap.get("status") == "ok":
        parsed = pmap.get("parsed")
        if isinstance(parsed, dict):
            mappings = parsed.get("mappings")
            if isinstance(mappings, list):
                observation_samples: int | None = None
                if "observation_samples" in parsed or mappings:
                    try:
                        observation_samples = _strict_json_int(
                            parsed.get("observation_samples"), positive=True
                        )
                    except ValueError as exc:
                        snapshot["process_io_map"] = {
                            **pmap,
                            "status": "parse_failed",
                            "parsed": None,
                            "error": f"invalid observation_samples: {exc}",
                        }
                        errors.append(
                            f"process_io_map: invalid observation_samples: {exc}"
                        )
                        observation_samples = None
                        mappings = []
                cleaned: list[dict] = []
                for i, mapping in enumerate(mappings):
                    if not isinstance(mapping, dict):
                        errors.append(
                            f"process_io_map: mappings[{i}] not object, dropped"
                        )
                        continue
                    try:
                        pid = _strict_json_int(mapping.get("pid"), positive=True)
                        observation_count = _strict_json_int(
                            mapping.get("observation_count")
                        )
                        if (
                            observation_samples is None
                            or observation_count > observation_samples
                        ):
                            raise ValueError(
                                "observation_count exceeds observation_samples"
                            )
                    except ValueError as exc:
                        errors.append(
                            f"process_io_map: mappings[{i}] invalid count/PID ({exc}), dropped"
                        )
                        continue
                    mapping["pid"] = pid
                    cleaned.append(mapping)
                if snapshot.get("process_io_map") is pmap:
                    parsed["mappings"] = cleaned
                    identity_error = _process_map_identity_error(parsed, cleaned)
                    if identity_error is not None:
                        snapshot["process_io_map"] = {
                            **pmap,
                            "status": "parse_failed",
                            "parsed": None,
                            "error": identity_error,
                        }
                        errors.append(f"process_io_map: {identity_error}")
            elif mappings is not None:
                snapshot["process_io_map"] = {
                    **pmap,
                    "status": "parse_failed",
                    "parsed": None,
                    "error": "mappings not list",
                }
                errors.append("process_io_map: mappings not list")

    # target 顶层字段必须是 dict（{path: str}），
    # 否则 R400 第 945 行 (snapshot.get("target") or {}).get("path") 会在
    # 非空字符串/数字/list/bool 上崩溃（'str'/'bool' object has no attribute 'get'）。
    tgt = snapshot.get("target")
    if not isinstance(tgt, dict):
        if tgt is not None:
            errors.append(f"target: not a dict ({type(tgt).__name__}), ignored")
        snapshot["target"] = {}

    # mounts 顶层字段必须是 dict 列表，否则 R200/R300 无法安全迭代。
    mnts = snapshot.get("mounts")
    if not isinstance(mnts, list):
        if mnts is not None:
            errors.append(f"mounts: not a list ({type(mnts).__name__}), ignored")
        snapshot["mounts"] = []
    else:
        cleaned_m = [m for m in mnts if isinstance(m, dict)]
        if len(cleaned_m) != len(mnts):
            errors.append(
                f"mounts: dropped {len(mnts) - len(cleaned_m)} non-object entry/entries"
            )
            snapshot["mounts"] = cleaned_m

    mounts_provider = snapshot.get("mounts_provider")
    if not isinstance(mounts_provider, dict):
        inferred_status = "ok" if snapshot["mounts"] else "missing"
        if isinstance(legacy_availability, dict):
            legacy_missing = legacy_availability.get("missing")
            legacy_partial = legacy_availability.get("partial")
            legacy_errors = legacy_availability.get("errors")
            legacy_missing = legacy_missing if isinstance(legacy_missing, list) else []
            legacy_partial = legacy_partial if isinstance(legacy_partial, list) else []
            legacy_errors = legacy_errors if isinstance(legacy_errors, list) else []
            if "mounts" in legacy_missing:
                inferred_status = "missing"
            for item in legacy_partial:
                text = str(item)
                if text.startswith("mounts:"):
                    inferred_status = (
                        "unsupported" if "unsupported" in text else "empty"
                    )
            for item in legacy_errors:
                text = str(item)
                if not text.startswith("mounts:"):
                    continue
                inferred_status = next(
                    (
                        status
                        for status in (
                            "permission_denied",
                            "command_failed",
                            "parse_failed",
                        )
                        if status in text
                    ),
                    "command_failed",
                )
        snapshot["mounts_provider"] = {
            "source": "mounts",
            "status": inferred_status,
            "parsed": copy.deepcopy(snapshot["mounts"]),
        }
    else:
        status = mounts_provider.get("status")
        if isinstance(status, str) and status in {"ok", "empty"}:
            parsed_mounts = mounts_provider.get("parsed")
            if status == "empty" and snapshot["mounts"]:
                mounts_provider["status"] = "parse_failed"
                mounts_provider["parsed"] = None
                mounts_provider["error"] = (
                    "empty status conflicts with non-empty mounts"
                )
                errors.append(
                    "mounts_provider: empty status conflicts with non-empty mounts"
                )
            elif status == "ok" and not snapshot["mounts"]:
                mounts_provider["status"] = "parse_failed"
                mounts_provider["parsed"] = None
                mounts_provider["error"] = "ok status conflicts with empty mounts"
                errors.append("mounts_provider: ok status conflicts with empty mounts")
            elif parsed_mounts is None:
                mounts_provider["parsed"] = copy.deepcopy(snapshot["mounts"])
            elif not isinstance(parsed_mounts, list) or not all(
                isinstance(item, dict) for item in parsed_mounts
            ):
                mounts_provider["status"] = "parse_failed"
                mounts_provider["parsed"] = None
                mounts_provider["error"] = "parsed must be a list of objects"
                errors.append("mounts_provider: parsed must be a list of objects")
            elif parsed_mounts != snapshot["mounts"]:
                mounts_provider["status"] = "parse_failed"
                mounts_provider["parsed"] = None
                mounts_provider["error"] = "parsed differs from top-level mounts"
                errors.append("mounts_provider: parsed differs from top-level mounts")

    # 深层校验 diskstats_sample；每个 sample 的 disks 必须是 dict，
    ds = snapshot.get("diskstats_sample")
    if isinstance(ds, list):
        cleaned_ds = []
        bad = 0
        for s in ds:
            if not isinstance(s, dict):
                bad += 1
                continue
            disks = s.get("disks")
            if not isinstance(disks, dict):
                bad += 1
                continue
            # 设备值必须都是 dict；非法值整体丢弃该 sample（避免 _compute_disk_rates 崩溃）
            if not all(
                isinstance(v, dict)
                and _validate_numeric_dict(v, _NUMERIC_FIELDS["diskstats"])
                for v in disks.values()
            ):
                bad += 1
                continue
            # timestamp 必须可转有限 float
            ts = s.get("timestamp", 0)
            try:
                fts = float(ts)
                if fts != fts or fts in (float("inf"), float("-inf")) or fts < 0:
                    raise ValueError
                s = {**s, "timestamp": fts}
            except (TypeError, ValueError, OverflowError):
                bad += 1
                continue
            cleaned_ds.append(s)
        # 始终写回 cleaned_ds，保留 timestamp 等字段的规范化结果。
        # 也必须落盘，否则 _compute_disk_rates 拿到原始 str timestamp 会 int>str 崩溃。
        snapshot["diskstats_sample"] = cleaned_ds
        if bad:
            errors.append(f"diskstats_sample: dropped {bad} malformed sample(s)")
        if len(cleaned_ds) >= 2 and any(
            cleaned_ds[index]["timestamp"] <= cleaned_ds[index - 1]["timestamp"]
            for index in range(1, len(cleaned_ds))
        ):
            snapshot["diskstats_sample"] = []
            errors.append("diskstats_sample: timestamps must be strictly increasing")
        elif len(cleaned_ds) >= 2:
            first_disks = cleaned_ds[0]["disks"]
            last_disks = cleaned_ds[-1]["disks"]
            counter_fields = (
                "reads_completed",
                "writes_completed",
                "sectors_read",
                "sectors_written",
                "time_reading_ms",
                "time_writing_ms",
                "time_io_ms",
                "weighted_time_io_ms",
            )
            new_devices = sorted(set(last_disks) - set(first_disks))
            reset_devices: list[str] = []
            for device in set(first_disks) & set(last_disks):
                try:
                    if any(
                        float(last_disks[device].get(key, 0))
                        < float(first_disks[device].get(key, 0))
                        for key in counter_fields
                    ):
                        reset_devices.append(device)
                except (TypeError, ValueError, OverflowError):
                    reset_devices.append(device)
            if new_devices:
                errors.append(
                    f"diskstats_sample: new devices lack baseline: {new_devices}"
                )
            if reset_devices:
                errors.append(
                    f"diskstats_sample: counter reset devices ignored: {sorted(reset_devices)}"
                )
    elif ds is not None:
        snapshot["diskstats_sample"] = []
        errors.append("diskstats_sample: not a list")

    # legacy list→dict 转换已提前到数值契约校验之前。

    # iostat 各 sample_count 必须是非负整数，不能直接信任外部字符串。
    iostat_pr = snapshot.get("iostat")
    if isinstance(iostat_pr, dict) and iostat_pr.get("status") == "ok":
        ip = iostat_pr.get("parsed")
        if isinstance(ip, dict) and isinstance(ip.get("disks"), dict):
            for dname, metrics in ip["disks"].items():
                if not isinstance(metrics, dict):
                    continue
                for count_field in ("sample_count", *_IOSTAT_FIELD_SAMPLE_COUNTS):
                    sc = metrics.get(count_field)
                    if sc is None:
                        continue
                    try:
                        value = _strict_float(sc)
                        if value is None or value < 0 or not value.is_integer():
                            raise ValueError
                        metrics[count_field] = int(value)
                    except (TypeError, ValueError, OverflowError):
                        metrics.pop(count_field, None)
                        errors.append(f"iostat disk {dname}: bad {count_field} dropped")

    # 从 provider 实际状态重建 availability，不信任调用方传入的值。
    # df/memory are post-window static context. They must retain their own
    # timestamps and status, but cannot be required to fit the dynamic window.
    _STATIC_CONTEXT_PROVIDERS = {"df", "memory"}
    _VALID_STATUS = {
        "ok",
        "missing",
        "permission_denied",
        "command_failed",
        "parse_failed",
        "empty",
        "unsupported",
    }
    avail = snapshot.get("availability")
    if not isinstance(avail, dict):
        avail = {"missing": [], "partial": [], "errors": []}
    missing, partial, verr = set(), set(), set()
    for k in ("missing", "partial", "errors"):
        v = avail.get(k)
        if isinstance(v, list):
            avail[k] = [str(x) for x in v if not isinstance(x, (dict, list))]
        elif v is not None:
            avail[k] = []
    for pname in _PROVIDER_NAMES:
        pr = snapshot.get(pname)
        if not isinstance(pr, dict):
            missing.add(pname)
            continue
        st = pr.get("status")
        if st is None:
            missing.add(pname)
        elif not isinstance(st, str) or st not in _VALID_STATUS:
            # unhashable status（list/dict）或非法枚举应标记 parse_failed。
            verr.add(f"{pname}: invalid status {st!r}")
            pr["status"] = "parse_failed"
            pr.setdefault("error", f"invalid status: {st!r}")
            errors.append(f"{pname}: parse_failed")
        elif st == "missing":
            missing.add(pname)
        elif st in ("permission_denied", "command_failed", "parse_failed"):
            errors.append(f"{pname}: {st}")
        elif st in ("empty", "unsupported"):
            partial.add(f"{pname}: {st}")
    # 从 provider 实际状态完全重建 availability，不与调用方残留值合并，
    # 避免 R000 报告 stale missing 同时 R100 用该 provider 输出 high 的自相矛盾。
    avail["missing"] = sorted(missing)
    avail["partial"] = sorted(partial)
    avail["errors"] = sorted(set(errors))
    # collected_at 校验
    ca = snapshot.get("collected_at")
    if not isinstance(ca, str) or not ca.strip():
        verr.add("collected_at missing or non-string")
    elif _parse_iso(ca) is None:
        verr.add("collected_at must be ISO8601 with timezone")
    window = snapshot.get("window")
    if not isinstance(window, dict) or _snapshot_interval(snapshot) is None:
        verr.add(
            "window.start/end must be increasing ISO8601 timestamps with timezone and anchored to collected_at"
        )
    else:
        for pname in _PROVIDER_NAMES:
            if pname in _STATIC_CONTEXT_PROVIDERS:
                continue
            provider = snapshot.get(pname)
            if not isinstance(provider, dict):
                continue
            provider_status = provider.get("status")
            if provider_status != "ok" and not (
                pname == "mounts_provider" and provider_status == "empty"
            ):
                continue
            if provider.get("started_at") is None and provider.get("ended_at") is None:
                continue
            if _provider_interval(snapshot, pname) is None:
                verr.add(f"{pname}: invalid or outside snapshot.window")
    snapshot["availability"] = avail
    # validation_errors 与 provider errors 分离并单次构造，避免重复追加。
    validation_errors = sorted(set(errors) | verr)
    return snapshot, validation_errors


_VALID_EXPERIMENT_RESULTS = {"improved", "no_change", "worse", "inconclusive"}


_CERTIFIED_PROFILE_SCOPE = "matched_workload_device_timeline"


_CERTIFIED_PROFILE_PROVENANCE = {
    "device_free_percent": {
        ("profiler_timeline", "device_idle_interval_ratio"),
        ("profiler_database", "database_device_free_metric"),
    },
    "mte2_ratio": {
        ("profiler_database", "workload_total_cycle_ratio"),
    },
}


def _profile_provenance_error(metric: str, value: Any) -> str | None:
    """Return why one dynamic profile metric lacks certifying provenance."""
    if not isinstance(value, dict):
        return "must be an object"
    source_type = value.get("source_type")
    extraction_method = value.get("extraction_method")
    if not isinstance(source_type, str) or not isinstance(extraction_method, str):
        return "source_type and extraction_method must be strings"
    if (source_type, extraction_method) not in _CERTIFIED_PROFILE_PROVENANCE.get(
        metric, set()
    ):
        return "has an unsupported source_type/extraction_method pair"
    if value.get("metric") != metric:
        return f"metric must equal {metric!r}"
    artifact_id = value.get("artifact_id")
    if (
        not isinstance(artifact_id, str)
        or not artifact_id.strip()
        or len(artifact_id) > 4096
    ):
        return "artifact_id must be a non-empty string of at most 4096 characters"
    device_id = value.get("device_id")
    if not isinstance(device_id, int) or isinstance(device_id, bool) or device_id < 0:
        return "device_id must be a non-negative JSON integer"
    return None


def _audit_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(
            f"{field} must be a non-empty string of at most 4096 characters"
        )
    return value


def _audit_window(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    start = value.get("start")
    end = value.get("end")
    start_epoch = _parse_iso(start)
    end_epoch = _parse_iso(end)
    if start_epoch is None or end_epoch is None or end_epoch <= start_epoch:
        raise ValueError(f"{field}.start/end must be increasing timezone-aware ISO8601")
    return {"start": start, "end": end}


def _audit_target(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    pid = value.get("pid")
    if pid is not None:
        pid = _strict_json_int(pid, positive=True)
    path = value.get("path")
    if path is not None and (not isinstance(path, str) or not path.strip()):
        raise ValueError(f"{field}.path must be null or a non-empty string")
    return {"pid": pid, "path": path}


def _normalize_overlap_provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("overlap_provenance must be an object")
    artifact_id = _audit_string(
        value.get("artifact_id"), "overlap_provenance.artifact_id"
    )
    device_id = _strict_json_int(value.get("device_id"))
    if value.get("metric") != "device_free_percent":
        raise ValueError("overlap_provenance.metric must equal 'device_free_percent'")
    if value.get("extraction_method") != "timeline_interval_overlap":
        raise ValueError(
            "overlap_provenance.extraction_method must equal 'timeline_interval_overlap'"
        )
    host_rule_ids = value.get("host_rule_ids")
    if (
        not isinstance(host_rule_ids, list)
        or not host_rule_ids
        or any(
            not isinstance(rule, str) or rule not in {"R100", "R200", "R300", "R400"}
            for rule in host_rule_ids
        )
    ):
        raise ValueError(
            "overlap_provenance.host_rule_ids must be a non-empty R100-R400 string list"
        )
    return {
        "artifact_id": artifact_id,
        "device_id": device_id,
        "metric": "device_free_percent",
        "extraction_method": "timeline_interval_overlap",
        "host_rule_ids": sorted(set(host_rule_ids)),
        "host_evidence_interval": _audit_window(
            value.get("host_evidence_interval"),
            "overlap_provenance.host_evidence_interval",
        ),
        "device_evidence_interval": _audit_window(
            value.get("device_evidence_interval"),
            "overlap_provenance.device_evidence_interval",
        ),
        "target": _audit_target(value.get("target"), "overlap_provenance.target"),
    }


def _normalize_experiment_observation(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    raw_metric = value.get("device_free_percent")
    metric = _strict_float(raw_metric)
    if metric is None or not 0 <= metric <= 100:
        raise ValueError(f"{field}.device_free_percent must be in [0,100]")
    return {
        "artifact_id": _audit_string(value.get("artifact_id"), f"{field}.artifact_id"),
        "window": _audit_window(value.get("window"), f"{field}.window"),
        "device_free_percent": metric,
    }


def _normalize_controlled_experiment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("controlled_experiment must be an object")
    result = value.get("result")
    if not isinstance(result, str) or result not in _VALID_EXPERIMENT_RESULTS:
        raise ValueError("controlled_experiment.result is not a valid enum")
    if value.get("metric") != "device_free_percent":
        raise ValueError(
            "controlled_experiment.metric must equal 'device_free_percent'"
        )
    baseline = _normalize_experiment_observation(
        value.get("baseline"), "controlled_experiment.baseline"
    )
    treatment = _normalize_experiment_observation(
        value.get("treatment"), "controlled_experiment.treatment"
    )
    if result == "improved" and not (
        treatment["device_free_percent"] < baseline["device_free_percent"]
    ):
        raise ValueError(
            "controlled_experiment improved result requires treatment device_free_percent below baseline"
        )
    return {
        "result": result,
        "experiment_id": _audit_string(
            value.get("experiment_id"), "controlled_experiment.experiment_id"
        ),
        "device_id": _strict_json_int(value.get("device_id")),
        "metric": "device_free_percent",
        "action": _audit_string(value.get("action"), "controlled_experiment.action"),
        "target": _audit_target(value.get("target"), "controlled_experiment.target"),
        "baseline": baseline,
        "treatment": treatment,
    }


def _profile_metric_is_certified(profile: dict, metric: str) -> bool:
    """Require an audited timeline scope and metric-specific profiler provenance."""
    if metric not in profile:
        return False
    window = profile.get("profile_window")
    if not isinstance(window, dict) or window.get("scope") != _CERTIFIED_PROFILE_SCOPE:
        return False
    provenance = profile.get("provenance")
    if not isinstance(provenance, dict):
        return False
    return _profile_provenance_error(metric, provenance.get(metric)) is None


def _certified_profile_metrics(profile: dict) -> list[str]:
    return sorted(
        metric
        for metric in _CERTIFIED_PROFILE_PROVENANCE
        if _profile_metric_is_certified(profile, metric)
    )


def _normalize_profile(profile: dict | None) -> dict:
    """校验 profile 数值字段与嵌套 conduction_evidence。

    保留单返回值（clean dict）以兼容既有调用；完整错误见 _normalize_profile_with_errors。
    """
    return _normalize_profile_with_errors(profile)[0]


def _normalize_profile_with_errors(profile: dict | None) -> tuple[dict, list[str]]:
    """校验 profile 并返回 (clean, errors)，由调用方公开 errors。

    conduction_evidence 严格契约：
      - io_npu_overlap_observed 只接受真正的 JSON bool True（'false'/'0'/0/1/字符串均不算）。
      - controlled_experiment 必须是对象；result 必须是明确枚举，仅 'improved' 升级。
    非法嵌套值丢弃该证据并记录 error，不得崩溃，也不得升级 high 置信度。
    """
    if profile is None:
        return {}, []
    if not isinstance(profile, dict):
        return {}, [
            f"profile 顶层必须是 JSON object，实际为 {type(profile).__name__}，已忽略"
        ]
    clean = dict(profile)
    errors: list[str] = []
    # 数值范围校验；bool 已被 _strict_float 拒绝。
    _RANGES = {"device_free_percent": (0.0, 100.0), "mte2_ratio": (0.0, 1.0)}
    for key, (lo, hi) in _RANGES.items():
        if key in clean:
            raw = clean[key]
            try:
                val = _strict_float(raw)
                if val is None or val < lo or val > hi:
                    raise ValueError(f"{key} out of range [{lo},{hi}]")
                clean[key] = val
            except (ValueError, TypeError):
                errors.append(f"{key}={raw!r} 非法或越界，已丢弃")
                clean.pop(key, None)
    profile_window = clean.get("profile_window")
    if profile_window is not None:
        if not isinstance(profile_window, dict):
            errors.append("profile_window 非对象，已丢弃")
            clean.pop("profile_window", None)
        else:
            start = profile_window.get("start")
            end = profile_window.get("end")
            start_epoch = _parse_iso(start)
            end_epoch = _parse_iso(end)
            if start_epoch is None or end_epoch is None or end_epoch <= start_epoch:
                errors.append(
                    "profile_window.start/end 必须是递增且带时区的 ISO8601 时间，已丢弃"
                )
                clean.pop("profile_window", None)
            else:
                normalized_window = {"start": start, "end": end}
                scope = profile_window.get("scope")
                if isinstance(scope, str) and scope:
                    normalized_window["scope"] = scope
                clean["profile_window"] = normalized_window
    dynamic_metrics = sorted(
        metric for metric in _CERTIFIED_PROFILE_PROVENANCE if metric in clean
    )
    if dynamic_metrics:
        normalized_window = clean.get("profile_window")
        scope = (
            normalized_window.get("scope")
            if isinstance(normalized_window, dict)
            else None
        )
        if scope != _CERTIFIED_PROFILE_SCOPE:
            errors.append(
                f"profile_window.scope={scope!r} 不是认证 scope {_CERTIFIED_PROFILE_SCOPE!r}；动态指标仅可作非认证候选"
            )

        raw_provenance = clean.get("provenance")
        clean_provenance: dict[str, Any] = {}
        if not isinstance(raw_provenance, dict):
            errors.append("provenance 非对象或缺失；动态指标仅可作非认证候选")
        else:
            for metric in dynamic_metrics:
                entry = raw_provenance.get(metric)
                reason = _profile_provenance_error(metric, entry)
                if reason is None:
                    clean_provenance[metric] = dict(entry)
                else:
                    errors.append(
                        f"provenance.{metric} {reason}；该指标仅可作非认证候选"
                    )
        clean["provenance"] = clean_provenance
    ce = clean.get("conduction_evidence")
    if ce is None:
        return clean, errors
    if not isinstance(ce, dict):
        errors.append("conduction_evidence 非对象，已丢弃")
        clean.pop("conduction_evidence", None)
        return clean, errors
    clean_ce: dict[str, Any] = {}
    ov = ce.get("io_npu_overlap_observed")
    if ov is True:
        try:
            overlap_provenance = _normalize_overlap_provenance(
                ce.get("overlap_provenance")
            )
        except (ValueError, TypeError) as exc:
            errors.append(
                f"io_npu_overlap_observed 缺少合法 overlap_provenance（{exc}），忽略"
            )
        else:
            clean_ce["io_npu_overlap_observed"] = True
            clean_ce["overlap_provenance"] = overlap_provenance
    elif ov is False or ov is None:
        pass  # False/缺失：不置位
    else:
        errors.append(f"io_npu_overlap_observed={ov!r} 非 boolean，忽略")
    exp = ce.get("controlled_experiment")
    if exp is not None:
        try:
            clean_ce["controlled_experiment"] = _normalize_controlled_experiment(exp)
        except (ValueError, TypeError) as exc:
            errors.append(f"controlled_experiment 非法（{exc}），忽略")
    clean["conduction_evidence"] = clean_ce
    return clean, errors


def validate_analysis_request(
    snapshot: dict, profile: dict | None = None
) -> tuple[dict, dict, list[str], list[str], str | None]:
    """所有 mode（all/R000-R500）与 eval 共用的唯一输入契约入口。

    返回 (normalized_snapshot, normalized_profile, validation_errors,
          profile_validation_errors, fatal_error)。
    fatal_error 非空表示不可恢复契约错误（顶层非 dict / unsupported schema major），
    调用方应返回结构化 error + 非零退出。其余错误为局部降级，分析继续。
    """
    if not isinstance(snapshot, dict):
        return (
            {},
            {},
            [],
            [],
            f"snapshot must be a JSON object, got {type(snapshot).__name__}",
        )
    try:
        _validate_json_resources(snapshot)
    except ValueError as exc:
        return {}, {}, [], [], f"snapshot resource limit exceeded: {exc}"
    profile_resource_error: str | None = None
    if profile is not None:
        try:
            _validate_json_resources(profile)
        except ValueError as exc:
            profile_resource_error = f"profile resource limit exceeded: {exc}"
            profile = None
    sv = snapshot.get("schema_version")
    if sv is None:
        # 缺 schema_version 时按 legacy 1.x 兼容，并显式给出 warning。
        sv = "1.0"
        legacy_warn = "schema_version 缺失，按 legacy 1.x 处理（建议显式标注）"
    else:
        legacy_warn = None
        _major_val, sv_err = _validate_schema_version(sv)
        if sv_err is not None:
            return (
                {},
                {},
                [],
                [],
                sv_err,
            )
    snapshot, verr = normalize_and_validate(snapshot)
    if legacy_warn:
        verr = [legacy_warn] + list(verr)
    # collected_at 缺失时记录所有 mode 可见的 validation_error，但不阻断分析。
    # （schema 仍可用；窗口分析在无 timestamp 时退化为"无法证伪"，见 interval_overlap_ratio）。
    profile, pverr = _normalize_profile_with_errors(profile)
    if profile_resource_error is not None:
        pverr.append(profile_resource_error)
    dynamic_profile_fields = {
        key for key in ("device_free_percent", "mte2_ratio") if key in profile
    }
    if dynamic_profile_fields and not _profile_window_matches_snapshot(
        snapshot, profile
    ):
        if _profile_interval(profile) is None:
            reason = "缺少合法 profile_window"
        else:
            reason = "profile_window 与 snapshot.window 重叠不足 50%"
        fields = ", ".join(sorted(dynamic_profile_fields))
        pverr.append(f"{fields} {reason}，动态指标已丢弃")
        for key in dynamic_profile_fields:
            profile.pop(key, None)
    conduction = profile.get("conduction_evidence")
    if isinstance(conduction, dict) and conduction.get("io_npu_overlap_observed"):
        if not _profile_window_matches_snapshot(snapshot, profile):
            conduction.pop("io_npu_overlap_observed", None)
            pverr.append(
                "io_npu_overlap_observed 缺少与 snapshot.window 有效重叠的 profile_window，已忽略"
            )
    pverr = sorted(set(pverr))
    return snapshot, profile, verr, pverr, None


def _target_pid_scope(snapshot: dict) -> set[int] | None:
    """Return the explicit target PID plus identity-bound, chained descendants."""
    target = snapshot.get("target")
    raw_pid = target.get("pid") if isinstance(target, dict) else None
    if not isinstance(raw_pid, int) or isinstance(raw_pid, bool) or raw_pid <= 0:
        return None
    allowed = {raw_pid}
    parsed = _parsed(_provider(snapshot, "process_io_map"))
    if not isinstance(parsed, dict):
        return allowed
    candidate_parents: dict[int, set[int]] = defaultdict(set)
    for entry in parsed.get("pid_tree", []) or []:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("pid")
        boot_id = entry.get("boot_id")
        starttime = entry.get("pid_starttime_ticks")
        parent_pid = entry.get("parent_pid")
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 0
            and pid != raw_pid
            and entry.get("role") == "descendant"
            and isinstance(parent_pid, int)
            and not isinstance(parent_pid, bool)
            and parent_pid > 0
            and parent_pid != pid
            and isinstance(boot_id, str)
            and _BOOT_ID_PATTERN.fullmatch(boot_id)
            and isinstance(starttime, int)
            and not isinstance(starttime, bool)
            and starttime >= 0
        ):
            candidate_parents[pid].add(parent_pid)

    # Resolve only a unique parent chain rooted at the explicit target. Cycles,
    # disconnected entries, duplicate conflicting parents, and legacy entries without
    # parent_pid remain outside the trusted workload scope.
    pending = {
        pid: next(iter(parents))
        for pid, parents in candidate_parents.items()
        if len(parents) == 1
    }
    while pending:
        resolved = {pid for pid, parent_pid in pending.items() if parent_pid in allowed}
        if not resolved:
            break
        allowed.update(resolved)
        for pid in resolved:
            pending.pop(pid, None)
    return allowed
