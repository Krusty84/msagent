#!/usr/bin/env python3
"""Validate PTA profiler output for parsing and trace visualization readiness."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

TRACE_NAME = "trace_view.json"
CSV_NAMES = {"op_statistic.csv", "kernel_details.csv"}
DB_PREFIX = "ascend_pytorch_profiler_"


def validate_trace(path: Path) -> tuple[bool, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"invalid JSON: {exc}"
    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict):
        events = payload.get("traceEvents")
    else:
        events = None
    if not isinstance(events, list) or not events:
        return False, "traceEvents must be a non-empty list"

    def is_number(value: object) -> bool:
        try:
            float(value)
        except (TypeError, ValueError):
            return False
        return True

    duration_events = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("ph") == "X"
        and isinstance(event.get("name"), str)
        and bool(event["name"].strip())
        and "pid" in event
        and "tid" in event
        and is_number(event.get("ts"))
        and is_number(event.get("dur"))
        and float(event["dur"]) >= 0
    ]
    if not duration_events:
        return (
            False,
            "trace contains no complete-duration event with name/pid/tid/timestamp",
        )
    return True, f"{len(events)} trace events, {len(duration_events)} duration events"


def validate_csv(path: Path) -> tuple[bool, str]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.reader(file)
            header = next(reader, None)
            first_row = next(reader, None)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return False, f"invalid CSV: {exc}"
    if not header or not any(column.strip() for column in header):
        return False, "CSV header is empty"
    if not first_row or not any(column.strip() for column in first_row):
        return False, "CSV has no data rows"
    return True, f"{len(header)} columns with data"


def validate_db(path: Path) -> tuple[bool, str]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
    except (OSError, sqlite3.DatabaseError) as exc:
        return False, f"invalid SQLite DB: {exc}"
    if not integrity or integrity[0] != "ok":
        return False, f"SQLite quick_check failed: {integrity}"
    if not tables:
        return False, "SQLite DB contains no profiler tables"
    return True, f"SQLite quick_check ok, {len(tables)} tables"


def build_report(root: Path, expectation: str) -> dict[str, object]:
    traces = sorted(root.rglob(TRACE_NAME))
    csv_files = sorted(path for path in root.rglob("*.csv") if path.name in CSV_NAMES)
    db_files = sorted(
        path for path in root.rglob("*.db") if path.name.startswith(DB_PREFIX)
    )
    checks: list[dict[str, object]] = []
    for kind, paths, validator in (
        ("trace", traces, validate_trace),
        ("csv", csv_files, validate_csv),
        ("db", db_files, validate_db),
    ):
        for path in paths:
            passed, detail = validator(path)
            checks.append(
                {
                    "kind": kind,
                    "path": str(path.relative_to(root)),
                    "passed": passed,
                    "detail": detail,
                }
            )

    trace_ready = bool(traces) and any(
        check["kind"] == "trace" and check["passed"] for check in checks
    )
    parsed_output_ready = bool(csv_files) and all(
        check["passed"] for check in checks if check["kind"] == "csv"
    )
    text_ready = trace_ready and parsed_output_ready
    db_ready = bool(db_files) and all(
        check["passed"] for check in checks if check["kind"] == "db"
    )
    expected_ready = {
        "text": text_ready,
        "db": db_ready,
        "either": text_ready or db_ready,
    }[expectation]
    all_valid = bool(checks) and all(bool(check["passed"]) for check in checks)
    return {
        "root": str(root),
        "expect": expectation,
        "text_ready": text_ready,
        "db_ready": db_ready,
        "parsed_output_ready": parsed_output_ready,
        "visualizable": trace_ready,
        "passed": expected_ready and all_valid,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--expect", choices=("text", "db", "either"), default="either")
    args = parser.parse_args()
    if not args.output_dir.is_dir():
        parser.error(f"output directory does not exist: {args.output_dir}")
    report = build_report(args.output_dir.resolve(), args.expect)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
