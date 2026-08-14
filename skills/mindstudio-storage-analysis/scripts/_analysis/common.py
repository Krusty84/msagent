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
"""Shared finding, provider, device, and time-window helpers."""

from __future__ import annotations

import math
import re
from typing import Any

# 与 collector 共享 schema 版本策略
try:
    import collect_io_snapshot as _c

    SUPPORTED_MAJOR = _c.SUPPORTED_MAJOR
except Exception:  # noqa: BLE001 - 独立运行时不强依赖 collector
    SUPPORTED_MAJOR = 1

RULE_IDS = ["R000", "R100", "R200", "R300", "R400", "R500"]


_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _finding(
    rule_id: str,
    severity: str,
    confidence: str,
    summary: str,
    evidence_fields: list[str] | None = None,
    missing_evidence: list[str] | None = None,
    next_checks: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造一条结构化 finding。"""
    f: dict[str, Any] = {
        "rule_id": rule_id,
        "severity": severity,  # info / low / medium / high
        "confidence": confidence,  # low / medium / high / none
        "summary": summary,
        "evidence_fields": evidence_fields or [],
        "missing_evidence": missing_evidence or [],
        "recommended_next_checks": next_checks or [],
    }
    f.update(extra)
    return f


def _provider(snapshot: dict, name: str) -> dict:
    """安全取 provider 块，兼容旧 schema（available 布尔）和新 schema（status 字符串）。"""
    pr = snapshot.get(name, {}) or {}
    return pr if isinstance(pr, dict) else {}


def _status(pr: dict) -> str:
    """统一获取 provider 状态。新 schema 用 status；旧 schema 退化推断。"""
    s = pr.get("status")
    if isinstance(s, str) and s:
        return s
    # 旧 schema 兼容
    if pr.get("available") is True:
        return "ok"
    if pr.get("available") is False:
        return "missing"
    return "unknown"


def _parsed(pr: dict) -> Any:
    return pr.get("parsed")


_PROVIDER_NAMES = (
    "mounts_provider",
    "iostat",
    "pidstat",
    "nfs",
    "glusterfs",
    "df",
    "process_io_map",
    "memory",
    "block_devices",
)


_SCHEMA_VERSION_RE = re.compile(r"^\d+\.\d+$")


def _major(schema_version: str) -> int:
    try:
        return int(str(schema_version).split(".")[0])
    except (ValueError, IndexError):
        return -1


def _validate_schema_version(sv: Any) -> tuple[int, str | None]:
    """严格校验 `<major>.<minor>` 格式与受支持的 major 版本。"""
    if not isinstance(sv, str) or not sv or not _SCHEMA_VERSION_RE.match(sv):
        return -1, f"malformed schema_version {sv!r} (expect digits.digits)"
    major_text = sv.split(".", 1)[0]
    if len(major_text) > 9:
        return -1, "malformed schema_version: major component is too long"
    try:
        major = int(major_text)
    except ValueError:
        return -1, "malformed schema_version: major component is not an integer"
    if major != SUPPORTED_MAJOR:
        return (
            major,
            f"unsupported schema_version {sv}: major {major} != supported {SUPPORTED_MAJOR}",
        )
    return major, None


def _delta(d1: dict, d0: dict, key: str) -> float:
    """两次采样某字段的差值（容错非数值/缺失）。"""
    try:
        return float(d1.get(key, 0)) - float(d0.get(key, 0))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _f(v: Any, default: float = 0.0) -> float:
    """安全转 float；非法（字符串/None/NaN/Inf）→ default。"""
    if isinstance(v, bool):
        return default
    try:
        out = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / Inf
        return default
    return out


def _snapshot_duration(snapshot: dict) -> float | None:
    """Return finite positive snapshot duration seconds when available."""
    duration = _f(snapshot.get("duration_seconds"), default=0.0)
    if duration > 0:
        return duration
    interval = _snapshot_interval(snapshot)
    if interval is not None:
        return interval[1] - interval[0]
    return None


def _format_device_pid_map(items: dict[str, list], limit: int = 5) -> str:
    """Format device<-pids map without hiding count/preview mismatches."""
    pairs = list(items.items())
    shown = "; ".join(f"{dev}<-{pids}" for dev, pids in pairs[:limit])
    if len(pairs) > limit:
        shown += f"; ...（另 {len(pairs) - limit} 个）"
    return shown


def _canonical_dev(name: str) -> str:
    """把设备名/挂载源归一到整盘/逻辑主设备名（analyzer 侧，与 collector 对齐）。

    iostat 设备名已是整盘（sda/dm-0/md0/nvme0n1），直接返回。
    挂载源（/dev/sda1、/dev/mapper/*、/dev/md0）按启发式折叠：
      /dev/sda1 → sda；/dev/nvme0n1p2 → nvme0n1；/dev/dm-0 → dm-0；/dev/md0 → md0。
    无法仅靠名解析的 /dev/mapper/X 保留原值（应由 collector 的 sysfs 解析处理）。
    """
    if not isinstance(name, str) or not name:
        return ""
    n = name.removeprefix("/dev/")
    # nvme0n1p2 → nvme0n1
    m = re.match(r"^(nvme\d+n\d+)p\d+$", n)
    if m:
        return m.group(1)
    # mmcblk0p1 / rbd0p1 / nbd0p1 → corresponding whole device.
    m = re.match(r"^((?:mmcblk|rbd|nbd)\d+)p\d+$", n)
    if m:
        return m.group(1)
    # md0p1 / dm-1p2 → md0 / dm-1（md/dm 分区折叠）
    m = re.match(r"^((?:dm-)?\d+|md\d+)p\d+$", n)
    if m:
        return m.group(1)
    # sda1 / vdb3 → sda / vdb（整盘无数字后缀）
    m = re.match(r"^((?:sd|vd|hd|xvd)[a-z]+)\d+$", n)
    if m:
        return m.group(1)
    return n


def interval_overlap_ratio(a: tuple[Any, Any] | None, b: tuple[Any, Any] | None) -> float:
    """计算两个时间区间相对较短区间的重叠占比。

    任一区间缺失/非法 → 0.0（证据不足，不能确认同窗）。
    两者均存在：完全重叠 1.0，不重叠 0.0，部分重叠 ∈ (0,1)。
    """
    if a is None or b is None:
        return 0.0
    try:
        a0, a1 = float(a[0]), float(a[1])
        b0, b1 = float(b[0]), float(b[1])
    except (TypeError, ValueError, IndexError):
        return 0.0
    if any(t != t or t in (float("inf"), float("-inf")) for t in (a0, a1, b0, b1)):
        return 0.0
    if a1 <= a0 or b1 <= b0:
        return 0.0
    overlap = max(0.0, min(a1, b1) - max(a0, b0))
    shorter = min(a1 - a0, b1 - b0)
    return overlap / shorter if shorter > 0 else 0.0


def intervals_have_common_overlap(
    *intervals: tuple[Any, Any] | None,
    min_overlap_seconds: float = 1.0,
    min_overlap_ratio: float = 0.5,
) -> bool:
    """所有区间是否具有足量公共交集；缺失、非法或瞬时擦边一律 False。"""
    if not intervals or any(interval is None for interval in intervals):
        return False
    starts: list[float] = []
    ends: list[float] = []
    try:
        for interval in intervals:
            if interval is None:
                return False
            start, end = float(interval[0]), float(interval[1])
            if any(value != value or value in (float("inf"), float("-inf")) for value in (start, end)):
                return False
            if end <= start:
                return False
            starts.append(start)
            ends.append(end)
    except (TypeError, ValueError, IndexError):
        return False
    overlap = min(ends) - max(starts)
    shortest = min(end - start for start, end in zip(starts, ends, strict=True))
    if overlap < min_overlap_seconds or shortest <= 0:
        return False
    return (overlap / shortest) >= min_overlap_ratio


def intervals_overlap_or_are_adjacent(
    first: tuple[Any, Any] | None,
    second: tuple[Any, Any] | None,
    max_gap_seconds: float = 2.0,
) -> bool:
    """Accept overlapping provider windows or back-to-back reads within tolerance."""
    if first is None or second is None:
        return False
    try:
        first_start, first_end = float(first[0]), float(first[1])
        second_start, second_end = float(second[0]), float(second[1])
        max_gap = float(max_gap_seconds)
    except (TypeError, ValueError, IndexError, OverflowError):
        return False
    values = (first_start, first_end, second_start, second_end, max_gap)
    if not all(math.isfinite(value) for value in values):
        return False
    if first_end <= first_start or second_end <= second_start or max_gap < 0:
        return False
    if first_end >= second_start and second_end >= first_start:
        return True
    gap = second_start - first_end if first_end < second_start else first_start - second_end
    return gap <= max_gap


def _provider_interval(snapshot: dict, name: str) -> tuple[Any, Any] | None:
    """取某 provider 的 (started_at, ended_at) 区间；缺失或不可解析返回 None。"""
    top = _snapshot_interval(snapshot)
    if top is None:
        return None
    pr = _provider(snapshot, name)
    s, e = pr.get("started_at"), pr.get("ended_at")
    if not isinstance(s, str) or not isinstance(e, str) or not s or not e:
        return None
    start, end = _parse_iso(s), _parse_iso(e)
    if start is None or end is None or end <= start:
        return None
    # 允许线程调度/序列化产生最多 2 秒漂移，但拒绝陈旧 provider 拼接。
    if start < top[0] - 2.0 or end > top[1] + 2.0:
        return None
    overlap = min(end, top[1]) - max(start, top[0])
    if overlap <= 0 or overlap / (end - start) < 0.5:
        return None
    return (start, end)


def _snapshot_interval(snapshot: dict) -> tuple[float, float] | None:
    """Parse a top-level window anchored to `collected_at`."""
    window = snapshot.get("window")
    if not isinstance(window, dict):
        return None
    start = _parse_iso(window.get("start"))
    end = _parse_iso(window.get("end"))
    if start is None or end is None or end <= start:
        return None
    collected_at = _parse_iso(snapshot.get("collected_at"))
    if collected_at is None or abs(collected_at - start) > 2.0:
        return None
    return (start, end)


def _profile_interval(profile: dict) -> tuple[float, float] | None:
    """Parse a profiler task window; malformed or absent windows are unusable."""
    window = profile.get("profile_window")
    if not isinstance(window, dict):
        return None
    start = _parse_iso(window.get("start"))
    end = _parse_iso(window.get("end"))
    if start is None or end is None or end <= start:
        return None
    return (start, end)


def _profile_window_matches_snapshot(snapshot: dict, profile: dict) -> bool:
    """Require at least half of the profiler window to overlap the IO snapshot."""
    snapshot_interval = _snapshot_interval(snapshot)
    profile_interval = _profile_interval(profile)
    if snapshot_interval is None or profile_interval is None:
        return False
    overlap = min(snapshot_interval[1], profile_interval[1]) - max(snapshot_interval[0], profile_interval[0])
    profile_duration = profile_interval[1] - profile_interval[0]
    return overlap > 0 and overlap / profile_duration >= 0.5


def _finding_evidence_interval(finding: dict) -> tuple[float, float] | None:
    """Return a validated actual evidence interval carried by a Host finding."""
    raw = finding.get("evidence_interval")
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        start, end = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        return None
    return start, end


def _profile_host_overlap_rules(profile: dict, host_findings: list[dict]) -> list[str]:
    """Return confirmed Host rules whose actual evidence overlaps the profile."""
    profile_interval = _profile_interval(profile)
    if profile_interval is None:
        return []
    matched: list[str] = []
    for finding in host_findings:
        evidence_interval = _finding_evidence_interval(finding)
        if intervals_have_common_overlap(profile_interval, evidence_interval):
            matched.append(str(finding.get("rule_id") or ""))
    return sorted(rule for rule in matched if rule)


def _parse_iso(ts: str) -> float | None:
    """带时区 ISO8601 → epoch 秒；不可解析或 naive timestamp 返回 None。"""
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.timestamp()
    except (ValueError, TypeError):
        return None
