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
"""Local block-device analysis and rules R000/R100."""

from __future__ import annotations

import math
from typing import Any

from .common import (
    _delta,
    _f,
    _finding,
    _parsed,
    _provider,
    _provider_interval,
    _snapshot_duration,
    _snapshot_interval,
    _status,
)


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
            if any(float(d1.get(key, 0)) < float(d0.get(key, 0)) for key in counter_fields):
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


_AWAIT_BY_TYPE = {"hdd": 20.0, "ssd": 5.0, "unknown": 15.0}


_UTIL_HIGH = 90.0  # 单次采样"忙"的参考线


_UTIL_SUSTAINED_MEAN = 85.0  # 窗口平均"持续忙"的参考线


_AVGQU_HIGH = 2.0  # 队列积压参考线


_MIN_SAMPLES_HIGH = 3  # 声称"持续饱和"所需最少采样数（iostat 路径）


_BANDWIDTH_KBPS_HEURISTIC = 100 * 1024  # ≈100 MB/s


_IOPS_HEURISTIC = 10000.0


def _await_threshold(device_type: str | None) -> float:
    return _AWAIT_BY_TYPE.get(str(device_type or "unknown").lower(), _AWAIT_BY_TYPE["unknown"])


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

    has_util = any(key in d and d.get(key) is not None for key in ("util_percent", "util_max", "util_p95"))
    has_queue = "avgqu_sz" in d and d.get("avgqu_sz") is not None
    has_await = any(key in d and d.get(key) is not None for key in ("await", "r_await_ms", "w_await_ms"))
    has_rate = any(key in d and d.get(key) is not None for key in ("r_per_s", "w_per_s", "rkB_per_s", "wkB_per_s"))

    def _field_count(field: str) -> int:
        if field not in d or d.get(field) is None:
            return 0
        return _count(d.get(f"{field}_sample_count"), sample_count)

    util_sample_count = _count(d.get("util_sample_count"), sample_count if has_util else 0)

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
    queue_support_dense = queue_bad and queue_with_util_sample_count >= _MIN_SAMPLES_HIGH
    # "有量吞吐"：排除 r/s=1/rkB=4 这类几乎无 IO 的 util 失真场景
    total_kbps = rkB + wkB
    total_iops = r_per_s + w_per_s
    throughput_meaningful = total_kbps >= 1024 or total_iops >= 100
    # 设备基线接近上限（用户提供规格时才成立）
    base_max_mbps = _f(baseline.get("max_read_mbps"))
    base_max_write_mbps = _f(baseline.get("max_write_mbps"))
    base_max_iops = _f(baseline.get("max_iops"))
    near_bandwidth_ceiling = (base_max_mbps > 0 and (rkB / 1024) >= 0.85 * base_max_mbps) or (
        base_max_write_mbps > 0 and (wkB / 1024) >= 0.85 * base_max_write_mbps
    )
    near_iops_ceiling = base_max_iops > 0 and total_iops >= 0.85 * base_max_iops
    baseline_backed = near_bandwidth_ceiling or near_iops_ceiling

    # high：持续 + 压力 + (持续真实队列 OR 基线接近上限)。
    # 队列背书必须使用队列自身的共现样本，不能借用 await 的样本密度。
    confirmed = sustained and pressure and (queue_support_dense or (baseline_backed and pressure_samples_dense))
    # medium(likely)：持续 + 压力(await) + 有量吞吐，但无队列/基线背书
    likely = sustained and pressure and pressure_samples_dense and throughput_meaningful and not confirmed
    # medium(transient)：忙 + 有压力 + 有量吞吐，但非持续（偶发抖动 / 采样不足）。
    # 必须有量吞吐——util 高但吞吐极低（r/s=1/rkB=4）是 util 失真，不算饱和。
    transient = busy and pressure and throughput_meaningful and not (confirmed or likely)
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
        "health_evidence_dense": max(queue_with_util_sample_count, await_with_util_sample_count) >= _MIN_SAMPLES_HIGH,
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
    evidence_duration = evidence_interval[1] - evidence_interval[0] if evidence_interval is not None else None
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
    sparse = [a for a in assessed if a["health_evidence_complete"] and not a["health_evidence_dense"]]

    if sustained:
        finding["confidence"] = "high"
        finding["severity"] = "high"
        finding["saturated_devices"] = sustained
        subtypes = sorted({a["subtype"] for a in sustained})
        backing = "队列积压" if any(a.get("avgqu_sz", 0) >= _AVGQU_HIGH for a in sustained) else "设备基线接近上限"
        finding["summary"] = (
            f"检测到 {len(sustained)} 个设备持续 IO 饱和（{', '.join(subtypes)} 型，"
            f"窗口持续高 util + {backing}）："
            + "; ".join(f"{a['device']} util_mean={a['util_percent']}%" for a in sustained[:3])
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
            + "; ".join(f"{a['device']} util_max={a['util_max']}%" for a in transient[:3])
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
            finding.setdefault("missing_evidence", []).append("每设备至少 3 个同时覆盖 util 与 queue/await 的有效样本")
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
            finding["summary"] = "未检测到设备 IO 饱和（窗口内 util/await/队列均在参考线内）。"
        finding["note"] = "阈值按设备类型区分；NVMe 的 util 在多队列下含义弱化。"
    if short_window:
        if finding.get("confidence") == "high":
            finding["confidence"] = "medium"
            if finding.get("severity") == "high":
                finding["severity"] = "medium"
            finding["summary"] += f" 但实际证据窗口仅 {duration:.1f}s，短于 10s，置信度封顶 medium。"
        else:
            finding["summary"] += f" 实际证据窗口仅 {duration:.1f}s，短于 10s；短窗口不能认证持续压力或健康。"
        missing = "更长的同窗采样（建议 30s，短窗口不得认证持续压力或健康）"
        if missing not in finding.setdefault("missing_evidence", []):
            finding["missing_evidence"].append(missing)
    if evidence_interval is None:
        finding.setdefault("missing_evidence", []).append(f"{source} 有效采集时间窗（用于与 PID/NPU 证据做因果对齐）")
        if finding.get("confidence") == "high":
            finding["confidence"] = "medium"
            if finding.get("severity") == "high":
                finding["severity"] = "medium"
            finding["summary"] += (
                " 但数据源缺少有效 started_at/ended_at 时间窗，无法确认数据新鲜度，置信度封顶 medium。"
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
