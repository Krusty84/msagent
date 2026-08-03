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
"""
IO Snapshot 确定性分析器（mindstudio-storage-analysis）。

输入 collect_io_snapshot.py 产出的 IO Snapshot（dict 或 JSON 文件），
输出结构化 findings 列表，每条包含：
    rule_id / severity / confidence / evidence_fields / missing_evidence
    / summary / recommended_next_checks

设计要点：
  - mte2_ratio 完全移出 NPU 传导链的"必需证据"。MTE2 是 AI Core 内部/邻近
    存储层的数据搬运（GM→UB/L1），高占比只代表算子内数据搬运压力，不能证明
    Host 存储/DataLoader 供给不足。高 mte2 + Host IO 正常时应转交计算分析。
  - NPU 传导链用 step throughput / device Free / DataLoader wait / batch ready
    与 Host IO 异常的"同窗相关性"，三档置信度。
  - R200 拆成两层：仅"识别为网络挂载"不构成瓶颈，必须有同窗
    RTT/execute/retrans/major-timeout 性能证据才能确认。
  - 阈值不写成跨设备绝对真理：优先支持设备基线/用户规格/对照实验，通用阈值仅弱提示。
  - 未知 schema major 版本拒绝确定性分析（返回明确错误）。

用法:
    python3 analyze_io_snapshot.py io_snapshot.json
    python3 analyze_io_snapshot.py io_snapshot.json --mode all
    python3 analyze_io_snapshot.py io_snapshot.json -o findings.json
"""

from __future__ import annotations

import argparse
import copy
from collections import defaultdict
import json
import math
import os
import re
import sys
from typing import Any

# 与 collector 共享 schema 版本策略
try:
    import collect_io_snapshot as _c

    SUPPORTED_MAJOR = _c.SUPPORTED_MAJOR
except Exception:  # noqa: BLE001 - 独立运行时不强依赖 collector
    SUPPORTED_MAJOR = 1

RULE_IDS = ["R000", "R100", "R200", "R300", "R400", "R500"]

# severity / confidence 排序（用于"正向问题结论"判定）
_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


# --- 通用 finding 构造 ---------------------------------------------------


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


# --- Snapshot 取值辅助 ---------------------------------------------------


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


# --- diskstats 差值计算（结构化 r/s、await、%util 等）-------------------


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


def _disks_from_iostat(snapshot: dict) -> tuple[list[dict], str]:
    """从 iostat provider 取归一化的 per-disk 指标列表。

    collector 真实输出：parsed["disks"] 是 {name: metrics} 字典（见
    io_snapshot_schema.md §6 与 collection_guide.md）。这里归一成
    [{"name": name, **metrics}, ...] 列表，与 _compute_disk_rates 同构。

    兼容历史/合成 fixture 传入 list 形态（name 已在元素内）。
    返回 (disks_list, source_label)；无 iostat 证据时 ([], "")。
    """
    iostat_pr = _provider(snapshot, "iostat")
    if _status(iostat_pr) != "ok" or not isinstance(_parsed(iostat_pr), dict):
        return [], ""
    raw = (_parsed(iostat_pr) or {}).get("disks")
    if isinstance(raw, dict):
        disks = []
        for name, m in raw.items():
            # 先展开 metrics 再覆盖 name，避免 metrics 内的
            # 非字符串/不可哈希 name 覆盖 canonical key，导致 device_baselines.get 崩溃。
            entry = dict(m) if isinstance(m, dict) else {}
            entry["name"] = name
            disks.append(entry)
        return disks, "iostat"
    if isinstance(raw, list):
        # 容错：list 形态需每个元素自带 name
        disks = [d for d in raw if isinstance(d, dict) and d.get("name")]
        return disks, "iostat"
    return [], ""


def _collect_disks(snapshot: dict) -> tuple[list[dict], str]:
    """统一的 per-disk 指标入口：优先 iostat，回退 diskstats 两次采样差值。

    始终返回 (list[dict], source)，每项含 name/r_per_s/rkB_per_s/r_await_ms/
    util_percent/avgqu_sz 等统一字段名。这是 R100/R300 共用的唯一取盘入口，
    保证 collector→analyzer 端到端无适配层、无 dict/list 误用。
    """
    disks, source = _disks_from_iostat(snapshot)
    if disks:
        return disks, source
    samples = snapshot.get("diskstats_sample", []) or []
    rates = _compute_disk_rates(samples)
    return (rates, "diskstats_delta") if rates else ([], "")


def _compute_disk_rates(samples: list[dict]) -> list[dict]:
    """根据 diskstats 两次采样差值，计算每设备速率指标。

    返回 [{name, r_per_s, w_per_s, rkB_per_s, r_await_ms, util_percent, avgqu_sz}, ...]
    """
    if len(samples) < 2:
        return []
    s0 = samples[0].get("disks", {})
    s1 = samples[1].get("disks", {})
    t0 = samples[0].get("timestamp", 0)
    t1 = samples[1].get("timestamp", 0)
    try:
        dt = float(t1) - float(t0)
    except (TypeError, ValueError, OverflowError):
        return []
    # 非递增时间戳不能虚构为 1 秒，否则累计 counter 会被放大成假饱和。
    if not math.isfinite(dt) or dt <= 0:
        return []
    # disks 在 DiskStatSample 里是 {name: {...}}（pydantic dump）。
    s0m = s0 if isinstance(s0, dict) else {}
    s1m = s1 if isinstance(s1, dict) else {}
    results: list[dict] = []
    for name, d1 in s1m.items():
        d0 = s0m.get(name)
        if not isinstance(d0, dict) or not isinstance(d1, dict):
            # 新出现设备没有 t0 基线，当前累计值不能当窗口 delta。
            continue
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
        try:
            if any(
                float(d1.get(key, 0)) < float(d0.get(key, 0)) for key in counter_fields
            ):
                # 设备重置/热拔插/counter wrap：整条设备窗口无效。
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        reads = _delta(d1, d0, "reads_completed")
        writes = _delta(d1, d0, "writes_completed")
        sect_r = _delta(d1, d0, "sectors_read")
        sect_w = _delta(d1, d0, "sectors_written")
        time_read_ms = _delta(d1, d0, "time_reading_ms")
        time_write_ms = _delta(d1, d0, "time_writing_ms")
        busy_ms = _delta(d1, d0, "time_io_ms")
        weighted_ms = _delta(d1, d0, "weighted_time_io_ms")
        r_per_s = reads / dt
        w_per_s = writes / dt
        # sector = 512B = 0.5KB
        rkB_per_s = sect_r * 0.5 / dt
        wkB_per_s = sect_w * 0.5 / dt
        r_await = (time_read_ms / reads) if reads > 0 else 0.0
        w_await = (time_write_ms / writes) if writes > 0 else 0.0
        util_pct = (busy_ms / (dt * 1000.0) * 100.0) if dt > 0 else 0.0
        util_pct = min(util_pct, 100.0)
        avgqu_sz = (weighted_ms / (dt * 1000.0)) if dt > 0 else 0.0
        results.append(
            {
                "name": name,
                "r_per_s": r_per_s,
                "w_per_s": w_per_s,
                "rkB_per_s": rkB_per_s,
                "wkB_per_s": wkB_per_s,
                "r_await_ms": r_await,
                "w_await_ms": w_await,
                "util_percent": util_pct,
                "avgqu_sz": avgqu_sz,
            }
        )
    return results


# --- 各根因桶分析 --------------------------------------------------------

# 设备类型 → r_await 参考线（毫秒）；未知类型使用保守值避免误报。
_AWAIT_BY_TYPE = {"hdd": 20.0, "ssd": 5.0, "unknown": 15.0}
_UTIL_HIGH = 90.0  # 单次采样"忙"的参考线
_UTIL_SUSTAINED_MEAN = 85.0  # 窗口平均"持续忙"的参考线
_AVGQU_HIGH = 2.0  # 队列积压参考线
_MIN_SAMPLES_HIGH = 3  # 声称"持续饱和"所需最少采样数（iostat 路径）
# 吞吐/IOPS "明显有量"的经验值（无设备规格时，仅用于区分 io_pressure vs bandwidth/iops）
_BANDWIDTH_KBPS_HEURISTIC = 100 * 1024  # ≈100 MB/s
_IOPS_HEURISTIC = 10000.0
_IOPS_HIGH = 5000.0  # 小 IO IOPS 参考（R300 small-IO 候选用）
_GLUSTER_SMALL_READ_SYSCALLS_PER_SECOND = 500.0


def _await_threshold(device_type: str | None) -> float:
    return _AWAIT_BY_TYPE.get(
        str(device_type or "unknown").lower(), _AWAIT_BY_TYPE["unknown"]
    )


def _classify_r100_disk(d: dict) -> dict:
    """评估单个设备的饱和度，返回结构化判定。

    high（sustained）需"持续窗口 + 真实队列积压 或 设备基线接近上限"——
    避免 util 高但吞吐极低（NVMe util 失真/采样伪影）或仅偶发抖动被误判 high。
    无设备基线时，util 高 + await 高但队列低只能到 medium（likely）。
    """
    util = _f(d.get("util_percent"))
    util_max = _f(d.get("util_max"), util)
    util_p95 = _f(d.get("util_p95"), util)
    r_await = _f(d.get("r_await_ms"))
    w_await = _f(d.get("w_await_ms"))
    generic_await = _f(d.get("await"))
    avgqu = _f(d.get("avgqu_sz"))
    r_per_s = _f(d.get("r_per_s"))
    w_per_s = _f(d.get("w_per_s"))
    rkB = _f(d.get("rkB_per_s"))
    wkB = _f(d.get("wkB_per_s"))
    device_type = d.get("device_type") or "unknown"
    from_diskstats = bool(d.get("_from_diskstats"))

    def _count(value: Any, default: int = 0) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return max(0, parsed)

    sample_count = _count(d.get("sample_count"), 2 if from_diskstats else 1)
    await_thr = _await_threshold(device_type)
    # baseline 必须是 dict；其他真值类型没有可用的 .get 语义。
    raw_baseline = d.get("baseline")
    baseline = raw_baseline if isinstance(raw_baseline, dict) else {}

    has_util = any(
        key in d and d.get(key) is not None
        for key in ("util_percent", "util_max", "util_p95")
    )
    has_queue = "avgqu_sz" in d and d.get("avgqu_sz") is not None
    has_await = any(
        key in d and d.get(key) is not None
        for key in ("await", "r_await_ms", "w_await_ms")
    )
    has_rate = any(
        key in d and d.get(key) is not None
        for key in ("r_per_s", "w_per_s", "rkB_per_s", "wkB_per_s")
    )

    def _field_count(field: str) -> int:
        if field not in d or d.get(field) is None:
            return 0
        return _count(d.get(f"{field}_sample_count"), sample_count)

    util_sample_count = _count(
        d.get("util_sample_count"), sample_count if has_util else 0
    )

    def _paired_with_util_count(field: str) -> int:
        if field not in d or d.get(field) is None or not has_util:
            return 0
        paired_key = f"{field}_with_util_sample_count"
        # Missing co-occurrence evidence is unknown, not an invitation to infer it
        # from two independent counts that may describe disjoint reports.
        return _count(d.get(paired_key), 0) if paired_key in d else 0

    queue_sample_count = _field_count("avgqu_sz")
    generic_await_sample_count = _field_count("await")
    read_await_sample_count = _field_count("r_await_ms")
    write_await_sample_count = _field_count("w_await_ms")
    await_sample_count = max(
        generic_await_sample_count,
        read_await_sample_count,
        write_await_sample_count,
    )
    queue_with_util_sample_count = _paired_with_util_count("avgqu_sz")
    generic_await_with_util_sample_count = _paired_with_util_count("await")
    read_await_with_util_sample_count = _paired_with_util_count("r_await_ms")
    write_await_with_util_sample_count = _paired_with_util_count("w_await_ms")
    await_with_util_sample_count = max(
        generic_await_with_util_sample_count,
        read_await_with_util_sample_count,
        write_await_with_util_sample_count,
    )

    busy = util >= _UTIL_HIGH or util_max >= _UTIL_HIGH
    sustained = util >= _UTIL_SUSTAINED_MEAN and util_sample_count >= _MIN_SAMPLES_HIGH
    read_await_bad = r_per_s > 0 and r_await >= await_thr
    write_await_bad = w_per_s > 0 and w_await >= await_thr
    generic_await_bad = (r_per_s + w_per_s) > 0 and generic_await >= await_thr
    await_bad = read_await_bad or write_await_bad or generic_await_bad
    queue_bad = avgqu >= _AVGQU_HIGH
    # 压力信号：await 超设备类型阈值 或 队列积压。NVMe 高 util 但 await/队列正常 = util 失真，不算压力。
    pressure = await_bad or queue_bad
    triggered_sample_counts: list[int] = []
    if queue_bad:
        triggered_sample_counts.append(queue_with_util_sample_count)
    if read_await_bad:
        triggered_sample_counts.append(read_await_with_util_sample_count)
    if write_await_bad:
        triggered_sample_counts.append(write_await_with_util_sample_count)
    if generic_await_bad:
        triggered_sample_counts.append(generic_await_with_util_sample_count)
    pressure_sample_count = max(triggered_sample_counts, default=0)
    pressure_samples_dense = pressure_sample_count >= _MIN_SAMPLES_HIGH
    queue_support_dense = (
        queue_bad and queue_with_util_sample_count >= _MIN_SAMPLES_HIGH
    )
    # "有量吞吐"：排除 r/s=1/rkB=4 这类几乎无 IO 的 util 失真场景
    total_kbps = rkB + wkB
    total_iops = r_per_s + w_per_s
    throughput_meaningful = total_kbps >= 1024 or total_iops >= 100
    # 设备基线接近上限（用户提供规格时才成立）
    base_max_mbps = _f(baseline.get("max_read_mbps"))
    base_max_write_mbps = _f(baseline.get("max_write_mbps"))
    base_max_iops = _f(baseline.get("max_iops"))
    near_bandwidth_ceiling = (
        base_max_mbps > 0 and (rkB / 1024) >= 0.85 * base_max_mbps
    ) or (base_max_write_mbps > 0 and (wkB / 1024) >= 0.85 * base_max_write_mbps)
    near_iops_ceiling = base_max_iops > 0 and total_iops >= 0.85 * base_max_iops
    baseline_backed = near_bandwidth_ceiling or near_iops_ceiling

    # high：持续 + 压力 + (持续真实队列 OR 基线接近上限)。
    # 队列背书必须使用队列自身的共现样本，不能借用 await 的样本密度。
    confirmed = (
        sustained
        and pressure
        and (queue_support_dense or (baseline_backed and pressure_samples_dense))
    )
    # medium(likely)：持续 + 压力(await) + 有量吞吐，但无队列/基线背书
    likely = (
        sustained
        and pressure
        and pressure_samples_dense
        and throughput_meaningful
        and not confirmed
    )
    # medium(transient)：忙 + 有压力 + 有量吞吐，但非持续（偶发抖动 / 采样不足）。
    # 必须有量吞吐——util 高但吞吐极低（r/s=1/rkB=4）是 util 失真，不算饱和。
    transient = (
        busy and pressure and throughput_meaningful and not (confirmed or likely)
    )
    # 其余（含 busy 但无压力 = util 失真，或低 util）→ none

    if confirmed:
        level = "sustained"
    elif likely:
        level = "likely"
    elif transient:
        level = "transient"
    else:
        level = "none"

    avg_io_kb = (total_kbps / total_iops) if total_iops > 0 else 0
    pressure = confirmed or likely
    if pressure:
        if near_iops_ceiling or (total_iops >= _IOPS_HEURISTIC and avg_io_kb < 16):
            subtype = "iops"
        elif near_bandwidth_ceiling or total_kbps >= _BANDWIDTH_KBPS_HEURISTIC:
            subtype = "bandwidth"
        else:
            subtype = "io_pressure"
    else:
        subtype = "io_pressure"
    return {
        "device": d.get("name"),
        "device_type": device_type,
        "util_percent": round(util, 1),
        "util_max": round(util_max, 1),
        "util_p95": round(util_p95, 1),
        "r_await_ms": round(r_await, 2),
        "w_await_ms": round(w_await, 2),
        "r_per_s": round(r_per_s, 1),
        "w_per_s": round(w_per_s, 1),
        "rkB_per_s": round(rkB, 1),
        "wkB_per_s": round(wkB, 1),
        "avgqu_sz": round(avgqu, 2),
        "sample_count": sample_count,
        "metric_sample_counts": {
            "util": util_sample_count,
            "queue": queue_sample_count,
            "await": await_sample_count,
            "queue_with_util": queue_with_util_sample_count,
            "await_with_util": await_with_util_sample_count,
            "pressure": pressure_sample_count,
        },
        "await_threshold_ms": await_thr,
        "subtype": subtype,
        "level": level,
        "pressure_confirmed": pressure,
        "baseline_backed": baseline_backed,
        "metric_coverage": {
            "util": has_util,
            "queue": has_queue,
            "await": has_await,
            "rate": has_rate,
        },
        "health_evidence_complete": has_util and (has_queue or has_await),
        "health_evidence_dense": max(
            queue_with_util_sample_count, await_with_util_sample_count
        )
        >= _MIN_SAMPLES_HIGH,
    }


def analyze_r100(snapshot: dict) -> dict:
    """R100 吞吐 / IOPS 饱和（设备忙）：窗口聚合 + 设备类型 + 分级置信。

    - high：持续饱和（util_mean≥85 且采样≥3）+ await/队列超设备类型阈值。
    - medium：偶发饱和（util 偶高但非持续），或采样不足，或 util 高但 await/队列正常。
    - info（high confidence 无饱和）：足量窗口样本内无任何饱和迹象。
    subtype：仅在有量吞吐且确认压力时标 bandwidth/iops，否则 io_pressure（不臆造带宽饱和）。
    数据来源优先 iostat.parsed，缺失时退化到 diskstats 差值（视为窗口聚合，sample_count=2）。
    """
    finding = _finding(
        "R100",
        "info",
        "none",
        "",
        next_checks=[
            "对照设备规格带宽/IOPS，确认是否接近上限",
            "做本地缓存/预取对照实验，观察 util 是否下降",
        ],
    )
    disks, source = _collect_disks(snapshot)
    if source == "diskstats_delta":
        for d in disks:
            d["_from_diskstats"] = True

    if not disks:
        finding["missing_evidence"] = ["iostat.parsed", "diskstats_sample（两次采样）"]
        finding["confidence"] = "none"
        finding["summary"] = "无设备级 IO 速率证据，无法判定 R100。"
        return finding

    finding["evidence_fields"] = [f"{source}.disks"]
    evidence_interval: tuple[float, float] | None = None
    if source == "iostat":
        evidence_interval = _provider_interval(snapshot, "iostat")
    elif source == "diskstats_delta":
        samples = snapshot.get("diskstats_sample") or []
        if isinstance(samples, list) and len(samples) >= 2:
            try:
                start = float(samples[0].get("timestamp"))
                end = float(samples[-1].get("timestamp"))
                top = _snapshot_interval(snapshot)
                if (
                    top is not None
                    and math.isfinite(start)
                    and math.isfinite(end)
                    and end > start
                    and start >= top[0] - 2.0
                    and end <= top[1] + 2.0
                ):
                    evidence_interval = (start, end)
            except (AttributeError, TypeError, ValueError, OverflowError):
                evidence_interval = None
    finding["source_provider"] = source
    finding["evidence_window_valid"] = evidence_interval is not None
    if evidence_interval is not None:
        finding["evidence_interval"] = list(evidence_interval)
    evidence_duration = (
        evidence_interval[1] - evidence_interval[0]
        if evidence_interval is not None
        else None
    )
    duration = evidence_duration or _snapshot_duration(snapshot)
    short_window = duration is not None and duration < 10
    # 可选设备基线（用户提供规格：{name: {max_read_mbps, max_iops}}）—— 有基线才能确认带宽/IOPS 接近上限
    baselines = snapshot.get("device_baselines") or {}
    if not isinstance(baselines, dict):
        baselines = {}
    for d in disks:
        bl = baselines.get(d.get("name")) or baselines.get(f"/dev/{d.get('name')}")
        if isinstance(bl, dict):
            d["baseline"] = bl
    assessed = [_classify_r100_disk(d) for d in disks]
    finding["assessed_devices"] = assessed
    sustained = [a for a in assessed if a["level"] == "sustained"]
    likely = [a for a in assessed if a["level"] == "likely"]
    transient = [a for a in assessed if a["level"] == "transient"]
    incomplete = [a for a in assessed if not a["health_evidence_complete"]]
    sparse = [
        a
        for a in assessed
        if a["health_evidence_complete"] and not a["health_evidence_dense"]
    ]

    if sustained:
        finding["confidence"] = "high"
        finding["severity"] = "high"
        finding["saturated_devices"] = sustained
        subtypes = sorted({a["subtype"] for a in sustained})
        backing = (
            "队列积压"
            if any(a.get("avgqu_sz", 0) >= _AVGQU_HIGH for a in sustained)
            else "设备基线接近上限"
        )
        finding["summary"] = (
            f"检测到 {len(sustained)} 个设备持续 IO 饱和（{', '.join(subtypes)} 型，"
            f"窗口持续高 util + {backing}）："
            + "; ".join(
                f"{a['device']} util_mean={a['util_percent']}%" for a in sustained[:3]
            )
        )
    elif likely:
        finding["confidence"] = "medium"
        finding["severity"] = "medium"
        finding["saturated_devices"] = likely
        finding["summary"] = (
            f"检测到 {len(likely)} 个设备持续高 util + await 超 {likely[0]['device_type']} 阈值，"
            f"但无队列积压或设备基线背书，仅判为 likely（需规格/对照确认）："
            + "; ".join(f"{a['device']} util={a['util_percent']}%" for a in likely[:3])
        )
        finding["missing_evidence"] = [
            "device_baselines（设备规格带宽/IOPS，用于确认接近上限）",
            "或 avgqu_sz 持续 ≥2（真实队列积压）",
        ]
    elif transient:
        finding["confidence"] = "medium"
        finding["severity"] = "medium"
        finding["saturated_devices"] = transient
        finding["summary"] = (
            f"检测到 {len(transient)} 个设备出现 IO 压力但非持续（偶发高 util、采样不足、"
            f"或 util 高但吞吐/队列正常）："
            + "; ".join(
                f"{a['device']} util_max={a['util_max']}%" for a in transient[:3]
            )
        )
        finding["note"] = "需更长采样窗口或设备规格确认是否为持续瓶颈。"
    else:
        finding["severity"] = "info"
        if incomplete:
            finding["confidence"] = "low"
            finding["summary"] = (
                f"{len(incomplete)} 个设备的指标字段覆盖不足，不能高置信排除 IO 压力；"
                "至少需要 util 与 queue/await 组合证据。"
            )
            finding.setdefault("missing_evidence", []).append(
                "每设备完整的 util + queue/await 指标（以及对应读写速率）"
            )
        elif sparse:
            finding["confidence"] = "medium"
            finding["summary"] = (
                f"{len(sparse)} 个设备虽有 util 与 queue/await 字段，但共同有效样本不足 "
                f"{_MIN_SAMPLES_HIGH} 个，不能高置信排除 IO 压力。"
            )
            finding.setdefault("missing_evidence", []).append(
                "每设备至少 3 个同时覆盖 util 与 queue/await 的有效样本"
            )
        elif short_window:
            finding["confidence"] = "medium"
            finding["summary"] = (
                f"短窗口（{duration:.1f}s）内未检测到设备 IO 饱和；采样窗偏短，"
                "只能说明本窗口未捕获压力，不能排除偶发存储瓶颈。"
            )
            finding.setdefault("missing_evidence", []).extend(
                [
                    "更长的同窗采样（建议 30s，避免短窗偶发值误导）",
                    "设备规格/对照基线（用于确认是否接近上限）",
                ]
            )
        else:
            finding["confidence"] = "high"
            finding["summary"] = (
                "未检测到设备 IO 饱和（窗口内 util/await/队列均在参考线内）。"
            )
        finding["note"] = "阈值按设备类型区分；NVMe 的 util 在多队列下含义弱化。"
    if short_window:
        if finding.get("confidence") == "high":
            finding["confidence"] = "medium"
            if finding.get("severity") == "high":
                finding["severity"] = "medium"
            finding["summary"] += (
                f" 但实际证据窗口仅 {duration:.1f}s，短于 10s，置信度封顶 medium。"
            )
        else:
            finding["summary"] += (
                f" 实际证据窗口仅 {duration:.1f}s，短于 10s；"
                "短窗口不能认证持续压力或健康。"
            )
        missing = "更长的同窗采样（建议 30s，短窗口不得认证持续压力或健康）"
        if missing not in finding.setdefault("missing_evidence", []):
            finding["missing_evidence"].append(missing)
    if evidence_interval is None:
        finding.setdefault("missing_evidence", []).append(
            f"{source} 有效采集时间窗（用于与 PID/NPU 证据做因果对齐）"
        )
        if finding.get("confidence") == "high":
            finding["confidence"] = "medium"
            if finding.get("severity") == "high":
                finding["severity"] = "medium"
            finding["summary"] += (
                " 但数据源缺少有效 started_at/ended_at 时间窗，无法确认数据新鲜度，"
                "置信度封顶 medium。"
            )
    return finding


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
    return isinstance(item, dict) and str(item.get("fstype") or "").lower() == "fuse.glusterfs"


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
        matches = [
            mount
            for mount in gluster_mounts
            if _path_under_mount(target_path, str(mount.get("mount_point") or ""))
        ]
        if not matches:
            return [], "target_path_non_glusterfs"
        longest = max(
            len(_canonicalize_path(str(mount.get("mount_point") or "")))
            for mount in matches
        )
        return (
            [
                mount
                for mount in matches
                if len(_canonicalize_path(str(mount.get("mount_point") or ""))) == longest
            ],
            "target_path_glusterfs",
        )

    target_pid = target.get("pid") if isinstance(target, dict) else None
    if isinstance(target_pid, int) and not isinstance(target_pid, bool) and target_pid > 0:
        process_map = _provider(snapshot, "process_io_map")
        parsed = _parsed(process_map) if _status(process_map) == "ok" else None
        if not isinstance(parsed, dict):
            return [], "target_process_io_map_unresolved"
        allowed_pids = _target_pid_scope(snapshot)
        if not allowed_pids:
            return [], "target_process_io_map_unresolved"
        current = {_gluster_identity(mount): mount for mount in gluster_mounts}
        selected: dict[tuple[str, str, str], dict] = {}
        for mapping in parsed.get("mappings", []) or []:
            if not isinstance(mapping, dict) or mapping.get("pid") not in allowed_pids:
                continue
            if not _is_glusterfs_mount(mapping):
                continue
            identity = _gluster_identity(mapping, source_key="source")
            if identity in current:
                selected[identity] = current[identity]
        if selected:
            return list(selected.values()), "target_process_io_map_glusterfs"
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
    try:
        rchar = max(0.0, float(process_io.get("rchar") or 0))
        read_bytes = max(0.0, float(process_io.get("read_bytes") or 0))
        syscr = max(0.0, float(process_io.get("syscr") or 0))
        stable_pids = max(0, int(process_io.get("stable_pid_count") or 0))
    except (TypeError, ValueError, OverflowError):
        return None
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
    if scope == "target_path_non_glusterfs":
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
                "显式 target.pid 未解析到当前设备或 NFS 挂载，"
                "不能把全机 NFS 指标归因给目标 workload。"
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
        mount
        for mount in (snapshot.get("mounts") or [])
        if _is_glusterfs_mount(mount)
    ]
    required, scope = _required_gluster_mounts(snapshot, current_mounts)
    if not required or scope.endswith("_unresolved") or scope.endswith("_non_glusterfs"):
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


# 共享库/日志/解释器/系统文件路径——这些路径上的多 PID 共享设备**不构成**数据 IO 争抢
# 避免把“共同打开根盘文件”误报为 IO 争抢。
_NON_DATA_PATH_PREFIXES = (
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
_NON_DATA_PATH_SUFFIXES = (".so", ".so.", ".pyc", ".pyo", ".pyd")
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


def _canonicalize_path(p: str) -> str:
    """规范化绝对路径：折叠重复 `/`、解析 `.`/`..`、去 trailing slash。

    相对路径或含越界 `..` 的路径原样返回（不臆测），由调用方按需处理。
    """
    if not isinstance(p, str) or not p:
        return ""
    # 折叠重复斜杠
    norm = re.sub(r"/+", "/", p)
    # 解析 . / ..（仅对绝对路径，词法解析，不触碰符号链接）
    if not norm.startswith("/"):
        return norm.rstrip("/") or "."
    parts = norm.split("/")
    stack: list[str] = []
    for part in parts:
        if part == "" or part == ".":
            continue
        if part == "..":
            if stack:
                stack.pop()
            # 越界 .. 保留（不臆测到 / 之上）
            else:
                stack.append("..")
            continue
        stack.append(part)
    return "/" + "/".join(stack)


def _is_data_relevant_path(path: str | None, target_path: str | None = None) -> bool:
    """判断一条映射路径是否"数据相关"（排除共享库/日志/解释器/系统文件）。

    使用路径组件边界匹配（`path == prefix` 或 `path` 位于 `prefix/` 下），
    避免 `/usrdata` 被 `/usr` 前缀误伤。
    target 先规范化（折叠 `//`、解析 `.`/`..`）。规则优先级：
      1. 系统后缀（.so/.pyc...）与 site-packages 永远排除（即便在 target 下）。
      2. **具体** target（非 `/`/空/`.`）→ 其子树覆盖系统目录前缀排除
         （用户明确指定 /opt/dataset 为数据根，即便 /opt 是系统前缀）。
      3. 过宽 target（`/`、空、`.`）或无 target → 系统目录前缀排除生效
         （避免 target='/' 把 /usr/lib/*.so 重新认作数据）。
    """
    if not path or not isinstance(path, str):
        return False
    p = _canonicalize_path(path)
    low = p.lower()
    # 1. 后缀/site-packages 永远排除
    if any(low.endswith(suf) for suf in _NON_DATA_PATH_SUFFIXES):
        return False
    if "/site-packages/" in low or "/dist-packages/" in low:
        return False
    # 2. 具体 target → 子树覆盖系统前缀
    effective_target = ""
    if target_path and isinstance(target_path, str):
        tp = _canonicalize_path(target_path)
        if tp and tp not in ("/", "."):
            effective_target = tp
    if effective_target:
        return p == effective_target or p.startswith(effective_target + "/")
    # 3. 无/过宽 target → 系统目录前缀排除
    for prefix in _NON_DATA_PATH_PREFIXES:
        if p == prefix or p.startswith(prefix + "/"):
            return False
    return True


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
    if (
        not isinstance(total_reports, int)
        or isinstance(total_reports, bool)
        or total_reports < _MIN_SAMPLES_HIGH
    ):
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


def _common_mapping_interval(
    entries: list[dict], pids: set[int]
) -> tuple[float, float] | None:
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
        pmap_observation_samples = _strict_json_int(
            pmap_parsed.get("observation_samples"), positive=True
        )
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
    from collections import defaultdict

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
            [
                _canonical_dev(device)
                for device in backing_raw
                if isinstance(device, str)
            ]
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
        finding["summary"] = (
            "已建立 PID→设备映射，但未观察到至少两个 PID 同时访问同一设备的证据。"
        )
        finding["missing_evidence"] = ["至少两个目标 workload PID 的同设备映射"]
        if len(active_pids) < 2:
            finding["missing_evidence"].append(
                "至少两个目标 workload PID 的同窗 pidstat 活跃 IO 样本"
            )
        if skipped_virtual_mounts:
            finding["note"] = (
                f"已忽略 {skipped_virtual_mounts} 条 tmpfs/overlay/proc/sysfs 等"
                "非持久伪文件系统映射。"
            )
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
        strong_pids = {
            e["pid"]
            for e in entries
            if e["pid"] is not None and e["path_relevant"] and e["active_io"]
        }
        identity_pids = {
            e["pid"]
            for e in entries
            if e["pid"] in strong_pids and e["identity_strong"]
        }
        process_identity_pids = {
            e["pid"]
            for e in entries
            if e["pid"] in strong_pids and e["process_identity_strong"]
        }
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
        temporally_bound_pids = {
            e["pid"] for e in temporally_bound_entries if e["pid"] is not None
        }
        mapping_overlap = _common_mapping_interval(
            temporally_bound_entries, temporally_bound_pids
        )
        temporal_mapping_ok = bool(
            pmap_observation_samples >= 2
            and len(temporally_bound_pids) >= 2
            and mapping_overlap is not None
            and mapping_overlap[1] - mapping_overlap[0] >= 1.0
        )
        if len(strong_pids) >= 2 and saturated:
            causal_candidates[dev] = sorted(strong_pids)
            unambiguous_pids = {
                pid
                for pid in temporally_bound_pids
                if len(pid_devices.get(pid, set())) == 1
            }
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
                    missing_reasons.append(
                        f"{dev}: PID 身份缺少 boot_id + starttime 绑定，不能排除 PID 复用"
                    )
                if unambiguous_pids != temporally_bound_pids:
                    missing_reasons.append(
                        f"{dev}: pidstat 是每 PID 聚合 IO，PID 同时映射多个数据设备，无法归因到该设备"
                    )
                if not temporal_mapping_ok:
                    missing_reasons.append(
                        f"{dev}: 每 PID 映射需实际观测至少两次，且观测区间须有至少 1s 公共交集"
                    )
        else:
            weak[dev] = pids
            if len(strong_pids) < 2:
                # 区分缺哪项，便于报告
                data_pids = {
                    e["pid"]
                    for e in entries
                    if e["pid"] is not None and e["path_relevant"]
                }
                active_pid_set = {
                    e["pid"] for e in entries if e["pid"] is not None and e["active_io"]
                }
                if len(data_pids) < 2:
                    missing_reasons.append(
                        f"{dev}: 数据相关路径的 PID 不足（{sorted(data_pids)}）"
                    )
                if len(active_pid_set) < 2:
                    missing_reasons.append(
                        f"{dev}: 有活跃 IO 的 PID 不足（{sorted(active_pid_set)}）"
                    )
                if data_pids and active_pid_set and not (data_pids & active_pid_set):
                    missing_reasons.append(
                        f"{dev}: 数据路径与活跃 IO 分属不同 PID，不能拼接为同进程争抢"
                    )
            if not saturated:
                missing_reasons.append(
                    f"{dev}: 设备未饱和（R100 未命中，争抢无实际影响）"
                )

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
                f"检测到 {len(confirmed)} 个多进程数据 IO 争抢候选，但 "
                "process_io_map 覆盖不完整，不能确认高置信争抢。"
            )
            finding["missing_evidence"] = [
                "完整 PID/FD 覆盖的 process_io_map（无 partial 截断或权限缺口）"
            ]
            return finding
        # high 结论要求 iostat/pidstat/process_io_map 三者都有合法时间窗，且
        # 存在正长度公共交集。缺失/非法时间不是“无法证伪即同窗”，而是证据不足。
        r100_win_raw = r100_finding.get("evidence_interval") if r100_finding else None
        r100_win = (
            tuple(r100_win_raw)
            if isinstance(r100_win_raw, (list, tuple)) and len(r100_win_raw) == 2
            else None
        )
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
        window_confirmed = {
            dev: pids
            for dev, pids in confirmed.items()
            if dev in device_evidence_intervals
        }
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
                f"（每 PID 均含同窗映射+数据路径+活跃 IO 且设备饱和）："
                + _format_device_pid_map(window_confirmed)
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
            finding["note"] = (
                "部分设备用启发式归一（无 /sys），canonical 身份可能不准。"
            )
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
        finding["note"] = (
            f"已忽略 {skipped_virtual_mounts} 条 tmpfs/overlay/proc/sysfs 等"
            "非持久伪文件系统映射。"
        )
    return finding


def analyze_r000(snapshot: dict) -> dict:
    """R000 信息不足：汇总哪些 provider 缺失/失败/部分可用。"""
    availability = snapshot.get("availability", {}) or {}
    missing = availability.get("missing", []) or []
    errors = availability.get("errors", []) or []
    partial = availability.get("partial", []) or []
    finding = _finding(
        "R000",
        "info",
        "high",
        "",
        evidence_fields=[
            "availability.missing",
            "availability.partial",
            "availability.errors",
        ],
    )
    parts = []
    if missing:
        parts.append(f"缺失数据源 {len(missing)} 个：{', '.join(missing)}")
    if partial:
        # empty/unsupported 也必须报告，不得描述为“关键数据源均可用”。
        parts.append(f"部分/空数据源 {len(partial)} 个：{', '.join(partial)}")
    if errors:
        parts.append(f"采集错误 {len(errors)} 个：{'; '.join(errors)}")
    if parts:
        finding["summary"] = "；".join(parts) + "。相关根因桶置信度降低。"
    else:
        finding["summary"] = "关键数据源均可用。"
        finding["severity"] = "info"
    return finding


# --- 主分析入口 ----------------------------------------------------------


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


# iostat 聚合器按字段记录实际参与聚合的报告数；这些计数不能大于总报告数。
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

# 各 provider parsed 中"必须为数值"的字段（缺失可，但出现值就必须可解析为有限 float）
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
                                    f"{list_key}[{i}] requires active_sample_count "
                                    "<= sample_count <= parsed.reports"
                                )
                            el["pid"] = pid
            # Network-provider mount_metrics must be a list. Deep values are
            # interpreted conservatively by each provider-specific rule.
            if name in {"nfs", "glusterfs"}:
                mm = parsed.get("mount_metrics")
                if mm is not None and not isinstance(mm, list):
                    raise ValueError("mount_metrics not list")
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
            "window.start/end must be increasing ISO8601 timestamps with timezone "
            "and anchored to collected_at"
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
            "overlap_provenance.extraction_method must equal "
            "'timeline_interval_overlap'"
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
            "controlled_experiment improved result requires treatment device_free_percent "
            "below baseline"
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
                f"profile_window.scope={scope!r} 不是认证 scope "
                f"{_CERTIFIED_PROFILE_SCOPE!r}；动态指标仅可作非认证候选"
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
                "io_npu_overlap_observed 缺少与 snapshot.window 有效重叠的 "
                "profile_window，已忽略"
            )
    pverr = sorted(set(pverr))
    return snapshot, profile, verr, pverr, None


def interval_overlap_ratio(
    a: tuple[Any, Any] | None, b: tuple[Any, Any] | None
) -> float:
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
            if any(
                value != value or value in (float("inf"), float("-inf"))
                for value in (start, end)
            ):
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
    gap = (
        second_start - first_end
        if first_end < second_start
        else first_start - second_end
    )
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
    overlap = min(snapshot_interval[1], profile_interval[1]) - max(
        snapshot_interval[0], profile_interval[0]
    )
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


def _temp_name(path: str) -> str:
    """生成同目录下含 PID 和随机串的唯一临时文件名。"""
    import uuid

    d, base = os.path.split(path)
    return os.path.join(d or ".", f".{base}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def analyze_all(snapshot: dict, profile: dict | None = None) -> dict:
    """运行全部规则，返回 {schema_version, findings: [...], summary}。

    经 validate_analysis_request 统一入口；schema/顶层 fatal 转为结构化 error。
    validation_errors / profile_validation_errors 提升到顶层，让 agent/用户可见。
    """
    snapshot, profile, verr, pverr, fatal = validate_analysis_request(snapshot, profile)
    if fatal:
        return {"error": fatal, "schema_version": "unknown", "findings": []}
    return _analyze_validated(snapshot, profile, verr, pverr)


def _analyze_validated(
    snapshot: dict,
    profile: dict,
    verr: list[str] | None = None,
    pverr: list[str] | None = None,
) -> dict:
    """分析已经完成统一规范化的请求，避免 CLI/all 二次清洗丢失诊断。"""
    verr = list(verr or [])
    pverr = list(pverr or [])
    sv = snapshot.get("schema_version", "1.0")

    # 先算 R100，再算 R200/R300/R400（R400 依赖 R100 的饱和设备集）。
    r100 = analyze_r100(snapshot)
    r200 = analyze_r200(snapshot)
    r300 = analyze_r300(snapshot)
    r400 = analyze_r400(snapshot, r100)

    findings = [analyze_r000(snapshot), r100, r200, r300, r400]

    # NPU 传导链的 Host IO 入口是 R100~R400 的并集。
    findings.append(
        analyze_r500_with_host(snapshot, profile or {}, [r100, r200, r300, r400])
    )

    high = [
        f
        for f in findings
        if f.get("severity") == "high" and f.get("confidence") in ("high", "medium")
    ]
    summary = (
        "未发现高置信度存储瓶颈。"
        if not high
        else (
            f"发现 {len(high)} 个高优先级问题：" + "; ".join(f["rule_id"] for f in high)
        )
    )
    result = {
        "schema_version": sv,
        "analyzed_at": None,
        "findings": findings,
        "summary": summary,
        "high_priority_count": len(high),
    }
    # 将 validation_errors 与 profile_validation_errors 提升到顶层。
    if verr:
        result["validation_errors"] = verr
    if pverr:
        result["profile_validation_errors"] = pverr
    return result


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
        if isinstance(raw_target_pid, int)
        and not isinstance(raw_target_pid, bool)
        and raw_target_pid > 0
        else None
    )
    if target_pid is None and not (isinstance(target_path, str) and target_path):
        return set(), False
    try:
        observation_samples = _strict_json_int(
            parsed.get("observation_samples"), positive=True
        )
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
            _canonical_dev(str(device))
            for device in (mapping.get("backing_devices") or [])
            if isinstance(device, str)
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
        observations = _strict_json_int(
            parsed.get("observation_samples"), positive=True
        )
    except ValueError:
        return {}
    intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for mapping in parsed.get("mappings", []) or []:
        if not isinstance(mapping, dict) or not _is_data_relevant_path(
            mapping.get("path"), target_path
        ):
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
            _canonical_dev(str(device))
            for device in (mapping.get("backing_devices") or [])
            if isinstance(device, str)
        )
        for device in devices:
            if device:
                intervals[device].append(interval)
    return dict(intervals)


def _target_binding_is_certified(snapshot: dict) -> bool:
    """Require an explicit target with strong block or current NFS identity evidence."""
    target = snapshot.get("target")
    if not isinstance(target, dict) or not (
        target.get("pid") is not None or bool(target.get("path"))
    ):
        return False
    _devices, block_identity_strong = _target_device_context(snapshot)
    nfs_mounts = [
        mount
        for mount in (snapshot.get("mounts") or [])
        if isinstance(mount, dict) and _norm_fstype_group(mount.get("fstype")) == "nfs"
    ]
    nfs_identities, _scope = _required_nfs_identities(snapshot, nfs_mounts)
    nfs_identity_strong = (
        bool(nfs_identities)
        and _provider_interval(snapshot, "mounts_provider") is not None
    )
    return block_identity_strong or nfs_identity_strong


def _project_host_assessments_to_target(
    snapshot: dict, assessments: list[dict]
) -> list[dict]:
    """有目标设备映射时，只允许目标 workload 的设备证据进入 R500。"""
    target_devices, target_identity_strong = _target_device_context(snapshot)
    target_mapping_intervals = _target_device_mapping_intervals(snapshot)
    nfs_mounts_for_target = [
        m
        for m in (snapshot.get("mounts") or [])
        if isinstance(m, dict) and _norm_fstype_group(m.get("fstype")) == "nfs"
    ]
    target_nfs_identities, target_nfs_scope = _required_nfs_identities(
        snapshot, nfs_mounts_for_target
    )
    target_nfs_known = bool(target_nfs_identities) or target_nfs_scope.endswith(
        "_non_nfs"
    )
    target = snapshot.get("target")
    target_requested = isinstance(target, dict) and (
        target.get("pid") is not None or bool(target.get("path"))
    )
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
            item["summary"] = (
                "目标 workload 的设备或挂载身份未解析，不能沿用全机 Host IO 结论。"
            )
            item.setdefault("missing_evidence", []).append(
                "目标 PID/路径到设备或 NFS 挂载的可靠映射"
            )
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
                if isinstance(mount, dict)
                and _nfs_identity(mount, source_key="source") in target_nfs_identities
            ]
            item["confirmed_mounts"] = confirmed
            if target_nfs_scope.endswith("_non_nfs"):
                item["performance_window_evaluated"] = True
                item["evidence_window_valid"] = True
            if not confirmed and finding.get("confirmed_mounts"):
                item["confidence"] = (
                    "high" if item.get("evidence_window_valid") else "none"
                )
                item["severity"] = "info"
                item["summary"] = "R200 evidence was outside target workload NFS scope."
            projected.append(item)
            continue
        if finding.get("rule_id") == "R300" and target_nfs_known:
            item = copy.deepcopy(finding)
            slow = [
                mount
                for mount in (item.get("metadata_slow_mounts") or [])
                if isinstance(mount, dict)
                and _nfs_identity(mount, source_key="source") in target_nfs_identities
            ]
            item["metadata_slow_mounts"] = slow
            if not slow and finding.get("metadata_slow_mounts"):
                item["confidence"] = "none"
                item["severity"] = "info"
                item["evidence_window_valid"] = False
                item["summary"] = (
                    "R300 metadata evidence was outside target workload NFS scope."
                )
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
                and (
                    target_pid_scope is None
                    or any(pid in target_pid_scope for pid in pids)
                )
            }
            intervals: dict[str, list[float]] = {}
            for device, interval in raw_intervals.items():
                canonical = _canonical_dev(str(device))
                parsed_interval = _finding_evidence_interval(
                    {"evidence_interval": interval}
                )
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
                item["summary"] = (
                    "R400 争抢证据未绑定到目标 workload 的设备和 PID 树，"
                    "不能进入 R500 传导链。"
                )
                item.setdefault("missing_evidence", []).append(
                    "目标 PID/进程树参与目标设备争抢的强身份同窗证据"
                )
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
            for mapping_interval in target_mapping_intervals.get(
                _canonical_dev(str(device.get("device") or "")), []
            ):
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
        overlap_intervals = [
            overlap
            for device in assessed
            if (overlap := _target_overlap(device)) is not None
        ]
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
            item["evidence_interval"] = list(
                max(overlap_intervals, key=lambda interval: interval[1] - interval[0])
            )
        else:
            item.pop("evidence_interval", None)
        if saturated:
            item["confidence"] = (
                "high"
                if any(device.get("level") == "sustained" for device in saturated)
                else "medium"
            )
            item["severity"] = "high" if item["confidence"] == "high" else "medium"
        elif assessed:
            item["severity"] = "info"
            if not target_health_complete:
                item["confidence"] = "low"
                item["summary"] = (
                    "目标 workload 映射设备的 util/queue/await 字段覆盖不足，"
                    "不能高置信排除 Host IO 压力。"
                )
                item.setdefault("missing_evidence", []).append(
                    "目标设备完整的 util + queue/await 指标"
                )
            elif finding.get("confidence") == "high":
                item["confidence"] = "high"
                item["summary"] = "目标 workload 映射设备在有效窗口内未检测到 IO 饱和。"
            else:
                item["confidence"] = (
                    str(finding.get("confidence"))
                    if finding.get("confidence") in _ORDER
                    else "none"
                )
                item["summary"] = (
                    "目标 workload 映射设备在当前窗口内未检测到 IO 饱和，但原始证据"
                    "不足以高置信排除偶发 Host IO 压力。"
                )
        else:
            item["confidence"] = "none"
            item["severity"] = "info"
            item["summary"] = "iostat 未覆盖目标 workload 映射设备，Host IO 状态未知。"
            item.setdefault("missing_evidence", []).append(
                f"目标设备 iostat 指标：{sorted(target_devices)}"
            )
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
            r200.get("performance_window_evaluated") is True
            and r200.get("evidence_window_valid") is True
        )
    elif r200_clear:
        r200_clear = r200.get("performance_window_evaluated") is True
    return r100_clear and r200_clear


def _profile_overlaps_finding(profile: dict, finding: dict | None) -> bool:
    """Require the profile to overlap a finding's actual dynamic evidence window."""
    if not isinstance(finding, dict):
        return False
    return intervals_have_common_overlap(
        _profile_interval(profile), _finding_evidence_interval(finding)
    )


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
    relevant_network_mount = bool(r200.get("network_mounts")) and not scope.endswith(
        "_non_nfs"
    )
    return not relevant_network_mount or _profile_overlaps_finding(profile, r200)


def analyze_r500_with_host(
    snapshot: dict, profile: dict, host_findings: list[dict] | bool
) -> dict:
    """R500 public API; Host findings are always re-derived from the snapshot.

    ``host_findings`` is retained for call compatibility but is never trusted. A caller
    cannot certify R500 by supplying a hand-written R100-R400 finding.

    确定性 analyzer 不重建或验证 profiler artifact；任何 JSON-only R500 结论
    置信度封顶 medium。可信 artifact verifier 实现前，不声称已认证的传导链。
    """
    # Public callers may invoke this rule directly. Normalize the snapshot/profile and
    # recompute every Host rule so supplied findings cannot forge certifying evidence.
    snapshot, profile, _validation_errors, _profile_errors, fatal = (
        validate_analysis_request(snapshot, profile)
    )
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
    host_ruled_out_same_window = _host_io_ruled_out_in_profile_window(
        assessments, profile
    )
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
            host_statement = (
                "Host IO 负向证据与 profiler 不同窗，不能确认 profiler 窗口内正常"
            )
            missing = [
                "profile.profile_window 与 Host IO 负向 evidence_interval 的足量公共交集"
            ]
        else:
            confidence = "low"
            host_statement = "Host IO 证据不足，尚不能确认正常或异常"
            missing = [
                "R100 有效设备窗口（iostat 或递增 diskstats）",
                "必要时补充 R200/R300/R400 证据",
            ]
        mte2_provenance_requirement = "profile.profile_window.scope + profile.provenance.mte2_ratio（认证 profiler database 来源）"
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
        if (
            host_ruled_out_same_window
            and device_free_certified
            and target_binding_certified
        ):
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
            missing = [
                "profile.profile_window 与 Host IO 负向 evidence_interval 的足量公共交集"
            ]
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
            "profile.profile_window.scope + profile.provenance.device_free_percent"
            "（认证 profiler timeline/DB 来源）"
        )
        if (
            not device_free_certified
            and device_free_provenance_requirement not in missing
        ):
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
            missing.append(
                "可信 profiler artifact verifier（核验 timeline/实验工件内容）"
            )
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
            missing.append(
                "profile.profile_window 与至少一个已确认 Host finding.evidence_interval 的足量公共交集"
            )
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
                missing_evidence=[
                    "可信 profiler artifact verifier（核验 timeline 中的设备空泡区间）"
                ],
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
                missing_evidence=[
                    "profile.profile_window 与已确认 Host finding.evidence_interval 的足量公共交集"
                ],
            )
    else:
        finding.update(
            confidence="medium",
            summary=f"{host_desc}，device Free={device_free_pct}%，需结合具体空泡段判断。",
        )
    return finding


def load_snapshot(path: str) -> dict:
    size = os.path.getsize(path)
    if size > _MAX_JSON_FILE_BYTES:
        raise ValueError(
            f"JSON file is {size} bytes; limit is {_MAX_JSON_FILE_BYTES} bytes"
        )
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    _validate_json_resources(payload)
    return payload


def _r500_host_findings_for_standalone(snapshot: dict) -> list[dict]:
    """单规则模式（--mode R500）下计算 Host 根因集合，复用 analyze_all 的同一逻辑。"""
    r100 = analyze_r100(snapshot)
    r200 = analyze_r200(snapshot)
    r300 = analyze_r300(snapshot)
    r400 = analyze_r400(snapshot, r100)
    return [r100, r200, r300, r400]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="IO Snapshot 确定性分析器（mindstudio-storage-analysis）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("snapshot", help="IO Snapshot JSON 文件路径")
    parser.add_argument(
        "--mode",
        "-m",
        default="all",
        choices=["all", "R000", "R100", "R200", "R300", "R400", "R500"],
        help="分析模式（默认 all）",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="可选的 NPU profiler 指标 JSON（含 device_free_percent/mte2_ratio）",
    )
    parser.add_argument(
        "-o", "--output", default=None, help="输出 findings JSON 文件路径"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认即 JSON）")
    args = parser.parse_args(argv)

    try:
        snapshot = load_snapshot(args.snapshot)
    except FileNotFoundError:
        print(f"错误: Snapshot 文件不存在: {args.snapshot}", file=sys.stderr)
        return 1
    except (ValueError, RecursionError, UnicodeDecodeError) as e:
        print(f"错误: Snapshot JSON 解析失败: {args.snapshot}: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        # 目录路径、权限及其他 OS 错误稳定返回非零且不泄露堆栈。
        print(f"错误: 无法读取 Snapshot {args.snapshot}: {e}", file=sys.stderr)
        return 1

    profile = None
    if args.profile:
        try:
            profile = load_snapshot(args.profile)
        except (OSError, ValueError, RecursionError, UnicodeDecodeError) as e:
            print(f"错误: profile 解析失败: {e}", file=sys.stderr)
            return 1

    # 所有 mode 共用 validate_analysis_request 处理 schema、时间和顶层错误。
    snapshot, profile, verr, pverr, fatal = validate_analysis_request(snapshot, profile)
    if args.profile and not ({"device_free_percent", "mte2_ratio"} & profile.keys()):
        pverr = sorted(
            set(pverr)
            | {"显式 --profile 未提供可用的 device_free_percent 或 mte2_ratio"}
        )
    profile_invalid = bool(args.profile and pverr)
    if fatal:
        result = {"error": fatal, "schema_version": "unknown", "findings": []}
    elif args.mode == "all":
        result = _analyze_validated(snapshot, profile, verr, pverr)
    else:
        func = {
            "R000": lambda: analyze_r000(snapshot),
            "R100": lambda: analyze_r100(snapshot),
            "R200": lambda: analyze_r200(snapshot),
            "R300": lambda: analyze_r300(snapshot),
            "R400": lambda: analyze_r400(snapshot, analyze_r100(snapshot)),
            "R500": lambda: analyze_r500_with_host(
                snapshot, profile or {}, _r500_host_findings_for_standalone(snapshot)
            ),
        }[args.mode]
        result = func()
        # 单规则结果也携带 validation_errors。
        if isinstance(result, dict):
            if verr:
                result["validation_errors"] = verr
            if pverr:
                result["profile_validation_errors"] = pverr

    # 顶层 error、unsupported schema 和输出写入失败均返回非零。
    out_text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        tmp = _temp_name(args.output)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(out_text)
            os.replace(tmp, args.output)
            print(f"findings 已写入: {args.output}", file=sys.stderr)
        except OSError as exc:
            print(f"错误: 无法写入 {args.output}: {exc}", file=sys.stderr)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return 3
    else:
        print(out_text)
    if isinstance(result, dict) and "error" in result:
        return 2
    if profile_invalid:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
