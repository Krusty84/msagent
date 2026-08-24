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
"""Block-device, diskstats, iostat, and read-ahead collection."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any


from .common import (
    DiskStat,
    DiskStatSample,
    ProviderResult,
    STATUS_CMD_FAILED,
    STATUS_EMPTY,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_PARSE_FAILED,
    STATUS_PERMISSION,
    _READAHEAD_PROBE_BUDGET_SECONDS,
    _STATIC_PROBE_TIMEOUT_SECONDS,
    _classify_cmd,
    _finite_mean,
    _finite_weighted_mean,
    _have_cmd,
    _is_block_device_candidate,
    _now_iso,
    _read_file,
    _run,
    _run_with_env,
    _strict_finite_float,
    parse_interval,
)


def _is_real_block_device(name: str) -> bool:
    """判断是否为物理/逻辑块设备主设备（排除分区）。

    通过 /sys/class/block/<name>/partition 判断：存在该文件且内容非 0 即分区。
    兼容无 /sys 的环境（如容器内只读视图缺失），此时只对已知分区命名
    做保守过滤。未知设备名按主设备保留，避免漏掉 mmcblk、rbd、nbd 等合法设备。
    """
    if not _is_block_device_candidate(name):
        return False
    part_file = f"/sys/class/block/{name}/partition"
    content, _err, status = _read_file(part_file)
    if status == 0:
        # partition 文件存在且编号 >0 时是分区。
        try:
            return int(content.strip()) == 0
        except ValueError:
            return True
    # /sys 不可见时仅识别无歧义的常见分区格式。rbd0/nbd0/mmcblk0
    # 的数字属于主设备名，不能按通用“末尾数字”规则过滤。
    partition_patterns = (
        r"^(?:nvme\d+n\d+|mmcblk\d+|rbd\d+|nbd\d+|md\d+|dm-\d+)p\d+$",
        r"^(?:sd|vd|hd|xvd)[a-z]+\d+$",
    )
    if any(re.fullmatch(pattern, name) for pattern in partition_patterns):
        return False
    return True


def _list_block_devices() -> list[str]:
    """列出物理块设备主设备名（排除分区）。"""
    content, _, status = _read_file("/proc/diskstats")
    if status != 0:
        return []
    devices = []
    for line in content.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        name = fields[2]
        if _is_real_block_device(name):
            devices.append(name)
    return sorted(set(devices))


def _parse_diskstats(content: str) -> dict[str, DiskStat]:
    """解析 /proc/diskstats 单次快照。"""
    disks: dict[str, DiskStat] = {}
    for line in content.splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        if not _is_real_block_device(name):
            continue
        disks[name] = DiskStat(
            name=name,
            reads_completed=float(f[3]),
            reads_merged=float(f[4]),
            sectors_read=float(f[5]),
            time_reading_ms=float(f[6]),
            writes_completed=float(f[7]),
            writes_merged=float(f[8]),
            sectors_written=float(f[9]),
            time_writing_ms=float(f[10]),
            io_in_progress=float(f[11]),
            time_io_ms=float(f[12]),
            weighted_time_io_ms=float(f[13]),
        )
    return disks


def collect_block_devices(
    duration: float,
) -> tuple[ProviderResult, list[DiskStatSample]]:
    """两次采样 /proc/diskstats，返回 ProviderResult 与两个 DiskStatSample。"""
    started = _now_iso()
    content0, err0, st0 = _read_file("/proc/diskstats")
    ts0 = time.time()
    if st0 != 0:
        status = {
            1: STATUS_MISSING,
            2: STATUS_PERMISSION,
        }.get(st0, STATUS_CMD_FAILED)
        return (
            ProviderResult(
                source="block_devices",
                status=status,
                started_at=started,
                ended_at=_now_iso(),
                error=err0,
            ),
            [],
        )
    disks0 = _parse_diskstats(content0)
    time.sleep(max(0.1, duration))
    content1, err1, st1 = _read_file("/proc/diskstats")
    ts1 = time.time()
    if st1 != 0:
        status = {
            1: STATUS_MISSING,
            2: STATUS_PERMISSION,
        }.get(st1, STATUS_CMD_FAILED)
        return (
            ProviderResult(
                source="block_devices",
                status=status,
                started_at=started,
                ended_at=_now_iso(),
                error=err1,
            ),
            [],
        )
    disks1 = _parse_diskstats(content1)
    samples = [
        DiskStatSample(sample_index=0, timestamp=ts0, disks=disks0),
        DiskStatSample(sample_index=1, timestamp=ts1, disks=disks1),
    ]
    return (
        ProviderResult(
            source="block_devices",
            status=STATUS_OK if disks1 else STATUS_EMPTY,
            started_at=started,
            ended_at=_now_iso(),
            error="",
        ),
        samples,
    )


def collect_iostat(duration: float) -> ProviderResult:
    """iostat 动态采样。优先 JSON 输出（sysstat 12.7+），失败降级到按表头解析的文本路径。

    - JSON 路径：`iostat -o JSON -x -k 1 <count>`（最稳健，不依赖列序）。
    - 文本降级：`iostat -xk 1 <count>`，按表头行建立列名→索引映射，兼容现代/旧列序。
    若 JSON 解析得到空 disks，则继续尝试文本路径（sysstat 12.6.x JSON 字段别名兼容）。
    全窗口聚合（-y 丢弃 boot 首报后保留全部真实区间报告），解析为结构化 per-disk 指标。固定 LC_ALL=C。
    """
    started = _now_iso()
    if not _have_cmd("iostat"):
        return ProviderResult(
            source="iostat",
            status=STATUS_MISSING,
            started_at=started,
            ended_at=_now_iso(),
        )
    env = {**os.environ, "LC_ALL": "C"}
    parsed: dict[str, Any] | None = None
    source_fmt = ""
    raw_out = ""
    exit_code = 0
    err = ""
    drop_boot = False  # -y 成功时为 False（无 boot 报告）；-y 不支持退回无 -y 时为 True
    winning_window: tuple[str, str] | None = None
    attempts: list[dict[str, Any]] = []

    # duration 必须是有限正整数秒；非法时标记 command_failed。
    eff = parse_interval(duration)
    if eff is None:
        return ProviderResult(
            source="iostat",
            status=STATUS_CMD_FAILED,
            started_at=started,
            ended_at=_now_iso(),
            error=f"invalid duration {duration!r} (need finite int seconds >=1)",
        )

    def _count(with_y: bool) -> int:
        return eff if with_y else eff + 1

    def _y_unsupported(ec: int, err: str) -> bool:
        """仅当 -y 因"选项不被支持"失败时返回 True（此时无 -y 的首份才是 boot 累计）。
        超时或其他瞬时失败不得误判为不支持。
        注意：不使用 'usage:' 关键词——瞬时错误的 stderr 也可能含 usage 行，
        会误判并静默丢弃 boot 报告。
        """
        if ec == 0:
            return False
        low = (err or "").lower()
        return any(k in low for k in ("invalid option", "unrecognized", "illegal option"))

    def _json_unsupported(ec: int, err: str) -> bool:
        """A text fallback is safe only when JSON was rejected before sampling."""
        return _y_unsupported(ec, err)

    def _timed_run(cmd: list[str]) -> tuple[int, str, str, str, str]:
        attempt_started = _now_iso()
        ec, out, attempt_error = _run_with_env(cmd, env=env, timeout=duration + 10)
        attempt_ended = _now_iso()
        attempts.append(
            {
                "exit_code": ec,
                "out": out,
                "err": attempt_error,
                "started_at": attempt_started,
                "ended_at": attempt_ended,
                "status": _classify_cmd("iostat", ec, out, attempt_error),
            }
        )
        return ec, out, attempt_error, attempt_started, attempt_ended

    def _mark_parse_result(candidate: dict[str, Any] | None) -> None:
        """Bind parser outcome to the command attempt that produced the output."""
        attempt = attempts[-1]
        if attempt["status"] != STATUS_OK:
            return
        if candidate is None:
            attempt["status"] = STATUS_PARSE_FAILED
        elif candidate.get("disks"):
            attempt["status"] = STATUS_OK
        else:
            # The schema/header was recognized but contained no supported real device.
            attempt["status"] = STATUS_EMPTY

    y_failed_ec = 0
    y_failed_err = ""

    # 1) JSON + -y
    ec_j, out_j, err_j, start_j, end_j = _timed_run(["iostat", "-y", "-o", "JSON", "-x", "-k", "1", str(_count(True))])
    candidate: dict[str, Any] | None = None
    if ec_j == 0 and out_j.strip().startswith("{"):
        candidate = _parse_iostat_json(out_j, drop_boot=False)
    _mark_parse_result(candidate)
    if candidate is not None and candidate.get("disks"):
        parsed = candidate
        source_fmt, raw_out, exit_code, err = "json", out_j, ec_j, err_j
        winning_window = (start_j, end_j)
    else:
        y_failed_ec, y_failed_err = ec_j, err_j
    # 2) Text fallback only after an immediate unsupported-option rejection. A
    # successful full JSON sampling window must never be followed by another full
    # text window, otherwise providers no longer describe the same workload period.
    if (
        parsed is None
        and ec_j != 0
        and any(
            token in f"{out_j}\n{err_j}".lower()
            for token in ("invalid option", "unrecognized", "illegal option", "usage:")
        )
    ):
        ec_t, out_t, err_t, start_t, end_t = _timed_run(["iostat", "-y", "-xk", "1", str(_count(True))])
        candidate = None
        if ec_t == 0 and out_t.strip():
            candidate = _parse_iostat_text(out_t, drop_boot=False)
        _mark_parse_result(candidate)
        if candidate is not None and candidate.get("disks"):
            parsed = candidate
            source_fmt, raw_out, exit_code, err = "text", out_t, ec_t, err_t
            winning_window = (start_t, end_t)
        else:
            parsed = None
            y_failed_ec, y_failed_err = ec_t, err_t
    # 3) -y 全部失败：仅在"选项不被支持"时退回无 -y（drop_boot=True，首份确为 boot）。
    #    超时/瞬时失败不得静默退回并误标 boot-dropped——如实报告 command_failed。
    if parsed is None and _y_unsupported(y_failed_ec, y_failed_err):
        ec_j2, out_j2, err_j2, start_j2, end_j2 = _timed_run(
            ["iostat", "-o", "JSON", "-x", "-k", "1", str(_count(False))]
        )
        candidate = None
        if ec_j2 == 0 and out_j2.strip().startswith("{"):
            candidate = _parse_iostat_json(out_j2, drop_boot=True)
        _mark_parse_result(candidate)
        if candidate is not None and candidate.get("disks"):
            parsed = candidate
            source_fmt, raw_out, exit_code, err = "json", out_j2, ec_j2, err_j2
            drop_boot = True
            winning_window = (start_j2, end_j2)
        if parsed is None and _json_unsupported(ec_j2, err_j2):
            ec_t2, out_t2, err_t2, start_t2, end_t2 = _timed_run(["iostat", "-xk", "1", str(_count(False))])
            candidate = None
            if ec_t2 == 0 and out_t2.strip():
                candidate = _parse_iostat_text(out_t2, drop_boot=True)
            _mark_parse_result(candidate)
            if candidate is not None and candidate.get("disks"):
                parsed = candidate
                source_fmt, drop_boot = "text", True
                exit_code, err, raw_out = ec_t2, err_t2, out_t2
                winning_window = (start_t2, end_t2)
    if parsed is None:
        # Prefer the last non-empty output so parser failures retain their diagnostic
        # payload and exact attempt window. If every attempt was empty, preserve the
        # final command result instead.
        diagnostic = next(
            (attempt for attempt in reversed(attempts) if attempt["out"].strip()),
            attempts[-1],
        )
        exit_code = diagnostic["exit_code"]
        raw_out = diagnostic["out"]
        err = diagnostic["err"]
        status = diagnostic["status"]
        winning_window = (diagnostic["started_at"], diagnostic["ended_at"])
    else:
        # 有效输出但无受支持设备时标记 empty，避免与 R000 矛盾。
        status = STATUS_OK if parsed.get("disks") else STATUS_EMPTY
    parsed_out = parsed if parsed is not None else None
    if parsed_out is not None:
        parsed_out["source_format"] = source_fmt
        # 记录 boot 报告处理方式，便于审计窗口对齐。
        parsed_out["boot_report"] = "dropped" if drop_boot else "excluded_by_y"
        # 标注设备类型（rotational：HDD=1, SSD/NVMe=0），供 analyzer 按介质选阈值。
        disks = parsed_out.get("disks") or {}
        for name, metrics in disks.items():
            if isinstance(metrics, dict):
                metrics["device_type"] = _device_type(name)
    return ProviderResult(
        source="iostat",
        status=status,
        started_at=winning_window[0] if winning_window else started,
        ended_at=winning_window[1] if winning_window else _now_iso(),
        exit_code=exit_code,
        stderr=err,
        raw=raw_out,
        parsed=parsed_out,
    )


def _device_type(name: str) -> str:
    """读 /sys/block/<dev>/queue/rotational 判断介质类型。

    返回 'hdd' / 'ssd' / 'unknown'（NVMe/SSD/未知统一归 ssd 阈值档；unknown 用保守阈值）。
    nvme* 默认归 ssd（NVMe 几乎不可能 rotational）。
    """
    if name.startswith("nvme"):
        return "ssd"
    content, _, st = _read_file(f"/sys/block/{name}/queue/rotational")
    if st == 0:
        return "hdd" if content.strip() == "1" else "ssd"
    return "unknown"


def _parse_iostat_json(text: str, drop_boot: bool = False) -> dict[str, Any] | None:
    """解析 iostat -o JSON 输出，收集 statistics 块并按设备聚合。

    JSON schema（sysstat）：{sysstat: {hosts: [{statistics: [{disk: [{disk_name / disk_device, ...}]}]}]}}
    兼容字段别名：设备名 `disk_name` / `disk_device`，利用率 `%util` / `util`，队列深度
    `aqu-sz` / `avgqu-sz`。drop_boot：collector 用了 -y 时为 False（全部为真实区间，全保留）；
    -y 不支持退回无 -y 时为 True（首份是开机累计，丢弃）。禁止按报告数量猜测，由 collector
    显式传入。
    返回 {disks: {name: 聚合指标}, reports: N}。聚合见 _aggregate_iostat_samples。
    """
    # 非字符串输入会让 json.loads 抛 TypeError，因此提前返回。
    if not isinstance(text, str):
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    # 拒绝会让后续 doc.get 失败的 null 或 list 顶层值。
    if not isinstance(doc, dict):
        return None
    # 逐层检查类型，避免畸形嵌套 JSON 导致异常。
    sysstat = doc.get("sysstat")
    if not isinstance(sysstat, dict):
        return None
    hosts = sysstat.get("hosts") or []
    if not isinstance(hosts, list) or not hosts:
        return None
    h0 = hosts[0]
    if not isinstance(h0, dict):
        return None
    stats_blocks = h0.get("statistics") or []
    if not isinstance(stats_blocks, list) or not stats_blocks:
        return None
    if drop_boot and len(stats_blocks) >= 2:
        stats_blocks = stats_blocks[1:]
    per_disk_samples: dict[str, list[dict[str, float]]] = {}
    saw_real_device = False
    for block in stats_blocks:
        if not isinstance(block, dict):
            return None
        if "disk" not in block:
            return None
        disks = block["disk"]
        if not isinstance(disks, list):
            return None
        for d in disks:
            if not isinstance(d, dict):
                return None
            name = d.get("disk_name") or d.get("disk_device")
            if not isinstance(name, str) or not name:
                return None
            if not _is_real_block_device(name):
                continue
            saw_real_device = True
            metrics = _iostat_disk_from_json(d)
            if metrics is None or not metrics:
                # Do not silently drop a malformed or unsupported real device from
                # an otherwise valid mixed-device report. It may be the target disk.
                return None
            if metrics:
                per_disk_samples.setdefault(name, []).append(metrics)
    if saw_real_device and not per_disk_samples:
        # The JSON envelope is valid, but its disk metric schema is unsupported.
        return None
    disks: dict[str, dict[str, Any]] = {}
    for name, samples in per_disk_samples.items():
        aggregated = _aggregate_iostat_samples(samples)
        if aggregated is None:
            return None
        disks[name] = aggregated
    return {"disks": disks, "reports": len(stats_blocks)}


def _iostat_disk_from_json(d: dict[str, Any]) -> dict[str, float] | None:
    """Extract one JSON disk; return None when a present known field is invalid."""
    out: dict[str, float] = {}
    mapping = {
        "r_per_s": "r/s",
        "w_per_s": "w/s",
        "rkB_per_s": "rkB/s",
        "wkB_per_s": "wkB/s",
        "rrqm_per_s": "rrqm/s",
        "wrqm_per_s": "wrqm/s",
        "avgqu_sz": ("aqu-sz", "avgqu-sz"),
        "await": "await",
        "r_await_ms": "r_await",
        "w_await_ms": "w_await",
        "util_percent": ("%util", "util"),
        "avgrq_sz": "rareq-sz",
    }
    for ours, theirs in mapping.items():
        if isinstance(theirs, tuple):
            candidates = theirs
        else:
            candidates = (theirs,)
        parsed_candidates: list[float] = []
        for key in candidates:
            if key not in d:
                continue
            parsed = _strict_finite_float(d[key])
            if parsed is None:
                return None
            parsed_candidates.append(parsed)
        if parsed_candidates:
            out[ours] = parsed_candidates[0]
    return out


_IOSTAT_AVG_FIELDS = (
    "r_per_s",
    "w_per_s",
    "rkB_per_s",
    "wkB_per_s",
    "rrqm_per_s",
    "wrqm_per_s",
    "avgqu_sz",
    "avgrq_sz",
)


def _aggregate_iostat_samples(
    samples: list[dict[str, float]],
) -> dict[str, Any] | None:
    """把同一设备的多次采样聚合为窗口统计。

    - 速率/队列/IO 大小：仅对实际含该字段的样本取算术平均，不把缺失字段补零。
    - r_await/w_await：按 r/s、w/s 加权平均（避免小 IO 样本被等权放大）。
    - util_percent：按实际 util 样本平均；另给 util_max / util_p95 /
      util_sample_count 供 analyzer 判"持续饱和"，sample_count 保留总报告数。
    """
    n = len(samples)
    out: dict[str, Any] = {"sample_count": n}

    def _col(key: str) -> list[float]:
        return [float(s[key]) for s in samples if key in s]

    for key in _IOSTAT_AVG_FIELDS:
        vals = _col(key)
        if vals:
            mean = _finite_mean(vals)
            if mean is None:
                return None
            out[key] = round(mean, 4)
            out[f"{key}_sample_count"] = len(vals)

    # 加权 await（按 r/s / w/s 加权；权重为 0 时退化为算术平均）
    for await_key, weight_key in (("r_await_ms", "r_per_s"), ("w_await_ms", "w_per_s")):
        values = [
            (float(sample[await_key]), float(sample.get(weight_key, 0) or 0))
            for sample in samples
            if await_key in sample
        ]
        if values:
            mean = _finite_weighted_mean(
                [value for value, _ in values],
                [max(0.0, weight) for _, weight in values],
            )
            if mean is None:
                return None
            out[await_key] = round(mean, 4)
            out[f"{await_key}_sample_count"] = len(values)
    # 综合 await：按总 IOPS (r/s+w/s) 加权。任一样本缺权重或总权重为 0
    # 时退化为算术平均，避免静默丢弃该样本。
    await_samples = [sample for sample in samples if "await" in sample]
    if await_samples:
        raw_await = [float(sample["await"]) for sample in await_samples]
        weights_complete = all("r_per_s" in sample or "w_per_s" in sample for sample in await_samples)
        weights = [
            max(0.0, float(sample.get("r_per_s", 0) or 0)) + max(0.0, float(sample.get("w_per_s", 0) or 0))
            for sample in await_samples
        ]
        aggregate_await = _finite_weighted_mean(raw_await, weights) if weights_complete else _finite_mean(raw_await)
        if aggregate_await is None:
            return None
        out["await"] = round(aggregate_await, 4)
        out["await_sample_count"] = len(await_samples)

    util_samples = [sample for sample in samples if "util_percent" in sample]
    valid_util_samples = [sample for sample in util_samples if 0 <= float(sample["util_percent"]) <= 100.5]
    invalid_util_count = len(util_samples) - len(valid_util_samples)
    if invalid_util_count and valid_util_samples and invalid_util_count / len(util_samples) <= 0.01:
        # Long sysstat windows can contain an isolated impossible %util sample
        # while all surrounding reports are valid. Do not let <=1% bad samples
        # invalidate every device; preserve the count for evidence review.
        accepted_util_samples = valid_util_samples
        out["util_invalid_sample_count"] = invalid_util_count
    else:
        accepted_util_samples = util_samples
    util = sorted(float(sample["util_percent"]) for sample in accepted_util_samples)
    # sysstat can report a small rounding overshoot while a device is fully
    # busy (for example 100.10%). Clamp only that narrow range; larger invalid
    # values remain visible for the analyzer's strict validation.
    if util and util[0] >= 0 and util[-1] <= 100.5:
        util = [min(value, 100.0) for value in util]
    if util:
        util_mean = _finite_mean(util)
        if util_mean is None:
            return None
        out["util_percent"] = round(util_mean, 4)
        out["util_max"] = round(util[-1], 4)
        out["util_sample_count"] = len(util)
        # p95：线性插值（n=1 时等于该值）
        rank = 0.95 * (len(util) - 1)
        lo = int(rank)
        hi = min(lo + 1, len(util) - 1)
        frac = rank - lo
        util_p95 = _finite_weighted_mean([util[lo], util[hi]], [1.0 - frac, frac])
        if util_p95 is None:
            return None
        out["util_p95"] = round(util_p95, 4)
    # High-confidence R100 evidence requires util and the supporting queue/await
    # metric in the same reports; independent sparse columns cannot be joined.
    for field in ("avgqu_sz", "await", "r_await_ms", "w_await_ms"):
        if field in out and util:
            out[f"{field}_with_util_sample_count"] = sum(1 for sample in accepted_util_samples if field in sample)
    return out


def _parse_iostat_text(text: str, drop_boot: bool = False) -> dict[str, Any] | None:
    """解析 iostat -xk 文本输出，按表头建立列名→索引并按设备聚合。

    drop_boot：collector 用了 -y 时为 False（全保留）；-y 不支持时为 True（丢弃首份 boot 累计）。
    禁止按报告数量猜测，由 collector 显式传入。
    返回 {disks: {name: 聚合指标}, reports: N}。聚合见 _aggregate_iostat_samples。
    """
    blocks = re.split(r"\n\s*\n", text.strip())
    col_map = {
        "r_per_s": ("r/s",),
        "w_per_s": ("w/s",),
        "rkB_per_s": ("rkB/s",),
        "wkB_per_s": ("wkB/s",),
        "rrqm_per_s": ("rrqm/s",),
        "wrqm_per_s": ("wrqm/s",),
        "avgqu_sz": ("aqu-sz", "avgqu-sz"),
        "await": ("await",),
        "r_await_ms": ("r_await",),
        "w_await_ms": ("w_await",),
        "util_percent": ("%util",),
        "avgrq_sz": ("rareq-sz", "avgrq-sz"),
    }
    # 先按报告收集每设备的采样（保留报告顺序），再决定是否丢弃首报
    report_samples: list[dict[str, dict[str, float]]] = []
    saw_supported_header = False
    saw_real_device = False
    for blk in blocks:
        lines = [ln for ln in blk.splitlines() if ln.strip()]
        if not lines:
            continue
        if not any("Device" in ln for ln in lines):
            first = lines[0].lstrip().lower()
            if first.startswith("linux ") or first.startswith("avg-cpu:"):
                continue
            return None
        header = next((ln.split() for ln in lines if "Device" in ln), [])
        rows = [ln for ln in lines if "Device" not in ln]
        if not header:
            continue
        idx = {col: header.index(col) for col in header}
        supported_columns = {alias for aliases in col_map.values() for alias in aliases}
        if not (set(idx) & supported_columns):
            return None
        saw_supported_header = True
        per_dev: dict[str, dict[str, float]] = {}
        for row in rows:
            f = row.split()
            if len(f) < 2:
                return None
            name = f[0]
            if not _is_real_block_device(name):
                continue
            saw_real_device = True
            m: dict[str, float] = {}
            for ours, aliases in col_map.items():
                parsed_aliases: list[float] = []
                for theirs in aliases:
                    if theirs not in idx:
                        continue
                    column = idx[theirs]
                    if column >= len(f):
                        return None
                    parsed = _strict_finite_float(f[column])
                    if parsed is None:
                        return None
                    parsed_aliases.append(parsed)
                if parsed_aliases:
                    m[ours] = parsed_aliases[0]
            if m:
                per_dev[name] = m
            else:
                return None
        report_samples.append(per_dev)
    if not saw_supported_header:
        return None
    # 仅在 collector 显式 drop_boot=True（-y 不支持的兼容路径）时丢弃首份 boot 报告。
    if drop_boot and len(report_samples) >= 2:
        report_samples = report_samples[1:]
    per_disk_samples: dict[str, list[dict[str, float]]] = {}
    for rep in report_samples:
        for name, m in rep.items():
            per_disk_samples.setdefault(name, []).append(m)
    if saw_real_device and not per_disk_samples:
        # A real device row existed, but no supported numeric metric could be parsed.
        return None
    disks = {name: _aggregate_iostat_samples(samples) for name, samples in per_disk_samples.items()}
    return {"disks": disks, "reports": len(report_samples)}


def collect_readahead_scheduler(
    budget_seconds: float = _READAHEAD_PROBE_BUDGET_SECONDS,
) -> tuple[dict[str, int], dict[str, str], list[str]]:
    """readahead（blockdev --getra）与 IO 调度器（/sys/block/<dev>/queue/scheduler）。

    需 root/读权限，失败收集到 partial 列表。仅对物理主设备采集（已用 _is_real_block_device 过滤）。
    readahead 优先读 blockdev；blockdev 不可用/失败时降级读 sysfs `read_ahead_kb` 并换算为 512B sector。
    """
    readahead: dict[str, int] = {}
    scheduler: dict[str, str] = {}
    partial: list[str] = []
    devices = _list_block_devices()
    deadline = time.monotonic() + max(0.0, budget_seconds)
    processed_devices: list[str] = []

    def _read_sysfs_readahead(dev: str) -> int | None:
        content, _, st = _read_file(f"/sys/block/{dev}/queue/read_ahead_kb")
        if st != 0:
            return None
        try:
            # sysfs 单位为 KiB；对齐 schema 的 512B sector 语义。
            return int(float(content.strip())) * 2
        except ValueError:
            return None

    have_blockdev = _have_cmd("blockdev")
    for index, dev in enumerate(devices):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            partial.append(
                f"readahead probe budget {budget_seconds:g}s exhausted; skipped {len(devices) - index} device(s)"
            )
            break
        blockdev_err = ""
        if have_blockdev:
            ec, out, _ = _run(
                ["blockdev", "--getra", f"/dev/{dev}"],
                timeout=min(_STATIC_PROBE_TIMEOUT_SECONDS, max(0.1, remaining)),
            )
            if ec == 0:
                try:
                    readahead[f"/dev/{dev}"] = int(out.strip())
                    processed_devices.append(dev)
                    continue
                except ValueError:
                    blockdev_err = "parse failed"
            else:
                blockdev_err = f"timed out after {_STATIC_PROBE_TIMEOUT_SECONDS:g}s" if ec == 124 else f"exit {ec}"
        sysfs_value = _read_sysfs_readahead(dev)
        if sysfs_value is not None:
            readahead[f"/dev/{dev}"] = sysfs_value
            if blockdev_err:
                partial.append(f"readahead {dev}: blockdev {blockdev_err}; used sysfs fallback")
        else:
            if blockdev_err:
                partial.append(f"readahead {dev}: blockdev {blockdev_err}; sysfs fallback unavailable")
            else:
                partial.append(f"readahead {dev}: sysfs unavailable")
        processed_devices.append(dev)
    for index, dev in enumerate(processed_devices):
        if time.monotonic() >= deadline:
            partial.append(
                f"scheduler probe budget {budget_seconds:g}s exhausted; "
                f"skipped {len(processed_devices) - index} device(s)"
            )
            break
        sch_file = f"/sys/block/{dev}/queue/scheduler"
        content, _, st = _read_file(sch_file)
        if st == 0:
            scheduler[f"/dev/{dev}"] = content.strip()
        # 无权限/不存在则跳过（不报 partial，scheduler 仅为辅助）
    return readahead, scheduler, partial
