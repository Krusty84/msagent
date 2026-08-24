#!/usr/bin/env python3
"""Discover candidate workload PIDs and data paths using bounded, read-only /proc inspection."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
_MAX_CMDLINE_BYTES = 64 * 1024
_MAX_CMDLINE_TEXT = 1024
_DEFAULT_PROCESS_SCAN_LIMIT = 4096
_DEFAULT_CANDIDATE_LIMIT = 20
_DEFAULT_FD_LIMIT = 256
_DEFAULT_TIME_BUDGET_SECONDS = 3.0
_USER_CACHE_PREFIX = str(Path.home() / ".cache").rstrip("/") + "/"

_TRAINING_SIGNALS: tuple[tuple[str, int], ...] = (
    ("torchrun", 45),
    ("deepspeed", 45),
    ("msrun", 45),
    ("horovodrun", 40),
    ("torch.distributed", 35),
    ("mindspore", 25),
    ("pytorch", 20),
    ("tensorflow", 20),
    ("train", 25),
    ("pretrain", 25),
    ("finetune", 25),
    ("inference", 15),
    ("dataloader", 15),
)

_PATH_OPTIONS: dict[str, str] = {
    "data": "dataset",
    "data-dir": "dataset",
    "data-file": "dataset",
    "data-path": "dataset",
    "data-root": "dataset",
    "dataset": "dataset",
    "dataset-dir": "dataset",
    "dataset-path": "dataset",
    "train-data": "dataset",
    "train-dir": "dataset",
    "input": "dataset",
    "input-dir": "dataset",
    "image-dir": "dataset",
    "manifest": "dataset_manifest",
    "file-list": "dataset_manifest",
    "checkpoint": "checkpoint",
    "checkpoint-dir": "checkpoint",
    "checkpoint-path": "checkpoint",
    "ckpt": "checkpoint",
    "resume": "checkpoint",
    "model-path": "checkpoint",
    "config": "config",
    "config-file": "config",
}

_SENSITIVE_OPTION = re.compile(
    r"(?:^|[.-])(?:password|passwd|token|secret|credential|api-?key|access-?key|"
    r"access-?key-id|private-?key|auth|authorization|authentication)$",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(://)[^/@\s:]+:[^/@\s]+@")
_DATA_NAME_HINT = re.compile(
    r"(?:^|[/_.-])(data|dataset|train|shard|record|image|input|checkpoint|ckpt)(?:$|[/_.-])",
    re.IGNORECASE,
)
_SYSTEM_PATH_PREFIXES = (
    "/usr/",
    "/lib/",
    "/lib64/",
    "/etc/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/run/",
    "/opt/conda/",
    _USER_CACHE_PREFIX,
)
_REMOTE_FILESYSTEMS = {
    "nfs",
    "nfs4",
    "cifs",
    "smb3",
    "lustre",
    "gpfs",
    "beegfs",
    "ceph",
    "fuse",
    "fuse.glusterfs",
    "fuse.s3fs",
    "fuse.goofys",
}


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


def _read_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        return stream.read(limit)


def _read_text(path: Path, limit: int = _MAX_CMDLINE_BYTES) -> str:
    return _read_bytes(path, limit).decode("utf-8", errors="replace")


def _parse_cmdline(raw: bytes) -> list[str]:
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _normalized_option(token: str) -> str:
    return token.lstrip("-").split("=", 1)[0].replace("_", "-").lower()


def sanitize_cmdline(tokens: list[str]) -> str:
    """Return a bounded command line with common secret-bearing values redacted."""
    safe: list[str] = []
    redact_next = False
    for token in tokens:
        if redact_next:
            safe.append("<redacted>")
            redact_next = False
            continue
        if "=" in token:
            name, _value = token.split("=", 1)
            if _SENSITIVE_OPTION.search(_normalized_option(name)):
                safe.append(f"{name}=<redacted>")
                continue
        if token.startswith("-") and _SENSITIVE_OPTION.search(_normalized_option(token)):
            safe.append(token)
            redact_next = True
            continue
        safe.append(_URL_CREDENTIALS.sub(r"\1<redacted>@", token))
    return " ".join(shlex.quote(token) for token in safe)[:_MAX_CMDLINE_TEXT]


def _lexical_path(value: str, cwd: str | None, *, accept_plain: bool) -> str | None:
    value = value.strip().strip("\"'")
    if not value or value.startswith("-") or "://" in value:
        return None
    if value.endswith(" (deleted)"):
        value = value[: -len(" (deleted)")]
    if value.startswith("~"):
        return None
    if os.path.isabs(value):
        return os.path.normpath(value)
    if not accept_plain and not value.startswith(("./", "../")) and "/" not in value:
        return None
    if not cwd or not os.path.isabs(cwd):
        return None
    return os.path.normpath(os.path.join(cwd, value))


def extract_cmdline_paths(tokens: list[str], cwd: str | None) -> list[dict[str, Any]]:
    """Extract explicit dataset/checkpoint paths and weaker standalone path arguments."""
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(path: str | None, role: str, source: str, option: str | None) -> None:
        if not path or _is_system_path(path):
            return
        key = (path, role, source)
        if key in seen:
            return
        seen.add(key)
        results.append(
            {"path": path, "role": role, "source": source, "option": option}
        )

    index = 1  # argv[0] is the executable, not a data candidate.
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            option = _normalized_option(token)
            role = _PATH_OPTIONS.get(option)
            if role:
                if "=" in token:
                    value = token.split("=", 1)[1]
                else:
                    value = tokens[index + 1] if index + 1 < len(tokens) else ""
                    if value and not value.startswith("-"):
                        index += 1
                add(
                    _lexical_path(value, cwd, accept_plain=True),
                    role,
                    "cmdline_explicit_option",
                    option,
                )
        else:
            add(
                _lexical_path(token, cwd, accept_plain=False),
                "unknown",
                "cmdline_path_argument",
                None,
            )
        index += 1
    return results


def _is_system_path(path: str) -> bool:
    normalized = os.path.normpath(path)
    return normalized in {"/", "/usr", "/lib", "/etc", "/proc", "/sys", "/dev"} or normalized.startswith(
        _SYSTEM_PATH_PREFIXES
    )


def _paths_overlap(left: str, right: str) -> bool:
    """Return whether either absolute path contains the other."""
    try:
        left = os.path.normpath(left)
        right = os.path.normpath(right)
        common = os.path.commonpath((left, right))
    except (TypeError, ValueError):
        return False
    return common in {left, right}


def _shares_data_scope(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_paths = [
        item["path"]
        for item in left["_cmdline_paths"]
        if item["role"] != "config"
    ]
    right_paths = [
        item["path"]
        for item in right["_cmdline_paths"]
        if item["role"] != "config"
    ]
    return any(_paths_overlap(a, b) for a in left_paths for b in right_paths)


def _read_ppid(proc_dir: Path) -> int | None:
    try:
        content = _read_text(proc_dir / "stat", 16 * 1024)
    except OSError:
        return None
    closing = content.rfind(")")
    if closing < 0:
        return None
    fields = content[closing + 1 :].split()
    try:
        value = int(fields[1])  # field 3 is state; field 4 is PPID.
    except (IndexError, ValueError):
        return None
    return value if value >= 0 else None


def _read_link(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _process_score(tokens: list[str], pattern: str | None) -> tuple[int, list[str]]:
    text = " ".join(tokens).lower()
    score = 0
    reasons: list[str] = []
    if pattern and pattern.lower() in text:
        score += 60
        reasons.append("匹配用户提供的进程特征")
    matched: set[str] = set()
    for signal, weight in _TRAINING_SIGNALS:
        if signal in text and signal not in matched:
            matched.add(signal)
            score += weight
            reasons.append(f"启动命令包含训练特征 {signal}")
    executable = os.path.basename(tokens[0]).lower() if tokens else ""
    if executable.startswith("python"):
        score += 5
        reasons.append("Python 进程")
    if any(
        token.startswith("-")
        and _PATH_OPTIONS.get(_normalized_option(token)) not in {None, "config"}
        for token in tokens[1:]
    ):
        score += 45
        reasons.append("启动命令明确包含数据集或检查点参数")
    return min(score, 100), reasons


def _decode_mount_token(token: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    return re.sub(r"\\([0-7]{3})", replace, token)


def _mount_table(proc_dir: Path) -> list[dict[str, str]]:
    try:
        content = _read_text(proc_dir / "mountinfo", 4 * 1024 * 1024)
    except OSError:
        return []
    mounts: list[dict[str, str]] = []
    for line in content.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = _decode_mount_token(fields[4])
            fstype = fields[separator + 1]
            source = _decode_mount_token(fields[separator + 2])
        except (ValueError, IndexError):
            continue
        mounts.append(
            {"mount_point": mount_point, "fstype": fstype, "source": source}
        )
    return sorted(mounts, key=lambda item: len(item["mount_point"]), reverse=True)


def _mount_for_path(path: str, mounts: list[dict[str, str]]) -> dict[str, str] | None:
    normalized = os.path.normpath(path)
    for mount in mounts:
        prefix = os.path.normpath(mount["mount_point"])
        if normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/"):
            return mount
    return None


def _open_file_evidence(proc_dir: Path, limit: int, deadline: float) -> tuple[list[dict[str, str]], list[str]]:
    evidence: list[dict[str, str]] = []
    warnings: list[str] = []
    fd_dir = proc_dir / "fd"
    try:
        entries = sorted(fd_dir.iterdir(), key=lambda path: int(path.name) if path.name.isdigit() else 10**12)
    except PermissionError:
        return [], ["无权限读取进程打开文件"]
    except OSError:
        return [], []
    if len(entries) > limit:
        warnings.append(f"打开文件超过 {limit} 个，只检查前 {limit} 个")
    for entry in entries[:limit]:
        if time.monotonic() >= deadline:
            warnings.append("达到目标发现时间预算，打开文件扫描提前结束")
            break
        target = _read_link(entry)
        if not target or not target.startswith("/") or _is_system_path(target):
            continue
        if target.startswith(("/dev/", "/proc/", "/sys/")):
            continue
        parent = os.path.dirname(target.rstrip("/")) or "/"
        if _is_system_path(parent):
            continue
        evidence.append({"path": parent, "sample_file": target})
    return evidence, warnings


def _rank_paths(
    cmdline_paths: list[dict[str, Any]],
    open_files: list[dict[str, str]],
    cwd: str | None,
    mounts: list[dict[str, str]],
    path_hint: str | None,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}

    def bucket(path: str, role: str) -> dict[str, Any]:
        item = buckets.setdefault(
            path,
            {
                "path": path,
                "role": role,
                "score": 0,
                "reasons": [],
                "sources": [],
                "sample_files": [],
            },
        )
        if item["role"] == "unknown" and role != "unknown":
            item["role"] = role
        return item

    for evidence in cmdline_paths:
        item = bucket(evidence["path"], evidence["role"])
        if evidence["source"] == "cmdline_explicit_option":
            points = 85 if evidence["role"] != "config" else 30
            reason = f"启动命令通过 --{evidence['option']} 明确指定"
        else:
            points = 25
            reason = "启动命令包含该路径"
        item["score"] += points
        item["reasons"].append(reason)
        item["sources"].append(evidence["source"])

    open_counts: dict[str, int] = defaultdict(int)
    open_samples: dict[str, list[str]] = defaultdict(list)
    for evidence in open_files:
        open_counts[evidence["path"]] += 1
        if len(open_samples[evidence["path"]]) < 5:
            open_samples[evidence["path"]].append(evidence["sample_file"])
    for path, count in open_counts.items():
        item = bucket(path, "open_file_parent")
        item["score"] += 35 + min(count, 8) * 5
        item["reasons"].append(f"进程当前打开了该目录下的 {count} 个文件")
        item["sources"].append("open_files")
        item["sample_files"].extend(open_samples[path])

    if cwd and os.path.isabs(cwd) and not _is_system_path(cwd):
        item = bucket(os.path.normpath(cwd), "working_directory")
        item["score"] += 10
        item["reasons"].append("进程工作目录")
        item["sources"].append("cwd")

    normalized_hint = os.path.normpath(path_hint) if path_hint else None
    if normalized_hint and os.path.isabs(normalized_hint) and not _is_system_path(normalized_hint):
        item = bucket(normalized_hint, "path_hint")
        item["score"] += 100
        item["reasons"].append("用户提供的数据路径线索")
        item["sources"].append("path_hint")
    for item in buckets.values():
        if _DATA_NAME_HINT.search(item["path"]):
            item["score"] += 15
            item["reasons"].append("路径名称包含数据或训练特征")
        if normalized_hint and item["path"] == normalized_hint:
            item["score"] += 100
            item["reasons"].append("精确匹配用户提供的路径线索")
            item["sources"].append("path_hint")
        elif normalized_hint and item["path"].startswith(
            normalized_hint.rstrip("/") + "/"
        ):
            item["score"] += 60
            item["reasons"].append("位于用户提供的路径范围内")
            item["sources"].append("path_hint")
        elif normalized_hint and normalized_hint.startswith(
            item["path"].rstrip("/") + "/"
        ):
            item["score"] += 5
            item["reasons"].append("是用户路径线索的上级目录，仅作弱参考")
        mount = _mount_for_path(item["path"], mounts)
        if mount:
            item["mount"] = mount
            if mount["fstype"].lower() in _REMOTE_FILESYSTEMS:
                item["score"] += 10
                item["reasons"].append(f"位于网络存储 {mount['fstype']}")
        item["score"] = min(int(item["score"]), 100)
        item["reasons"] = list(dict.fromkeys(item["reasons"]))
        item["sources"] = list(dict.fromkeys(item["sources"]))
        item["sample_files"] = list(dict.fromkeys(item["sample_files"]))

    return sorted(buckets.values(), key=lambda item: (-item["score"], item["path"]))


def _recommendation(processes: list[dict[str, Any]], explicit_pid: int | None) -> dict[str, Any]:
    if not processes:
        return {
            "pid": None,
            "path": None,
            "confidence": "none",
            "requires_confirmation": True,
            "reasons": ["没有找到训练进程候选"],
        }
    top = processes[0]
    second_score = processes[1]["score"] if len(processes) > 1 else -1
    process_clear = bool(explicit_pid is not None or (top["score"] >= 50 and top["score"] - second_score >= 15))
    paths = [item for item in top["path_candidates"] if item["role"] != "config"]
    path = paths[0] if paths else None
    next_path_score = paths[1]["score"] if len(paths) > 1 else -1
    path_clear = bool(path and path["score"] >= 70 and path["score"] - next_path_score >= 15)
    confidence = "high" if process_clear and path_clear else "medium" if process_clear or path_clear else "low"
    reasons: list[str] = []
    if process_clear:
        reasons.append("训练进程候选较明确")
    else:
        reasons.append("存在多个相近的训练进程候选")
    if path_clear:
        reasons.append("数据路径有明确的用户线索、命令行或打开文件证据")
    elif path and len(paths) > 1:
        reasons.append("数据路径仍有多个候选")
    elif path:
        reasons.append("只发现工作目录等较弱的数据路径线索")
    else:
        reasons.append("尚未发现可信的数据路径")
    result: dict[str, Any] = {
        "pid": top["pid"] if process_clear else None,
        "path": path["path"] if process_clear and path_clear else None,
        "confidence": confidence,
        "requires_confirmation": not (process_clear and path_clear),
        "reasons": reasons,
    }
    if process_clear:
        command = [
            "python3",
            "scripts/collect_io_snapshot.py",
            "--duration",
            "30",
            "--pid",
            str(top["pid"]),
        ]
        if path_clear and path:
            command.extend(["--path", path["path"]])
        command.extend(["--out", "io_snapshot.json"])
        result["preview_command"] = shlex.join(command)
    return result


def discover_targets(
    *,
    proc_root: Path = Path("/proc"),
    pid: int | None = None,
    process_pattern: str | None = None,
    path_hint: str | None = None,
    process_scan_limit: int = _DEFAULT_PROCESS_SCAN_LIMIT,
    candidate_limit: int = _DEFAULT_CANDIDATE_LIMIT,
    fd_limit: int = _DEFAULT_FD_LIMIT,
    time_budget_seconds: float = _DEFAULT_TIME_BUDGET_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + time_budget_seconds
    pattern = process_pattern
    warnings: list[str] = []

    if pid is not None:
        pids = [pid]
    else:
        try:
            pids = sorted(
                int(entry.name)
                for entry in proc_root.iterdir()
                if entry.name.isdigit() and entry.is_dir()
            )
        except OSError as exc:
            raise OSError(f"cannot enumerate {proc_root}: {exc}") from exc
        if len(pids) > process_scan_limit:
            warnings.append(
                f"系统进程超过 {process_scan_limit} 个，只检查前 {process_scan_limit} 个"
            )
            pids = pids[:process_scan_limit]

    basics: list[dict[str, Any]] = []
    permission_failures = 0
    for candidate_pid in pids:
        if time.monotonic() >= deadline:
            warnings.append("达到目标发现时间预算，进程扫描提前结束")
            break
        proc_dir = proc_root / str(candidate_pid)
        try:
            tokens = _parse_cmdline(_read_bytes(proc_dir / "cmdline", _MAX_CMDLINE_BYTES))
        except PermissionError:
            permission_failures += 1
            continue
        except OSError:
            continue
        if not tokens:
            continue
        if os.path.basename(tokens[0]) == "discover_io_target.py" or any(
            "discover_io_target.py" in token for token in tokens
        ):
            continue
        score, reasons = _process_score(tokens, pattern)
        if pid is not None:
            score = max(score, 100)
            reasons.insert(0, "用户明确指定该 PID")
        if pid is None and score < 20:
            continue
        cwd = _read_link(proc_dir / "cwd")
        cmdline_paths = extract_cmdline_paths(tokens, cwd)
        hint_matches_cmdline = bool(
            path_hint
            and any(
                evidence["role"] != "config"
                and _paths_overlap(evidence["path"], path_hint)
                for evidence in cmdline_paths
            )
        )
        if hint_matches_cmdline:
            score = min(score + 30, 100)
            reasons.append("启动命令的数据路径匹配用户线索")
        basics.append(
            {
                "pid": candidate_pid,
                "ppid": _read_ppid(proc_dir),
                "score": score,
                "reasons": list(dict.fromkeys(reasons)),
                "command": os.path.basename(tokens[0]),
                "cmdline": sanitize_cmdline(tokens),
                "cwd": cwd,
                "_tokens": tokens,
                "_cmdline_paths": cmdline_paths,
                "_hint_matches_cmdline": hint_matches_cmdline,
                "_proc_dir": proc_dir,
            }
        )

    by_pid = {item["pid"]: item for item in basics}
    for basic in basics:
        parent = by_pid.get(basic["ppid"])
        if parent and (
            parent["_tokens"] == basic["_tokens"]
            or (
                parent["score"] >= 50
                and _shares_data_scope(parent, basic)
            )
        ):
            basic["score"] = max(0, basic["score"] - 20)
            basic["reasons"].append("同一数据范围的父进程已作为训练主进程候选")
    basics.sort(key=lambda item: (-item["score"], item["pid"]))
    if len(basics) > candidate_limit:
        warnings.append(f"训练进程候选超过 {candidate_limit} 个，只展开前 {candidate_limit} 个")
        basics = basics[:candidate_limit]

    processes: list[dict[str, Any]] = []
    for basic in basics:
        proc_warnings: list[str] = []
        if time.monotonic() < deadline:
            open_files, proc_warnings = _open_file_evidence(
                basic["_proc_dir"], fd_limit, deadline
            )
        else:
            open_files = []
            proc_warnings.append("未展开打开文件：目标发现时间预算已用完")
        if time.monotonic() < deadline:
            mounts = _mount_table(basic["_proc_dir"])
        else:
            mounts = []
            proc_warnings.append("未读取挂载表：目标发现时间预算已用完")
        cmdline_paths = basic["_cmdline_paths"]
        hint_matches_open_files = bool(
            path_hint
            and any(
                _paths_overlap(evidence["sample_file"], path_hint)
                for evidence in open_files
            )
        )
        scoped_path_hint = (
            path_hint
            if pid is not None
            or basic["_hint_matches_cmdline"]
            or hint_matches_open_files
            else None
        )
        path_candidates = _rank_paths(
            cmdline_paths, open_files, basic["cwd"], mounts, scoped_path_hint
        )[:20]
        processes.append(
            {
                key: value
                for key, value in basic.items()
                if key
                not in {
                    "_tokens",
                    "_cmdline_paths",
                    "_hint_matches_cmdline",
                    "_proc_dir",
                }
            }
            | {"path_candidates": path_candidates, "warnings": proc_warnings}
        )

    if permission_failures:
        warnings.append(f"有 {permission_failures} 个进程因权限不足无法检查")
    recommendation = _recommendation(processes, pid)
    status = "ok" if processes else "no_candidates"
    if warnings or any(process["warnings"] for process in processes):
        status = "partial" if processes else "no_candidates"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "query": {
            "pid": pid,
            "process_pattern": process_pattern,
            "path_hint": path_hint,
        },
        "limits": {
            "process_scan_limit": process_scan_limit,
            "candidate_limit": candidate_limit,
            "fd_limit_per_process": fd_limit,
            "time_budget_seconds": time_budget_seconds,
        },
        "process_candidates": processes,
        "recommendation": recommendation,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读发现训练进程 PID 和候选数据路径（mindstudio-storage-analysis）"
    )
    parser.add_argument("--pid", type=int, help="已知或怀疑的目标进程 PID")
    parser.add_argument(
        "--process-pattern", help="用于缩小候选范围的进程命令文本特征"
    )
    parser.add_argument("--path-hint", help="用户提供的绝对数据路径线索")
    parser.add_argument("--max-processes", type=int, default=_DEFAULT_CANDIDATE_LIMIT)
    parser.add_argument("--max-fds", type=int, default=_DEFAULT_FD_LIMIT)
    parser.add_argument("--time-budget", type=float, default=_DEFAULT_TIME_BUDGET_SECONDS)
    parser.add_argument("-o", "--output", type=Path, help="输出候选 JSON；默认输出到终端")
    args = parser.parse_args(argv)
    if args.pid is not None and args.pid <= 0:
        parser.error("--pid must be a positive integer")
    if not 1 <= args.max_processes <= 100:
        parser.error("--max-processes must be between 1 and 100")
    if not 1 <= args.max_fds <= 4096:
        parser.error("--max-fds must be between 1 and 4096")
    if not 0.1 <= args.time_budget <= 30:
        parser.error("--time-budget must be between 0.1 and 30 seconds")
    if args.process_pattern and len(args.process_pattern) > 200:
        parser.error("--process-pattern is too long")
    if args.path_hint and not os.path.isabs(args.path_hint):
        parser.error("--path-hint must be an absolute path")
    try:
        payload = discover_targets(
            pid=args.pid,
            process_pattern=args.process_pattern,
            path_hint=args.path_hint,
            candidate_limit=args.max_processes,
            fd_limit=args.max_fds,
            time_budget_seconds=args.time_budget,
        )
        if args.output:
            if str(args.output) in {"-", "/dev/stdout", "/proc/self/fd/1"}:
                json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, allow_nan=False)
                sys.stdout.write("\n")
            else:
                _atomic_write_json(args.output.resolve(), payload)
        else:
            json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, allow_nan=False)
            sys.stdout.write("\n")
    except (OSError, ValueError) as exc:
        print(f"目标发现失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
