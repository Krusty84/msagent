#!/usr/bin/env python3
"""Validate independent PTA output sessions and their NPU analysis artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

OUTPUT_DIR_NAME = "ASCEND_PROFILER_OUTPUT"
TRACE_NAME = "trace_view.json"
CSV_NAMES = {"op_statistic.csv", "operator_details.csv", "kernel_details.csv"}
DB_PREFIX = "ascend_pytorch_profiler"
RANK_PATTERN = re.compile(r"(?:^|[_-])rank[_-]?(\d+)(?:[_-]|$)", re.IGNORECASE)
SESSION_SUFFIX = re.compile(r"_\d+_\d{14,}.*_ascend_pt$")
DB_DEVICE_SCHEMAS = {"NPU_INFO": {"id", "name"}}
DB_ACTIVITY_SCHEMAS = {
    "TASK": {"startns", "endns", "deviceid"},
    "CANN_API": {"startns", "endns", "name"},
    "COMPUTE_TASK_INFO": {"name", "globaltaskid", "tasktype"},
    "PYTORCH_API": {"startns", "endns", "name"},
}


def _is_number(value: object) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _is_npu_category(value: object) -> bool:
    category = str(value or "").strip().casefold()
    return category in {"async_npu", "npu", "cann", "acl"} or category.startswith(
        ("npu_", "cann_", "acl_")
    )


def _complete_timed_event(event: object, phases: set[str]) -> bool:
    return (
        isinstance(event, dict)
        and event.get("ph") in phases
        and isinstance(event.get("name"), str)
        and bool(event["name"].strip())
        and "pid" in event
        and "tid" in event
        and _is_number(event.get("ts"))
    )


def validate_trace(path: Path) -> tuple[bool, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"invalid JSON: {exc}"
    events = (
        payload
        if isinstance(payload, list)
        else payload.get("traceEvents")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(events, list) or not events:
        return False, "traceEvents must be a non-empty list"
    duration_events = [
        event
        for event in events
        if _complete_timed_event(event, {"X"})
        and _is_number(event.get("dur"))
        and float(event["dur"]) >= 0
    ]
    npu_events = [
        event
        for event in events
        if isinstance(event, dict) and _is_npu_category(event.get("cat"))
    ]
    if not duration_events:
        return False, "trace contains no complete duration event"
    if not npu_events:
        return False, "trace contains duration events but no NPU/CANN category evidence"
    complete_npu_events = [event for event in npu_events if event in duration_events]
    async_starts = {
        (event.get("cat"), event.get("id"))
        for event in npu_events
        if _complete_timed_event(event, {"s"}) and event.get("id") is not None
    }
    async_finishes = {
        (event.get("cat"), event.get("id"))
        for event in npu_events
        if _complete_timed_event(event, {"f"}) and event.get("id") is not None
    }
    async_pairs = async_starts & async_finishes
    if not complete_npu_events and not async_pairs:
        return (
            False,
            "NPU/CANN trace evidence has neither duration events nor matched async pairs",
        )
    categories = sorted({str(event.get("cat")) for event in npu_events})
    return True, (
        f"{len(events)} events; {len(duration_events)} duration events; "
        f"{len(npu_events)} NPU/CANN events ({len(complete_npu_events)} duration, "
        f"{len(async_pairs)} async pairs) in categories {categories}"
    )


CSV_SCHEMAS: dict[str, tuple[set[str], ...]] = {
    "kernel_details.csv": (
        {"name", "duration(us)", "device_id"},
        {"name", "duration", "device id"},
    ),
    "op_statistic.csv": ({"op type", "count"},),
    "operator_details.csv": (
        {"name", "device total duration(us)"},
        {"name", "device self duration(us)"},
    ),
}


def validate_csv(path: Path) -> tuple[bool, str]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            header = reader.fieldnames
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return False, f"invalid CSV: {exc}"
    if not header:
        return False, "CSV header is empty"
    columns = {column.strip().casefold() for column in header if column.strip()}
    schemas = CSV_SCHEMAS.get(path.name, ())
    if not any(schema <= columns for schema in schemas):
        expected = " or ".join("/".join(sorted(schema)) for schema in schemas)
        return False, f"unexpected {path.name} schema; require {expected}"
    data_rows = [
        row for row in rows if any(str(value or "").strip() for value in row.values())
    ]
    if not data_rows:
        return False, "CSV has no data rows"
    if path.name == "kernel_details.csv":
        folded = {column.strip().casefold(): column for column in header}
        name_key = folded["name"]
        duration_key = folded.get("duration(us)", folded.get("duration"))
        device_key = folded.get("device_id", folded.get("device id"))
        valid_rows = [
            row
            for row in data_rows
            if str(row.get(name_key, "")).strip()
            and duration_key is not None
            and _is_number(row.get(duration_key))
            and float(row[duration_key]) >= 0
            and device_key is not None
            and str(row.get(device_key, "")).strip().isdigit()
        ]
        if not valid_rows:
            return (
                False,
                "kernel CSV has no row with kernel name, device id, and duration",
            )
        return True, f"{len(valid_rows)} valid kernel rows; schema={sorted(columns)}"
    return True, f"{len(data_rows)} data rows; schema={sorted(columns)}"


def validate_db(path: Path) -> tuple[bool, str]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            table_columns = {
                str(row[0]): {
                    str(column[1]).casefold()
                    for column in connection.execute(
                        f'PRAGMA table_info("{str(row[0]).replace(chr(34), chr(34) * 2)}")'
                    ).fetchall()
                }
                for row in tables
            }
            by_upper = {name.upper(): name for name in table_columns}

            def matching_tables(schemas: dict[str, set[str]]) -> list[str]:
                matches = []
                for expected, required in schemas.items():
                    actual = by_upper.get(expected)
                    if actual is None or not required <= table_columns[actual]:
                        continue
                    quoted = actual.replace(chr(34), chr(34) * 2)
                    if connection.execute(
                        f'SELECT 1 FROM "{quoted}" LIMIT 1'
                    ).fetchone():
                        matches.append(actual)
                return matches

            device_tables = matching_tables(DB_DEVICE_SCHEMAS)
            activity_tables = matching_tables(DB_ACTIVITY_SCHEMAS)
    except (OSError, sqlite3.DatabaseError) as exc:
        return False, f"invalid SQLite DB: {exc}"
    if not integrity or integrity[0] != "ok":
        return False, f"SQLite quick_check failed: {integrity}"
    if not tables:
        return False, "SQLite DB contains no profiler tables"

    if not device_tables or not activity_tables:
        return False, (
            "SQLite DB lacks non-empty PTA device/activity tables with required "
            f"columns; tables={sorted(table_columns)}"
        )
    return True, (
        f"SQLite quick_check ok, {len(tables)} tables, "
        f"device={device_tables}, activity={activity_tables}"
    )


def _session_identity(output_dir: Path) -> tuple[str, int | None]:
    session_name = output_dir.parent.name
    worker = SESSION_SUFFIX.sub("", session_name)
    match = RANK_PATTERN.search(worker)
    return worker or session_name, int(match.group(1)) if match else None


def _artifact_check(
    root: Path, kind: str, path: Path, validator: Callable[[Path], tuple[bool, str]]
) -> dict[str, object]:
    passed, detail = validator(path)
    return {
        "kind": kind,
        "path": str(path.relative_to(root)),
        "passed": passed,
        "detail": detail,
    }


def _validate_session(
    root: Path, output_dir: Path, expectation: str
) -> dict[str, object]:
    traces = sorted(output_dir.glob(TRACE_NAME))
    csv_files = sorted(
        path for path in output_dir.glob("*.csv") if path.name in CSV_NAMES
    )
    db_files = sorted(
        path for path in output_dir.glob("*.db") if path.name.startswith(DB_PREFIX)
    )
    checks = [
        *[_artifact_check(root, "trace", path, validate_trace) for path in traces],
        *[_artifact_check(root, "csv", path, validate_csv) for path in csv_files],
        *[_artifact_check(root, "db", path, validate_db) for path in db_files],
    ]
    trace_ready = any(check["kind"] == "trace" and check["passed"] for check in checks)
    kernel_ready = any(
        check["kind"] == "csv"
        and Path(str(check["path"])).name == "kernel_details.csv"
        and check["passed"]
        for check in checks
    )
    db_ready = any(check["kind"] == "db" and check["passed"] for check in checks)
    text_ready = trace_ready and kernel_ready
    expected_ready = {
        "text": text_ready,
        "db": db_ready,
        "either": text_ready or db_ready,
    }[expectation]
    relevant = [
        check
        for check in checks
        if (expectation in ("text", "either") and check["kind"] in ("trace", "csv"))
        or (expectation in ("db", "either") and check["kind"] == "db")
    ]
    worker, rank = _session_identity(output_dir)
    return {
        "session": str(output_dir.relative_to(root)) or ".",
        "worker": worker,
        "rank": rank,
        "text_ready": text_ready,
        "db_ready": db_ready,
        "parsed_output_ready": kernel_ready,
        "visualizable": trace_ready,
        "passed": expected_ready and all(bool(check["passed"]) for check in relevant),
        "evidence": {
            "trace": {
                "ready": trace_ready,
                "required": expectation in ("text", "either"),
            },
            "kernel_csv": {
                "ready": kernel_ready,
                "required": expectation in ("text", "either"),
            },
            "database": {
                "ready": db_ready,
                "visualizable": False,
                "required": expectation == "db",
            },
        },
        "checks": checks,
    }


def _parse_expected_values(values: list[str] | None) -> set[str]:
    return {
        item.strip()
        for value in values or []
        for item in value.split(",")
        if item.strip()
    }


def build_report(
    root: Path,
    expectation: str,
    expected_sessions: int | None = None,
    expected_workers: set[str] | None = None,
    expected_ranks: set[int] | None = None,
) -> dict[str, object]:
    output_dirs = (
        [root]
        if root.name == OUTPUT_DIR_NAME
        else sorted(path for path in root.rglob(OUTPUT_DIR_NAME) if path.is_dir())
    )
    sessions = [_validate_session(root, path, expectation) for path in output_dirs]
    actual_workers = {str(session["worker"]) for session in sessions}
    actual_ranks = {
        int(session["rank"]) for session in sessions if session["rank"] is not None
    }
    missing_workers = sorted((expected_workers or set()) - actual_workers)
    missing_ranks = sorted((expected_ranks or set()) - actual_ranks)
    session_count_ready = (
        expected_sessions is None or len(sessions) >= expected_sessions
    )
    expectations_ready = (
        session_count_ready and not missing_workers and not missing_ranks
    )
    checks = [check for session in sessions for check in session["checks"]]
    return {
        "root": str(root),
        "expect": expectation,
        "passed": bool(sessions)
        and expectations_ready
        and all(bool(session["passed"]) for session in sessions),
        "text_ready": bool(sessions)
        and all(bool(session["text_ready"]) for session in sessions),
        "db_ready": bool(sessions)
        and all(bool(session["db_ready"]) for session in sessions),
        "parsed_output_ready": bool(sessions)
        and all(bool(session["parsed_output_ready"]) for session in sessions),
        "visualizable": bool(sessions)
        and all(bool(session["visualizable"]) for session in sessions),
        "expectations": {
            "expected_sessions": expected_sessions,
            "actual_sessions": len(sessions),
            "session_count_ready": session_count_ready,
            "expected_workers": sorted(expected_workers or set()),
            "actual_workers": sorted(actual_workers),
            "missing_workers": missing_workers,
            "expected_ranks": sorted(expected_ranks or set()),
            "actual_ranks": sorted(actual_ranks),
            "missing_ranks": missing_ranks,
            "passed": expectations_ready,
        },
        "sessions": sessions,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--expect", choices=("text", "db", "either"), default="either")
    parser.add_argument(
        "--expected-sessions", type=int, help="minimum independent output sessions"
    )
    parser.add_argument(
        "--expected-workers",
        action="append",
        metavar="NAME[,NAME...]",
        help="required worker names",
    )
    parser.add_argument(
        "--expected-ranks",
        action="append",
        metavar="RANK[,RANK...]",
        help="required rank ids",
    )
    args = parser.parse_args()
    if not args.output_dir.is_dir():
        parser.error(f"output directory does not exist: {args.output_dir}")
    if args.expected_sessions is not None and args.expected_sessions < 1:
        parser.error("--expected-sessions must be at least 1")
    expected_ranks_text = _parse_expected_values(args.expected_ranks)
    try:
        expected_ranks = {int(rank) for rank in expected_ranks_text}
    except ValueError as exc:
        parser.error(f"--expected-ranks values must be integers: {exc}")
    report = build_report(
        args.output_dir.resolve(),
        args.expect,
        expected_sessions=args.expected_sessions,
        expected_workers=_parse_expected_values(args.expected_workers),
        expected_ranks=expected_ranks,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
