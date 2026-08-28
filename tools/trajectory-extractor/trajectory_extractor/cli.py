"""Command line entry point for the trajectory extractor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import ALL_SOURCES, ExtractionRequest, discover_threads, extract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trajectory-extractor",
        description=(
            "Extract a normalized step graph from an msagent trajectory, ready to feed to SKILL.md synthesis."
        ),
    )
    parser.add_argument(
        "-w",
        "--working-dir",
        type=Path,
        default=Path.cwd(),
        help="Working directory the msagent session ran in (default: current directory)",
    )
    parser.add_argument("-t", "--thread-id", default=None, help="Thread to extract (default: most recent)")
    parser.add_argument(
        "--list-threads",
        action="store_true",
        help="List recorded threads for the working directory and exit",
    )
    parser.add_argument(
        "-s",
        "--source",
        default=",".join(ALL_SOURCES),
        help=f"Comma-separated sources to read from {ALL_SOURCES} (default: all)",
    )
    parser.add_argument("--trace-file", type=Path, default=None, help="Path to a --trace-jsonl file")
    parser.add_argument("--home", type=Path, default=None, help="Override MSAGENT_HOME")
    parser.add_argument("-o", "--out", type=Path, default=None, help="Write JSON here instead of stdout")
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help="Skip secret and PII scrubbing (not recommended for anything leaving the machine)",
    )
    parser.add_argument("--no-subagents", action="store_true", help="Ignore subagent checkpoint namespaces")
    parser.add_argument(
        "--no-parameterize-results",
        action="store_true",
        help="Only parameterize tool arguments, leaving result previews verbatim",
    )
    parser.add_argument(
        "--min-script-occurrences",
        type=int,
        default=2,
        help="How often a command must repeat to become a scripts/ candidate (default: 2)",
    )
    parser.add_argument(
        "--result-chars",
        type=int,
        default=2000,
        help="Truncate each stored result preview to this many characters (default: 2000)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Do not print the summary to stderr")
    return parser


def _print_threads(rows: list[dict], stream) -> None:
    if not rows:
        print("no recorded threads found for this working directory", file=stream)
        return
    header = f"{'THREAD':<38} {'AGENT':<12} {'AUDIT':<6} NAMESPACES"
    print(header, file=stream)
    print("-" * len(header), file=stream)
    for row in rows:
        namespaces = ", ".join(namespace or "main" for namespace in row["namespaces"]) or "-"
        print(
            f"{row['thread_id']:<38} {row['agent'] or '-':<12} {'yes' if row['has_audit'] else 'no':<6} {namespaces}",
            file=stream,
        )


def _print_summary(document, stream) -> None:
    stats = document.stats
    print(f"thread      : {document.thread_id or '-'}", file=stream)
    print(f"agent       : {document.agent or '-'}", file=stream)
    print(f"sources     : {', '.join(document.sources) or '-'}", file=stream)
    print(
        f"steps       : {stats['steps']} ({stats['failed_steps']} failed, {stats['subagent_steps']} from subagents)",
        file=stream,
    )
    print(f"tools       : {stats['distinct_tools']} distinct", file=stream)
    print(f"phases      : {stats['phases']}", file=stream)
    print(f"parameters  : {stats['parameters']}", file=stream)
    print(f"scripts     : {stats['script_candidates']} candidate(s)", file=stream)
    print(f"recoveries  : {stats['recoveries']}", file=stream)
    for warning in document.warnings:
        print(f"warning     : {warning}", file=stream)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    sources = tuple(part.strip() for part in args.source.split(",") if part.strip())
    unknown = [source for source in sources if source not in ALL_SOURCES]
    if unknown:
        print(f"unknown source(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    request = ExtractionRequest(
        working_dir=args.working_dir,
        thread_id=args.thread_id,
        sources=sources,
        trace_file=args.trace_file,
        home=args.home,
        redact=not args.no_redact,
        include_subagents=not args.no_subagents,
        min_script_occurrences=args.min_script_occurrences,
        parameterize_results=not args.no_parameterize_results,
        result_preview_chars=args.result_chars,
    )

    if args.list_threads:
        _print_threads(discover_threads(request), sys.stdout)
        return 0

    document = extract(request)
    payload = json.dumps(document.to_json(), ensure_ascii=False, indent=2)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)

    if not args.quiet:
        _print_summary(document, sys.stderr)

    return 0 if document.steps else 1
