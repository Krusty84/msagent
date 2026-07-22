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

设计要点（对应审核报告 P1 修复）：
  - mte2_ratio 完全移出 NPU 传导链的"必需证据"。MTE2 是 AI Core 内部/邻近
    存储层的数据搬运（GM→UB/L1），高占比只代表算子内数据搬运压力，不能证明
    Host 存储/DataLoader 供给不足。高 mte2 + Host IO 正常时应转交计算分析。
  - NPU 传导链用 step throughput / device Free / DataLoader wait / batch ready
    与 Host IO 异常的"同窗相关性"，三档置信度。
  - R200 拆成两层：仅"识别为网络挂载"不构成瓶颈，必须有 RTT/execute/retrans/
    吞吞吐等性能证据才能确认。
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
    major = int(sv.split(".")[0])
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
    window = snapshot.get("window")
    if isinstance(window, dict):
        try:
            start = float(window.get("start"))
            end = float(window.get("end"))
        except (TypeError, ValueError, OverflowError):
            return None
        if math.isfinite(start) and math.isfinite(end) and end > start:
            return end - start
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
            # DEFECT-3 自审（subagent）：先 spread metrics 再覆盖 name，避免 metrics 内的
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

# 设备类型 → r_await 参考线（毫秒）。Review P1-3：HDD/SSD 区分，未知用保守值避免误报。
_AWAIT_BY_TYPE = {"hdd": 20.0, "ssd": 5.0, "unknown": 15.0}
_UTIL_HIGH = 90.0  # 单次采样"忙"的参考线
_UTIL_SUSTAINED_MEAN = 85.0  # 窗口平均"持续忙"的参考线
_AVGQU_HIGH = 2.0  # 队列积压参考线
_MIN_SAMPLES_HIGH = 3  # 声称"持续饱和"所需最少采样数（iostat 路径）
# 吞吐/IOPS "明显有量"的经验值（无设备规格时，仅用于区分 io_pressure vs bandwidth/iops）
_BANDWIDTH_KBPS_HEURISTIC = 100 * 1024  # ≈100 MB/s
_IOPS_HEURISTIC = 10000.0
_IOPS_HIGH = 5000.0  # 小 IO IOPS 参考（R300 small-IO 候选用）


def _await_threshold(device_type: str | None) -> float:
    return _AWAIT_BY_TYPE.get(
        str(device_type or "unknown").lower(), _AWAIT_BY_TYPE["unknown"]
    )


def _classify_r100_disk(d: dict) -> dict:
    """评估单个设备的饱和度，返回结构化判定（Review P1-1/P1-3）。

    high（sustained）需"持续窗口 + 真实队列积压 或 设备基线接近上限"——
    避免 util 高但吞吐极低（NVMe util 失真/采样伪影）或仅偶发抖动被误判 high。
    无设备基线时，util 高 + await 高但队列低只能到 medium（likely）。
    """
    util = _f(d.get("util_percent"))
    util_max = _f(d.get("util_max"), util)
    util_p95 = _f(d.get("util_p95"), util)
    r_await = _f(d.get("r_await_ms"))
    w_await = _f(d.get("w_await_ms"))
    avgqu = _f(d.get("avgqu_sz"))
    r_per_s = _f(d.get("r_per_s"))
    w_per_s = _f(d.get("w_per_s"))
    rkB = _f(d.get("rkB_per_s"))
    wkB = _f(d.get("wkB_per_s"))
    device_type = d.get("device_type") or "unknown"
    try:
        sample_count = int(
            d.get("sample_count") or (2 if d.get("_from_diskstats") else 1)
        )
    except (TypeError, ValueError, OverflowError):
        sample_count = 1
    await_thr = _await_threshold(device_type)
    # DEFECT-2 自审（subagent）：baseline 必须是 dict（非 dict 真值会让 .get 崩溃）。
    raw_baseline = d.get("baseline")
    baseline = raw_baseline if isinstance(raw_baseline, dict) else {}

    busy = util >= _UTIL_HIGH or util_max >= _UTIL_HIGH
    sustained = (util >= _UTIL_SUSTAINED_MEAN) and (
        sample_count >= _MIN_SAMPLES_HIGH or d.get("_from_diskstats")
    )
    read_await_bad = r_per_s > 0 and r_await >= await_thr
    write_await_bad = w_per_s > 0 and w_await >= await_thr
    await_bad = read_await_bad or write_await_bad
    queue_bad = avgqu >= _AVGQU_HIGH
    # 压力信号：await 超设备类型阈值 或 队列积压。NVMe 高 util 但 await/队列正常 = util 失真，不算压力。
    pressure = await_bad or queue_bad
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

    # high：持续 + 压力 + (真实队列 OR 基线接近上限)
    confirmed = sustained and pressure and (queue_bad or baseline_backed)
    # medium(likely)：持续 + 压力(await) + 有量吞吐，但无队列/基线背书
    likely = sustained and pressure and throughput_meaningful and not confirmed
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
        "await_threshold_ms": await_thr,
        "subtype": subtype,
        "level": level,
        "pressure_confirmed": pressure,
        "baseline_backed": baseline_backed,
    }


def analyze_r100(snapshot: dict) -> dict:
    """R100 吞吐 / IOPS 饱和（设备忙）。Review P1-1/P1-3：窗口聚合 + 设备类型 + 分级置信。

    - high：持续饱和（util_mean≥85 且采样≥3）+ await/队列超设备类型阈值。
    - medium：偶发饱和（util 偶高但非持续），或采样不足，或 util 高但 await/队列正常。
    - info（high confidence 无饱和）：窗口内无任何饱和迹象。
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
        duration = _snapshot_duration(snapshot)
        short_window = duration is not None and duration < 10
        if short_window:
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
        return f"{host.strip().lower()}:{path.rstrip('/')}"
    return s.strip().lower()


def _norm_fstype_group(ft: Any) -> str:
    """nfs/nfs4 视为同一兼容组（第八轮 P1-3）。"""
    ft = str(ft or "").strip().lower()
    return "nfs" if ft in ("nfs", "nfs4") else ft


def _nfs_identity(item: dict, source_key: str = "device") -> tuple[str, str, str]:
    """Return normalized (source, mount_point, fstype) identity for an NFS mount/metric."""
    return (
        _norm_nfs_source(item.get(source_key)),
        str(item.get("mount_point", "")).rstrip("/"),
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
        matching = [
            m
            for m in nfs_mounts
            if isinstance(m, dict)
            and _path_under_mount(target_path, str(m.get("mount_point", "")))
        ]
        if matching:
            chosen = max(matching, key=lambda m: len(str(m.get("mount_point", ""))))
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
    if _status(provider) == "ok" and isinstance(parsed, dict):
        for mapping in parsed.get("mappings", []) or []:
            if not isinstance(mapping, dict):
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

    return current, "all_current_nfs_mounts"


def _bind_nfs_metrics(
    metrics: list[dict], nfs_mounts: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """第八轮 P1-3：按 (source, mount_point, fstype) 身份绑定 NFS metric 到当前挂载。

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
        mp = str(mm.get("mount_point", "")).rstrip("/")
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


def analyze_r200(snapshot: dict) -> dict:
    """R200 网络存储 / 挂载延迟。

    拆两层：(a) 识别为网络挂载（仅类型，非瓶颈）；
            (b) 确认瓶颈（必须有 RTT/execute/retrans/吞吐等性能证据）。
    仅 (a) 成立而 (b) 缺证据时，confidence=low 并列入 missing_evidence。

    覆盖范围（Review P1-6 诚实收窄）：自动性能确认**仅 NFS**（依赖
    /proc/self/mountstats per-op 指标）。CIFS/Lustre/GPFS/BeeGFS/Ceph/FUSE 仅
    "识别 + 人工指导"，不自动判瓶颈——这些文件系统需各自专用 provider
    （cifsiostat、lctl get_param、mmrepquota 等），当前未实现，标注 handoff。
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
    mounts_interval_valid = _provider_interval(snapshot, "mounts_provider") is not None

    mounts = snapshot.get("mounts", []) or []
    net_mounts = [
        m
        for m in mounts
        if isinstance(m, dict)
        and (
            str(m.get("fstype", "")).lower()
            in {"nfs", "nfs4", "cifs", "lustre", "gpfs", "beegfs", "ceph"}
            or str(m.get("fstype", "")).lower().startswith(("nfs", "fuse."))
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

    # 区分 NFS（可自动确认）与非 NFS 网络存储（仅识别 + 人工指导）
    nfs_mounts = [
        m
        for m in net_mounts
        if str(m.get("fstype", "")).lower() in {"nfs", "nfs4"}
        or str(m.get("fstype", "")).lower().startswith("nfs")
    ]
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

    # 第八轮 P1-3：NFS 性能证据按 (source, mount_point, fstype) 身份绑定到当前挂载。
    #   strong（身份完全匹配）→ 可 high；weak（无 source 仅路径匹配）→ 不得单独 high；
    #   unmatched（路径不在当前挂载）→ 忽略，避免旧/混入 metric 拼接为因果链。
    strong, weak, unmatched_metrics = _bind_nfs_metrics(metrics, nfs_mounts)
    # 只有当前采样窗的差值才是性能证据。cumulative/缺失/非法窗口只保留为
    # 背景信息，不能把开机以来的历史累计值归因给当前 workload。
    all_windowed = [mm for mm in strong if mm.get("windowing") == "delta"]
    non_windowed = [mm for mm in strong if mm.get("windowing") != "delta"]
    required_identities, required_scope = _required_nfs_identities(snapshot, nfs_mounts)
    target_nfs_irrelevant = required_scope.endswith("_non_nfs")
    if required_identities:
        windowed = [
            mm
            for mm in all_windowed
            if _nfs_identity(mm, source_key="source") in required_identities
        ]
    elif target_nfs_irrelevant:
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
        finding["evidence_window_valid"] = (
            mounts_interval_valid and _provider_interval(snapshot, "nfs") is not None
        )
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
    # 性能判据（Review P1-4：不再"一次重传即 high"，改用比率 + 最小样本 + 延迟阈值）。
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
        tx = float(tx_raw) if tx_raw is not None else 0.0
        major = mm.get("major_timeouts") or 0
        try:
            rtt = float(rtt)
            execute = float(execute)
            retrans = float(retrans)
            ops = float(ops)
            tx = float(tx)
            major = float(major)
        except (TypeError, ValueError, OverflowError):
            rtt = execute = retrans = ops = tx = major = 0.0
        # 与 nfs-utils/nfsiostat 一致：重传次数 / 原始操作请求数。
        counter_consistent = (
            tx_raw is not None
            and ops >= 0
            and retrans >= 0
            and tx >= ops
            and math.isclose(retrans, tx - ops, rel_tol=1e-6, abs_tol=1e-6)
        )
        if not counter_consistent:
            invalid_counter_metrics += 1
        retrans_ratio = (retrans / ops) if ops > 0 and counter_consistent else 0.0
        sufficient_ops = ops >= _MIN_OPS
        latency_bad = rtt >= _RTT_HIGH_MS or execute >= _EXEC_HIGH_MS
        retrans_bad = (
            sufficient_ops
            and counter_consistent
            and retrans_ratio >= _RETRANS_RATIO_HIGH
        )
        # 确认判据：延迟高（需充足样本）或 重传率达标（需充足样本）或 任意 major timeout
        confirmed_flag = (
            (latency_bad and sufficient_ops)
            or retrans_bad
            or major >= _MAJOR_TIMEOUT_ANY
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
            f"{invalid_counter_metrics} 个 NFS metric 的 ops/transmissions/retrans 缺失或不一致，重传证据已忽略。"
        )

    if confirmed:
        nfs_interval_valid = (
            mounts_interval_valid and _provider_interval(snapshot, "nfs") is not None
        )
        finding["evidence_window_valid"] = nfs_interval_valid
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
                "mounts_provider and nfs started_at/ended_at overlapping snapshot.window"
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
            # 第八轮 P1-3：存在 source 缺失的弱匹配 metric → 提示补 source（mountstats 设备字段），
            # 便于强身份绑定后再次确认；置信度仍不 high。
            finding["confidence"] = "medium"
            finding.setdefault("handoff_notes", []).append(
                "存在缺 source 的 NFS metric（仅路径弱匹配），无法做 (source,mount_point,fstype) "
                "身份强绑定；建议确认 mountstats 设备字段后重新分析。"
            )
    # 非 NFS 网络存储：明确标注"识别 + 人工指导"（Review P1-6 诚实收窄）
    if non_nfs_mounts:
        nn = ", ".join(sorted({str(m.get("fstype")) for m in non_nfs_mounts}))
        finding.setdefault("handoff_notes", []).append(
            f"非 NFS 网络存储（{nn}）仅识别类型，自动性能确认未实现："
            f"CIFS 用 cifsiostat/`/proc/fs/cifs/Stats`；Lustre 用 `lctl get_param osc.*.stats`；"
            f"GPFS/BeeGFS 用各自客户端统计。需人工采集后判定。"
        )
    return finding


def analyze_r300(snapshot: dict) -> dict:
    """R300 远程文件访问 / 元数据 / 小文件开销（Review P1-7：补直接证据）。

    证据强度：
      - **强（可确认）**：NFS mountstats 中元数据 op（GETATTR/LOOKUP/READDIR/...）
        窗内平均 RTT/execute 偏高——直接反映 open/stat/lookup 的远程访问耗时。
      - 中（候选）：iostat 小 IO 特征（高 IOPS + 低平均 IO 大小）。
      - 背景（不单独产生根因）：df inode 使用率（容量信号，非延迟证据）。
    """
    finding = _finding(
        "R300",
        "medium",
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
        # 第八轮 P1-3：元数据证据按 (source, mount_point, fstype) 身份绑定（与 R200 共用 helper）。
        nfs_mounts_for_bind = [
            m
            for m in (snapshot.get("mounts") or [])
            if isinstance(m, dict)
            and str(m.get("fstype", "")).lower().startswith("nfs")
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

    # 中证据：iostat 小 IO 特征
    disks, _ = _collect_disks(snapshot)
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
    if small_io_disks:
        finding["evidence_fields"].append("iostat.disks（小 IO 特征）")
    if high_inode:
        finding["evidence_fields"].append("df.filesystems（inode 使用·背景）")

    parts: list[str] = []
    if meta_slow:
        nfs_interval_valid = (
            _provider_interval(snapshot, "mounts_provider") is not None
            and _provider_interval(snapshot, "nfs") is not None
        )
        finding["evidence_window_valid"] = nfs_interval_valid
        finding["confidence"] = "high" if nfs_interval_valid else "medium"
        finding["severity"] = "high" if nfs_interval_valid else "medium"
        parts.append(
            f"{len(meta_slow)} 个网络挂载的元数据 op（GETATTR/LOOKUP/READDIR）窗内延迟偏高"
        )
        finding["metadata_slow_mounts"] = meta_slow
        if not nfs_interval_valid:
            finding.setdefault("missing_evidence", []).append(
                "mounts_provider and nfs started_at/ended_at overlapping snapshot.window"
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
    if high_inode:
        # inode 仅作背景信息输出，不改变 confidence（容量信号非延迟证据）。
        finding["high_inode_fs"] = high_inode
        parts.append(f"{len(high_inode)} 个文件系统 inode 使用率 >= 80%（背景信息）")

    if parts:
        finding["summary"] = "；".join(parts) + "。"
    else:
        finding["confidence"] = "low"
        finding["summary"] = (
            "缺少远程文件访问/元数据开销的直接证据（元数据 op 延迟、syscall 频率、cache 命中）。"
        )
        finding["missing_evidence"] = [
            "nfs.mount_metrics 的 GETATTR/LOOKUP/READDIR 窗内延迟",
            "数据集文件数量与平均大小（find | wc -l）",
            "page cache 命中率（第二次访问延迟对比）",
        ]
    return finding


# 共享库/日志/解释器/系统文件路径——这些路径上的多 PID 共享设备**不构成**数据 IO 争抢
# （Review P1-5：避免把"共同打开根盘文件"误报为 IO 争抢）。
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
    """第八轮 P1-4：规范化绝对路径——折叠重复 `/`、解析 `.`/`..`、去 trailing slash。

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

    第七轮 P2-2：路径组件边界匹配（`path == prefix` 或 `path` 位于 `prefix/` 下），
    避免 `/usrdata` 被 `/usr` 前缀误伤。
    第八轮 P1-4：target 先规范化（折叠 `//`、解析 `.`/`..`）。规则优先级：
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


def _active_io_pids(procs: list[dict], threshold: float = 100.0) -> set[int]:
    """pidstat 中有活跃 IO 的 PID 集合（kB_rd/s 或 kB_wr/s 超阈值，默认 100KB/s）。"""
    out: set[int] = set()
    for p in procs:
        try:
            kbr = float(p.get("kbr_per_s", 0) or 0)
            kbw = float(p.get("kbw_per_s", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if kbr >= threshold or kbw >= threshold:
            try:
                out.add(int(p.get("pid")))
            except (TypeError, ValueError, OverflowError):
                # G0-6 自审：int(float('inf')) 或 int(超大 float) 抛 OverflowError，
                # 旧守卫只捕获 TypeError/ValueError 会漏。pid 非法即跳过该进程。
                continue
    return out


def analyze_r400(snapshot: dict, r100_finding: dict | None = None) -> dict:
    """R400 多 rank / 多 worker / 多实例 IO 干扰。

    高置信度需**同时**满足（Review P1-5：避免共享根盘/打开 FD 误报）：
      1. ≥2 个不同 PID 映射到同一设备；
      2. 至少一条映射路径"数据相关"（排除共享库/日志/解释器/系统文件）；
      3. 这些 PID 在 pidstat 中有活跃 IO；
      4. 该设备在 R100 中饱和。
    任一缺失则降级为 medium/low/none，并在 missing_evidence 说明缺哪项。
    """
    if r100_finding is None:
        r100_finding = analyze_r100(snapshot)

    finding = _finding(
        "R400",
        "high",
        "none",
        "",
        next_checks=[
            "提供 --pid 或 --path，建立 rank/worker → PID → 设备映射",
            "做单卡 vs 多卡对照，观察加卡后是否变慢",
            "每 rank 用独立 shard，观察争抢是否消失",
        ],
    )

    pmap_pr = _provider(snapshot, "process_io_map")
    mappings: list[dict] = []
    if _status(pmap_pr) == "ok" and isinstance(_parsed(pmap_pr), dict):
        mappings = (_parsed(pmap_pr) or {}).get("mappings", []) or []

    pidstat_pr = _provider(snapshot, "pidstat")
    procs: list[dict] = []
    if _status(pidstat_pr) == "ok" and isinstance(_parsed(pidstat_pr), dict):
        procs = (_parsed(pidstat_pr) or {}).get("processes", []) or []
    active_pids = _active_io_pids(procs)

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

    # 按 canonical_device 聚合（Review P1-2：/dev/sda1→sda，/dev/mapper/*→dm-* 等）
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
        # dm/md/LVM 映射优先按共享底层设备聚合；无 backing 时按 canonical。
        contention_devices = (
            set(backing) if backing else ({canonical} if canonical else set())
        )
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
            pid_i = int(pid) if pid is not None else None
        except (TypeError, ValueError, OverflowError):
            # G0-6 自审：补 OverflowError（int(float('inf')) / int(超大 float)）。
            pid_i = None
        path_relevant = _is_data_relevant_path(m.get("path"), target_path=target_path)
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
            "device_saturated": bool(topology & saturated_devs),
            "identity_strong": resolution == "sysfs",
            "topology": sorted(topology),
            "mapping_identity": canonical,
        }
        for device in contention_devices:
            dev_to_entries[device].append(dict(entry))

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
            "已建立 PID→设备映射，但未发现多个进程访问同一设备（无争抢）。"
        )
        if skipped_virtual_mounts:
            finding["note"] = (
                f"已忽略 {skipped_virtual_mounts} 条 tmpfs/overlay/proc/sysfs 等"
                "非持久伪文件系统映射。"
            )
        return finding

    # 逐设备评估证据完整性（Review P1-3：data_relevant 与 active_io 必须绑定到同一 PID）
    confirmed: dict[str, list] = {}
    causal_candidates: dict[str, list] = {}
    weak: dict[str, list] = {}
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
        if len(strong_pids) >= 2 and saturated:
            causal_candidates[dev] = sorted(strong_pids)
            identity_pids = {
                e["pid"]
                for e in entries
                if e["pid"] in strong_pids and e["identity_strong"]
            }
            unambiguous_pids = {
                pid for pid in strong_pids if len(pid_devices.get(pid, set())) == 1
            }
            if identity_pids == strong_pids and unambiguous_pids == strong_pids:
                confirmed[dev] = sorted(strong_pids)
            else:
                weak[dev] = pids
                if identity_pids != strong_pids:
                    missing_reasons.append(
                        f"{dev}: PID→设备身份缺少 sysfs 精确解析，不能用 heuristic/unknown 身份确认争抢"
                    )
                if unambiguous_pids != strong_pids:
                    missing_reasons.append(
                        f"{dev}: pidstat 是每 PID 聚合 IO，PID 同时映射多个数据设备，无法归因到该设备"
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
        # high 结论要求 iostat/pidstat/process_io_map 三者都有合法时间窗，且
        # 存在正长度公共交集。缺失/非法时间不是“无法证伪即同窗”，而是证据不足。
        r100_win_raw = r100_finding.get("evidence_interval") if r100_finding else None
        r100_win = (
            tuple(r100_win_raw)
            if isinstance(r100_win_raw, (list, tuple)) and len(r100_win_raw) == 2
            else None
        )
        pidstat_win = _provider_interval(snapshot, "pidstat")
        pmap_win = _provider_interval(snapshot, "process_io_map")
        overlap_ok = intervals_have_common_overlap(r100_win, pidstat_win, pmap_win)
        finding["candidate_device_pid_conflicts"] = causal_candidates
        finding["evidence_fields"] = [
            "process_io_map.mappings",
            "pidstat.processes",
            "R100.saturated_devices",
        ]
        if overlap_ok:
            finding["device_pid_conflicts"] = confirmed
            finding["evidence_window_valid"] = True
            finding["confidence"] = "high"
            finding["severity"] = "high"
            finding["summary"] = (
                f"检测到 {len(confirmed)} 个设备被多个进程同时、活跃地争抢数据 IO（每 PID 均含"
                f"数据路径+活跃 IO 且设备饱和）：" + _format_device_pid_map(confirmed)
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
        # 第八轮 P1-1C：empty/unsupported 也必须报告，不得描述为"关键数据源均可用"。
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

    Review P1-4：用于校验外部 Snapshot/profile，"oops" 这类非法值必须被拒绝而非静默置 0。
    """
    if v is None or v == "":
        return None
    # 第七轮 P2-3：bool 不是合法数值（float(True)=1.0 会静默改变语义）。
    if isinstance(v, bool):
        raise ValueError("bool is not a valid float")
    try:
        f = float(v)  # "oops" → ValueError；"5"/5/5.0 → OK
    except OverflowError as exc:
        raise ValueError("float overflow") from exc
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("non-finite float")
    return f


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
    "pidstat_proc": ("kbr_per_s", "kbw_per_s", "kbccwd_per_s", "sample_count"),
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
_INTEGER_FIELDS = {"sample_count"}


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
                continue
            if value < 0:
                return False
            if key in _PERCENT_FIELDS and value > 100:
                return False
            if key in _INTEGER_FIELDS and not value.is_integer():
                return False
    return True


def normalize_and_validate(snapshot: dict) -> tuple[dict, list[str]]:
    """统一输入契约入口（第六轮 P1-3）：全量与所有单规则 --mode 入口共用。

    校验并规范化外部 Snapshot：顶层 dict、各 provider parsed 的容器与数值字段、
    diskstats 深层（设备值/timestamp）、iostat sample_count、availability 元素。
    单个 provider 损坏 → 标 parse_failed + availability.errors，其他规则继续运行，绝不崩溃。
    返回 (规范化后的 snapshot, errors)。
    """
    if not isinstance(snapshot, dict):
        return {}, ["snapshot: not a dict"]
    # 自审：深拷贝，避免就地修改调用方的 dict（最小惊讶原则）。
    snapshot = copy.deepcopy(snapshot)
    errors: list[str] = []
    legacy_availability = copy.deepcopy(snapshot.get("availability"))

    # 第八轮 P2-4：legacy iostat list → dict 必须在数值契约校验之前完成，
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
            if disk_fields:
                disks = parsed.get("disks")
                if disks is None:
                    parsed["disks"] = {}
                    disks = parsed["disks"]
                if not isinstance(disks, dict):
                    raise ValueError(f"disks not object ({type(disks).__name__})")
                for dname, metrics in disks.items():
                    if not _validate_numeric_dict(metrics, disk_fields):
                        raise ValueError(f"disk {dname} has invalid metric")
            # 第六轮自审 P2：list_key 存在但非 list → 直接 parse_failed
            # （覆盖 pidstat.processes / df.filesystems / nfs.mount_metrics，
            #  与 process_io_map.mappings 的保护对称，避免下游 .get 崩溃）。
            if elem_fields and list_key:
                seq = parsed.get(list_key)
                if seq is not None and not isinstance(seq, list):
                    raise ValueError(f"{list_key} not list ({type(seq).__name__})")
                # G0-4 自审：显式 null → 强制为空列表，避免下游 .get(key, []) 返回 None 遍历崩溃。
                if seq is None:
                    parsed[list_key] = []
                if isinstance(parsed.get(list_key), list):
                    for i, el in enumerate(parsed[list_key]):
                        if not _validate_numeric_dict(el, elem_fields):
                            raise ValueError(f"{list_key}[{i}] has invalid metric")
            # nfs mount_metrics 必须是 dict 列表（nfs 不走 elem_fields，单独校验容器）
            if name == "nfs":
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
    # DEFECT-1 自审（subagent）：collector 从 `df -iP` 解析的 iuse_percent 形如 "92%"（带 %），
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

    # Review P1-3：process_io_map.mappings 必须是 dict 列表（元素类型校验）
    pmap = snapshot.get("process_io_map")
    if isinstance(pmap, dict) and pmap.get("status") == "ok":
        parsed = pmap.get("parsed")
        if isinstance(parsed, dict):
            mappings = parsed.get("mappings")
            if isinstance(mappings, list):
                cleaned = [m for m in mappings if isinstance(m, dict)]
                if len(cleaned) != len(mappings):
                    parsed["mappings"] = cleaned
                    errors.append(
                        f"process_io_map: dropped {len(mappings) - len(cleaned)} non-dict mapping(s)"
                    )
            elif mappings is not None:
                snapshot["process_io_map"] = {
                    **pmap,
                    "status": "parse_failed",
                    "parsed": None,
                    "error": "mappings not list",
                }
                errors.append("process_io_map: mappings not list")

    # G0-6 自审（final gate）：target 顶层字段必须是 dict（{path: str}），
    # 否则 R400 第 945 行 (snapshot.get("target") or {}).get("path") 会在
    # 非空字符串/数字/list/bool 上崩溃（'str'/'bool' object has no attribute 'get'）。
    tgt = snapshot.get("target")
    if not isinstance(tgt, dict):
        if tgt is not None:
            errors.append(f"target: not a dict ({type(tgt).__name__}), ignored")
        snapshot["target"] = {}

    # Review 第六轮自检：mounts 顶层字段必须是 dict 列表，否则 R200/R300 迭代会崩溃。
    mnts = snapshot.get("mounts")
    if not isinstance(mnts, list):
        if mnts is not None:
            errors.append(f"mounts: not a list ({type(mnts).__name__}), ignored")
        snapshot["mounts"] = []
    else:
        cleaned_m = [m for m in mnts if isinstance(m, dict)]
        if len(cleaned_m) != len(mnts):
            errors.append(
                f"mounts: dropped {len(mnts) - len(cleaned_m)} non-object entr(y/ies)"
            )
            snapshot["mounts"] = cleaned_m

    mounts_provider = snapshot.get("mounts_provider")
    if not isinstance(mounts_provider, dict):
        inferred_status = "ok" if snapshot["mounts"] else "missing"
        if isinstance(legacy_availability, dict):
            legacy_missing = legacy_availability.get("missing") or []
            legacy_partial = legacy_availability.get("partial") or []
            legacy_errors = legacy_availability.get("errors") or []
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
        if status in {"ok", "empty"}:
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

    # Review 第六轮 P1-3：diskstats_sample 深层校验——每个 sample 的 disks 必须是 dict，
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
        # 第八轮自审：始终写回 cleaned_ds——timestamp 强制转换（'5'→5.0）即使 bad==0
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

    # 第七轮 P1-2 legacy list→dict 转换已提前到数值契约校验之前（第八轮 P2-4）。

    # Review 第六轮 P1-3：iostat sample_count 必须是非负整数（禁止裸 int() 信任外部字符串）
    iostat_pr = snapshot.get("iostat")
    if isinstance(iostat_pr, dict) and iostat_pr.get("status") == "ok":
        ip = iostat_pr.get("parsed")
        if isinstance(ip, dict) and isinstance(ip.get("disks"), dict):
            for dname, metrics in ip["disks"].items():
                if not isinstance(metrics, dict):
                    continue
                sc = metrics.get("sample_count")
                if sc is not None:
                    try:
                        n = int(sc)
                        if n < 0 or str(sc) not in (str(n), str(float(sc))):
                            raise ValueError
                        metrics["sample_count"] = n
                    except (TypeError, ValueError, OverflowError):
                        metrics.pop("sample_count", None)
                        errors.append(f"iostat disk {dname}: bad sample_count dropped")

    # 第七轮 P1-1：从 provider 实际状态重建 availability（不信任调用方传入的）。
    _PROVIDER_NAMES = (
        "mounts_provider",
        "iostat",
        "pidstat",
        "nfs",
        "df",
        "process_io_map",
        "memory",
        "block_devices",
    )
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
            # G0-1 自审：unhashable status（list/dict）或非法枚举 → parse_failed，不崩溃。
            verr.add(f"{pname}: invalid status {st!r}")
            pr["status"] = "parse_failed"
            pr.setdefault("error", f"invalid status: {st!r}")
        elif st == "missing":
            missing.add(pname)
        elif st in ("permission_denied", "command_failed", "parse_failed"):
            errors.append(f"{pname}: {st}")
        elif st in ("empty", "unsupported"):
            partial.add(f"{pname}: {st}")
    # G0-2 自审：从 provider 实际状态**完全重建** availability（不用调用方残留值合并），
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
    # 第八轮 P1-1D：validation_errors 与 provider errors 分离，单次构造，禁止二次 append 重复。
    validation_errors = sorted(set(errors) | verr)
    return snapshot, validation_errors


_VALID_EXPERIMENT_RESULTS = {"improved", "no_change", "worse", "inconclusive"}


def _normalize_profile(profile: dict | None) -> dict:
    """校验 profile 数值字段与嵌套 conduction_evidence（Review P1-2/P1-4）。

    保留单返回值（clean dict）以兼容既有调用；完整错误见 _normalize_profile_with_errors。
    """
    return _normalize_profile_with_errors(profile)[0]


def _normalize_profile_with_errors(profile: dict | None) -> tuple[dict, list[str]]:
    """校验 profile 并返回 (clean, errors)。第八轮 P2-1：errors 顶层可见。

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
    # 第七轮 P2-3：数值范围校验（bool 已被 _strict_float 拒绝）。
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
        clean_ce["io_npu_overlap_observed"] = True
    elif ov is False or ov is None:
        pass  # False/缺失：不置位
    else:
        errors.append(f"io_npu_overlap_observed={ov!r} 非 boolean，忽略")
    exp = ce.get("controlled_experiment")
    if exp is not None:
        if isinstance(exp, dict):
            result = exp.get("result")
            if isinstance(result, str) and result in _VALID_EXPERIMENT_RESULTS:
                clean_ce["controlled_experiment"] = {"result": result}
            elif result is None:
                pass
            else:
                errors.append(f"controlled_experiment.result={result!r} 非法枚举，忽略")
        else:
            errors.append(f"controlled_experiment={exp!r} 非对象，忽略")
    clean["conduction_evidence"] = clean_ce
    return clean, errors


def validate_analysis_request(
    snapshot: dict, profile: dict | None = None
) -> tuple[dict, dict, list[str], list[str], str | None]:
    """第八轮 P1-1：唯一输入契约入口，所有 mode（all/R000-R500）与 eval 共用。

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
    sv = snapshot.get("schema_version")
    if sv is None:
        # 第八轮 P1-1 item 8：缺 schema_version → legacy 兼容（按 1.x 处理）+ 显式 warning，不静默默认。
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
    # 第八轮 P1-1B：collected_at 缺失 → validation_error（所有 mode 可见），但不阻断分析
    # （schema 仍可用；窗口分析在无 timestamp 时退化为"无法证伪"，见 interval_overlap_ratio）。
    profile, pverr = _normalize_profile_with_errors(profile)
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
    """第八轮 P1-2：两个时间区间的重叠占比（相对较短区间）。

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
    """第八轮 P2-3：生成同目录下唯一的临时文件名（含 PID + 随机串），避免并发碰撞。"""
    import uuid

    d, base = os.path.split(path)
    return os.path.join(d or ".", f".{base}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def analyze_all(snapshot: dict, profile: dict | None = None) -> dict:
    """运行全部规则，返回 {schema_version, findings: [...], summary}。

    第八轮 P1-1：经 validate_analysis_request 统一入口；schema/顶层 fatal → 结构化 error。
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

    # Review P1-5：NPU 传导链的 Host IO 入口应是 R100~R400 的并集，而非仅 R100。
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
    # 第八轮 P1-1/P2-1：validation_errors 与 profile_validation_errors 提升到顶层。
    if verr:
        result["validation_errors"] = verr
    if pverr:
        result["profile_validation_errors"] = pverr
    return result


def _is_confirmed_host_issue(finding: dict) -> bool:
    """该根因桶是否构成已确认的 Host IO 压力（用于 R500 传导入口）。

    Review P1-2：只接受每条规则的**明确确认字段**，不接受通用 medium/medium 兜底——
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
    """从 PID→设备映射取得目标 workload 的设备集合及身份是否均为 sysfs 精确解析。"""
    provider = _provider(snapshot, "process_io_map")
    parsed = _parsed(provider)
    if (
        _status(provider) != "ok"
        or not isinstance(parsed, dict)
        or _provider_interval(snapshot, "process_io_map") is None
    ):
        return set(), False
    target = snapshot.get("target")
    target_path = target.get("path") if isinstance(target, dict) else None
    devices: set[str] = set()
    strong = True
    for mapping in parsed.get("mappings", []) or []:
        if not isinstance(mapping, dict):
            continue
        if not _is_data_relevant_path(mapping.get("path"), target_path):
            continue
        canonical = _canonical_dev(str(mapping.get("canonical_device") or ""))
        topology = [canonical] + [
            _canonical_dev(str(device))
            for device in (mapping.get("backing_devices") or [])
            if isinstance(device, str)
        ]
        devices.update(device for device in topology if device)
        if mapping.get("device_resolution") != "sysfs":
            strong = False
    return devices, bool(devices) and strong


def _project_host_assessments_to_target(
    snapshot: dict, assessments: list[dict]
) -> list[dict]:
    """有目标设备映射时，只允许目标 workload 的设备证据进入 R500。"""
    target_devices, target_identity_strong = _target_device_context(snapshot)
    nfs_mounts_for_target = [
        m
        for m in (snapshot.get("mounts") or [])
        if isinstance(m, dict) and str(m.get("fstype", "")).lower().startswith("nfs")
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
        if finding.get("rule_id") != "R100":
            projected.append(finding)
            continue
        item = copy.deepcopy(finding)
        assessed = [
            device
            for device in (item.get("assessed_devices") or [])
            if isinstance(device, dict) and device.get("device") in target_devices
        ]
        saturated = [
            device
            for device in (item.get("saturated_devices") or [])
            if isinstance(device, dict) and device.get("device") in target_devices
        ]
        item["assessed_devices"] = assessed
        item["saturated_devices"] = saturated
        item["evidence_window_valid"] = bool(
            item.get("evidence_window_valid") and target_identity_strong and assessed
        )
        if saturated:
            item["confidence"] = (
                "high"
                if any(device.get("level") == "sustained" for device in saturated)
                else "medium"
            )
            item["severity"] = "high" if item["confidence"] == "high" else "medium"
        elif assessed:
            original_confidence = (
                str(finding.get("confidence"))
                if finding.get("confidence") in _ORDER
                else "none"
            )
            item["confidence"] = original_confidence
            item["severity"] = "info"
            if original_confidence == "high":
                item["summary"] = "目标 workload 映射设备在有效窗口内未检测到 IO 饱和。"
            else:
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


def analyze_r500_with_host(
    snapshot: dict, profile: dict, host_findings: list[dict] | bool
) -> dict:
    """R500 的实际实现，接收已判定的 Host 根因集合（Review P1-5）。

    host_findings：R100~R400 的完整 finding 列表；函数内部只接受严格确认项。
    旧 bool 不携带可审计证据，按 unknown 处理，不得升级传导。

    设备侧传导链的置信度阶梯（Review P1-7：确定性 analyzer 不重建 profiler 时间线，
    只读取已采集指标 + agent 交叉验证后传入的 overlap 证据）：
      - high：Host IO 异常 + device Free 高 + (同窗重叠被观测 OR 对照实验改善)。
      - medium：Host IO 异常 + device Free 高，但无同窗/对照证据（封顶 medium，不称"已传导"）。
      - info（降级）：Host IO 异常但 NPU 不空泡（被计算掩盖）。
    """
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
    # bool 无 provenance，不能作为 Host 根因证据。
    if isinstance(host_findings, bool):
        assessments: list[dict] = []
    else:
        assessments = [f for f in host_findings if isinstance(f, dict)]
    # 直接 API 调用者可能只传一个手写 finding。若其声称 high 却缺严格确认字段，
    # 从 snapshot 重新计算同规则，防止伪造/旧结构绕过 provenance 契约。
    if any(
        f.get("confidence") == "high" and not _is_confirmed_host_issue(f)
        for f in assessments
    ):
        actual_r100 = analyze_r100(snapshot)
        actual_r200 = analyze_r200(snapshot)
        actual_r300 = analyze_r300(snapshot)
        actual_r400 = analyze_r400(snapshot, actual_r100)
        actual_by_rule = {
            item["rule_id"]: item
            for item in (actual_r100, actual_r200, actual_r300, actual_r400)
        }
        assessments = [
            f
            if _is_confirmed_host_issue(f)
            else actual_by_rule.get(str(f.get("rule_id")), f)
            for f in assessments
        ]
    assessments = _project_host_assessments_to_target(snapshot, assessments)
    confirmed_hosts = [f for f in assessments if _is_confirmed_host_issue(f)]
    has_host_io_issue = bool(confirmed_hosts)
    host_ruled_out = _host_io_ruled_out(assessments)
    host_rules = [f.get("rule_id", "") for f in confirmed_hosts]

    mte2 = profile.get("mte2_ratio")
    device_free_pct = profile.get("device_free_percent")
    # agent 交叉验证后传入的传导证据（确定性 analyzer 自身无法产生）
    conduction = profile.get("conduction_evidence") or {}
    overlap_observed = bool(
        conduction.get("io_npu_overlap_observed")
        and _profile_window_matches_snapshot(snapshot, profile)
    )
    experiment = conduction.get("controlled_experiment") or {}
    experiment_improved = experiment.get("result") == "improved"

    # 反例：高 mte2 + Host IO 正常 → 转交计算分析（P1 关键修复）
    if mte2 is not None and mte2 >= 0.3 and not has_host_io_issue:
        if host_ruled_out:
            confidence = "high"
            host_statement = "有效窗口内未发现设备级 Host IO 压力"
            missing: list[str] = []
        else:
            confidence = "low"
            host_statement = "Host IO 证据不足，尚不能确认正常或异常"
            missing = [
                "R100 有效设备窗口（iostat 或递增 diskstats）",
                "必要时补充 R200/R300/R400 证据",
            ]
        finding.update(
            confidence=confidence,
            severity="info",
            summary=(
                f"mte2_ratio={mte2} 偏高；{host_statement}。MTE2 是 AI Core 内部数据搬运，"
                f"不代表 Host 存储供给不足，应转交 ascend-computation-analysis 分析算子数据搬运。"
            ),
            handoff="ascend-computation-analysis",
            evidence_fields=["profile.mte2_ratio"],
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

    # Review P1-2：无任何已确认 Host IO 根因时，R500 不得输出 severity>=medium 的正向传导 finding。
    # device Free 高更可能来自 CPU 预处理/调度/通信/同步，应转交对应 skill，而非推荐 IO 缓存。
    if not has_host_io_issue:
        if host_ruled_out:
            summary = (
                f"有效窗口内未发现设备级 Host IO 压力，device Free={device_free_pct}% 的空泡"
                f"更可能来自 CPU 预处理/调度/通信/同步，转交对应 skill。"
            )
            missing = []
            confidence = "high"
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
        finding.update(
            confidence=confidence,
            severity="info",
            summary=summary,
            evidence_fields=["profile.device_free_percent"],
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
        # 有同窗重叠或对照实验证据 → high；否则封顶 medium（不声称"已传导"）。
        if overlap_observed or experiment_improved:
            proof = "同窗相关性已被观测" if overlap_observed else "对照实验已改善"
            finding.update(
                confidence="high",
                severity="high",
                summary=(
                    f"{host_desc}，device Free={device_free_pct}%，且{proof}，"
                    f"IO 压力已传导到设备侧空泡（传导链成立）。"
                ),
                evidence_fields=[
                    "profile.device_free_percent",
                    "profile.conduction_evidence",
                ],
            )
        else:
            finding.update(
                confidence="medium",
                severity="medium",
                summary=(
                    f"{host_desc}，device Free={device_free_pct}%，IO 压力可能传导到设备侧空泡，"
                    f"但缺同窗相关性/对照实验证据，置信度封顶 medium（不声称已传导）。"
                ),
                evidence_fields=["profile.device_free_percent"],
                missing_evidence=[
                    "profile.conduction_evidence.io_npu_overlap_observed（Host IO 异常区间与 device Free/step 空泡的同窗重叠）",
                    "profile.conduction_evidence.controlled_experiment（本地缓存/降并发后空泡是否同步下降）",
                ],
            )
    elif device_free_pct < 5 and has_host_io_issue:
        finding.update(
            confidence="high",
            severity="info",
            summary=(
                f"{host_desc}，但 device Free={device_free_pct}%（设备侧不空泡），"
                f"IO 压力被计算掩盖，当前不是关键瓶颈（R500 降级）。"
            ),
            evidence_fields=["profile.device_free_percent"],
            priority_downgrade=True,
        )
    else:
        finding.update(
            confidence="medium",
            summary=f"{host_desc}，device Free={device_free_pct}%，需结合具体空泡段判断。",
        )
    return finding


def load_snapshot(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"错误: Snapshot JSON 解析失败: {args.snapshot}: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        # 第八轮 P2-2：目录路径 / 权限 / 其他 OS 错误 → 稳定非零，不泄露堆栈。
        print(f"错误: 无法读取 Snapshot {args.snapshot}: {e}", file=sys.stderr)
        return 1

    profile = None
    if args.profile:
        try:
            with open(args.profile, encoding="utf-8") as f:
                profile = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"警告: profile 解析失败，忽略: {e}", file=sys.stderr)

    # 第八轮 P1-1：所有 mode 共用 validate_analysis_request（schema/collected_at/顶层 fatal）。
    snapshot, profile, verr, pverr, fatal = validate_analysis_request(snapshot, profile)
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
        # 单规则结果携带 validation_errors（第八轮 P1-1B）
        if isinstance(result, dict):
            if verr:
                result["validation_errors"] = verr
            if pverr:
                result["profile_validation_errors"] = pverr

    # 第七轮 P1-1C/P2-1：顶层 error/unsupported schema 返回非零；输出写入失败也非零。
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
