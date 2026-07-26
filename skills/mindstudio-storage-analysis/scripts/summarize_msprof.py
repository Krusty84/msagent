#!/usr/bin/env python3
"""Summarize non-certifying diagnostics from exported msprof op_summary CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


_MISSING_VALUES = {"", "n/a", "na", "--", "null", "none"}
_MTE2_RATIO_COLUMN = re.compile(
    r"^(?:mte2(?:_exe)?_ratio|ai[cv]_mte2(?:_exe)?_ratio)$", re.IGNORECASE
)
_MAX_PROFILE_WALK_ENTRIES = 10_000
_MAX_OP_SUMMARY_BYTES = 128 * 1024 * 1024
_MAX_OP_SUMMARY_ROWS = 1_000_000


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    if not path.parent.exists():
        raise OSError(f"output parent does not exist: {path.parent}")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _number(row: dict[str, str], field: str, row_number: int) -> float:
    value = str(row.get(field, "")).strip()
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(
            f"row {row_number}: {field} is not numeric: {value!r}"
        ) from exc
    if not math.isfinite(number):
        raise ValueError(f"row {row_number}: {field} must be finite")
    return number


def _integer(row: dict[str, str], field: str, row_number: int) -> int:
    number = _number(row, field, row_number)
    if not number.is_integer():
        raise ValueError(f"row {row_number}: {field} must be an integer")
    return int(number)


def _optional_ratio(value: Any, field: str, row_number: int) -> float | None:
    text = str(value if value is not None else "").strip()
    if text.lower() in _MISSING_VALUES:
        return None
    try:
        ratio = float(text)
    except ValueError as exc:
        raise ValueError(
            f"row {row_number}: {field} is not numeric or N/A: {text!r}"
        ) from exc
    if not math.isfinite(ratio) or not 0 <= ratio <= 1:
        raise ValueError(f"row {row_number}: {field} must be between 0 and 1")
    return ratio


def _find_op_summary(path: Path) -> Path:
    if path.is_file():
        return path
    matches: list[Path] = []
    visited = 0
    for root, dirs, files in os.walk(path):
        dirs.sort()
        visited += len(dirs)
        if visited > _MAX_PROFILE_WALK_ENTRIES:
            raise ValueError("profile directory traversal exceeded entry budget")
        for name in sorted(files):
            visited += 1
            if visited > _MAX_PROFILE_WALK_ENTRIES:
                raise ValueError("profile directory traversal exceeded entry budget")
            if name.startswith("op_summary_") and name.endswith(".csv"):
                matches.append(Path(root) / name)
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one op_summary_*.csv below {path}, found {len(matches)}"
        )
    return matches[0]


def _merge_duration(intervals: list[tuple[float, float]]) -> float:
    merged = 0.0
    current_start = -1.0
    current_end = -1.0
    for start, end in sorted(intervals):
        if start >= current_end:
            if current_end >= 0:
                merged += current_end - current_start
                if not math.isfinite(merged):
                    raise ValueError("merged task duration overflowed")
            current_start, current_end = start, end
        elif end > current_end:
            current_end = end
    if current_end >= 0:
        merged += current_end - current_start
        if not math.isfinite(merged):
            raise ValueError("merged task duration overflowed")
    return merged


def summarize(profile_path: Path, device: int | None = None) -> dict[str, Any]:
    op_summary = _find_op_summary(profile_path.resolve())
    if op_summary.stat().st_size > _MAX_OP_SUMMARY_BYTES:
        raise ValueError("op_summary CSV exceeds byte budget")
    intervals: dict[int, list[tuple[float, float]]] = defaultdict(list)
    ratios: dict[int, dict[str, dict[str, float]]] = defaultdict(dict)
    task_counts: dict[int, int] = defaultdict(int)
    required = {
        "Device_id",
        "Task Start Time(us)",
        "Task Duration(us)",
    }

    with op_summary.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            missing = sorted(required - set(reader.fieldnames or []))
            raise ValueError(f"op summary is missing required columns: {missing}")
        ratio_columns = [
            field for field in reader.fieldnames if _MTE2_RATIO_COLUMN.fullmatch(field)
        ]
        for row_number, row in enumerate(reader, start=2):
            if row_number - 1 > _MAX_OP_SUMMARY_ROWS:
                raise ValueError("op_summary CSV exceeds row budget")
            device_id = _integer(row, "Device_id", row_number)
            if device_id < 0:
                raise ValueError(f"row {row_number}: Device_id must be non-negative")
            start = _number(row, "Task Start Time(us)", row_number)
            duration = _number(row, "Task Duration(us)", row_number)
            if start < 0 or duration <= 0:
                raise ValueError(
                    f"row {row_number}: task start must be non-negative and duration positive"
                )
            end = start + duration
            if not math.isfinite(end):
                raise ValueError(f"row {row_number}: task end time must be finite")
            intervals[device_id].append((start, end))
            for field in ratio_columns:
                ratio = _optional_ratio(row.get(field), field, row_number)
                if ratio is not None:
                    stats = ratios[device_id].setdefault(
                        field, {"count": 0.0, "sum": 0.0, "min": ratio, "max": ratio}
                    )
                    stats["count"] += 1
                    stats["sum"] += ratio
                    stats["min"] = min(stats["min"], ratio)
                    stats["max"] = max(stats["max"], ratio)
            task_counts[device_id] += 1

    available_devices = sorted(intervals)
    if not available_devices:
        raise ValueError("op summary has no task rows")
    if device is None:
        if len(available_devices) != 1:
            raise ValueError(
                f"profile contains devices {available_devices}; select one with --device"
            )
        device = available_devices[0]
    if device not in intervals:
        raise ValueError(
            f"device {device} is absent from profile; available devices: {available_devices}"
        )

    selected = intervals[device]
    window_start_us = min(start for start, _ in selected)
    window_end_us = max(end for _, end in selected)
    window_us = window_end_us - window_start_us
    active_us = _merge_duration(selected)
    if window_us <= 0 or not 0 < active_us <= window_us:
        raise ValueError("task intervals do not form a valid profiling window")
    gap_proxy_percent = 100.0 * (window_us - active_us) / window_us
    ratio_summary = {
        field: {
            "sample_count": int(stats["count"]),
            "min": round(stats["min"], 6),
            "max": round(stats["max"], 6),
            "arithmetic_mean": round(stats["sum"] / stats["count"], 6),
        }
        for field, stats in sorted(ratios[device].items())
        if stats["count"]
    }

    return {
        "diagnostic_proxies": {
            "op_summary_task_gap_proxy_percent": round(gap_proxy_percent, 6),
            "task_timestamp_extent_us": {
                "start": round(window_start_us, 6),
                "end": round(window_end_us, 6),
            },
            "mte2_ratio_by_column": ratio_summary,
        },
        "provenance": {
            "source": "msprof op_summary",
            "source_file": str(op_summary),
            "device_id": device,
            "task_count": task_counts[device],
            "merged_exported_task_duration_us": round(active_us, 6),
            "task_timestamp_extent_us": round(window_us, 6),
            "r500_certifying_metrics": [],
            "conduction_evidence_inferred": False,
            "limitations": [
                "op_summary is aggregate data and does not provide a device timeline",
                "exported task gaps are not device idle time",
                "per-task cycle ratios are not aggregated into a workload mte2_ratio",
            ],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize non-certifying diagnostics from msprof op_summary data"
    )
    parser.add_argument("profile", type=Path, help="PROF directory or op_summary CSV")
    parser.add_argument("--device", type=int)
    parser.add_argument("--output", "-o", type=Path)
    args = parser.parse_args(argv)
    if not args.profile.exists():
        parser.error(f"profile path does not exist: {args.profile}")
    if args.device is not None and args.device < 0:
        parser.error("--device must be non-negative")

    try:
        summary = summarize(args.profile, args.device)
        if args.output:
            _atomic_write_json(args.output.resolve(), summary)
        else:
            json.dump(
                summary, sys.stdout, ensure_ascii=False, indent=2, allow_nan=False
            )
            sys.stdout.write("\n")
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        print(f"msprof summary failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
