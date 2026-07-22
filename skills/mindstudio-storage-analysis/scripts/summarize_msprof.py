#!/usr/bin/env python3
"""Build a conservative R500 profile summary from exported msprof CSV data."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    if not path.parent.exists():
        raise OSError(f"output parent does not exist: {path.parent}")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
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


def _find_op_summary(path: Path) -> Path:
    if path.is_file():
        return path
    matches = sorted(path.rglob("op_summary_*.csv"))
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
            current_start, current_end = start, end
        elif end > current_end:
            current_end = end
    if current_end >= 0:
        merged += current_end - current_start
    return merged


def summarize(profile_path: Path, device: int | None = None) -> dict[str, Any]:
    op_summary = _find_op_summary(profile_path.resolve())
    intervals: dict[int, list[tuple[float, float]]] = defaultdict(list)
    mte2_weighted: dict[int, float] = defaultdict(float)
    mte2_weights: dict[int, float] = defaultdict(float)
    task_counts: dict[int, int] = defaultdict(int)
    required = {
        "Device_id",
        "Task Start Time(us)",
        "Task Duration(us)",
        "mte2_ratio",
    }

    with op_summary.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            missing = sorted(required - set(reader.fieldnames or []))
            raise ValueError(f"op summary is missing required columns: {missing}")
        for row_number, row in enumerate(reader, start=2):
            device_id = _integer(row, "Device_id", row_number)
            if device_id < 0:
                raise ValueError(f"row {row_number}: Device_id must be non-negative")
            start = _number(row, "Task Start Time(us)", row_number)
            duration = _number(row, "Task Duration(us)", row_number)
            ratio = _number(row, "mte2_ratio", row_number)
            if start < 0 or duration <= 0:
                raise ValueError(
                    f"row {row_number}: task start must be non-negative and duration positive"
                )
            if not 0 <= ratio <= 1:
                raise ValueError(
                    f"row {row_number}: mte2_ratio must be between 0 and 1"
                )
            intervals[device_id].append((start, start + duration))
            mte2_weighted[device_id] += ratio * duration
            mte2_weights[device_id] += duration
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
    free_percent = 100.0 * (window_us - active_us) / window_us
    mte2_ratio = mte2_weighted[device] / mte2_weights[device]

    return {
        "device_free_percent": round(free_percent, 6),
        "mte2_ratio": round(mte2_ratio, 6),
        "profile_window": {
            "start": datetime.fromtimestamp(
                window_start_us / 1_000_000, tz=timezone.utc
            ).isoformat(),
            "end": datetime.fromtimestamp(
                window_end_us / 1_000_000, tz=timezone.utc
            ).isoformat(),
            "scope": "between_first_and_last_exported_device_task",
        },
        "provenance": {
            "source": "msprof op_summary",
            "source_file": str(op_summary),
            "device_id": device,
            "task_count": task_counts[device],
            "active_time_us": round(active_us, 6),
            "window_time_us": round(window_us, 6),
            "mte2_weighting": "task_duration",
            "conduction_evidence_inferred": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize exported msprof op_summary data for R500"
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
            json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        print(f"msprof summary failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
