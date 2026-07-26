#!/usr/bin/env python3
"""Read-only live validation for Linux, storage providers, and Ascend availability.

The runner never creates a synthetic IO/compute workload or changes host
configuration. Its Ascend runtime probe only initializes ACL, queries the
logical-device count, and finalizes ACL. Run the collector while a
representative workload is active to validate meaningful device, NFS, and
profiler evidence.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = SKILL_ROOT / "scripts" / "collect_io_snapshot.py"
ANALYZER = SKILL_ROOT / "scripts" / "analyze_io_snapshot.py"
VALID_PROVIDER_STATUS = {
    "ok",
    "missing",
    "permission_denied",
    "command_failed",
    "parse_failed",
    "empty",
    "unsupported",
}
_MAX_JSON_FILE_BYTES = 64 * 1024 * 1024


@dataclass
class Check:
    id: str
    status: str
    detail: str


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    if not path.parent.exists():
        raise OSError(f"report parent does not exist: {path.parent}")
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


def _read_json_file(path: Path) -> Any:
    size = path.stat().st_size
    if size > _MAX_JSON_FILE_BYTES:
        raise ValueError(
            f"JSON file is {size} bytes; limit is {_MAX_JSON_FILE_BYTES} bytes"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _load_profile(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = _read_json_file(path)
    except (OSError, ValueError, RecursionError, UnicodeDecodeError) as exc:
        return None, str(exc)
    if not isinstance(payload, dict):
        return None, f"profile must be a JSON object, got {type(payload).__name__}"
    if not ({"device_free_percent", "mte2_ratio"} & payload.keys()):
        return None, "profile has no device_free_percent or mte2_ratio"
    window = payload.get("profile_window")
    if (
        not isinstance(window, dict)
        or not isinstance(window.get("start"), str)
        or not isinstance(window.get("end"), str)
    ):
        return (
            None,
            "profile_window.start/end are required for dynamic profiler metrics",
        )
    return payload, None


def _r500_is_certified(finding: dict[str, Any] | None) -> bool:
    """R500 positive-high is unavailable until trusted artifact verification exists."""
    del finding
    return False


def _normalized_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return os.path.normpath(value)


def _path_under_mount(path: str, mount_point: str) -> bool:
    normalized_path = _normalized_path(path)
    normalized_mount = _normalized_path(mount_point)
    if not normalized_path or not normalized_mount:
        return False
    return normalized_path == normalized_mount or normalized_path.startswith(
        normalized_mount.rstrip("/") + "/"
    )


def _normalized_nfs_source(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if ":" in value:
        host, _, export = value.partition(":")
        return f"{host.strip().lower()}:{export.rstrip('/')}"
    return value.strip().lower()


def _normalized_nfs_fstype(value: Any) -> str:
    fstype = str(value or "").strip().lower()
    return "nfs" if fstype in {"nfs", "nfs4"} else fstype


def _nfs_identity(item: dict[str, Any], source_key: str) -> tuple[str, str, str]:
    return (
        _normalized_nfs_source(item.get(source_key)),
        _normalized_path(item.get("mount_point")),
        _normalized_nfs_fstype(item.get("fstype")),
    )


def _parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _provider_interval_in_snapshot(
    snapshot: dict[str, Any], provider: Any
) -> tuple[float, float] | None:
    if not isinstance(provider, dict):
        return None
    window = snapshot.get("window")
    if not isinstance(window, dict):
        return None
    snapshot_start = _parse_iso_timestamp(window.get("start"))
    snapshot_end = _parse_iso_timestamp(window.get("end"))
    collected_at = _parse_iso_timestamp(snapshot.get("collected_at"))
    provider_start = _parse_iso_timestamp(provider.get("started_at"))
    provider_end = _parse_iso_timestamp(provider.get("ended_at"))
    timestamps = (
        snapshot_start,
        snapshot_end,
        collected_at,
        provider_start,
        provider_end,
    )
    if any(value is None for value in timestamps):
        return None
    (
        snapshot_start,
        snapshot_end,
        collected_at,
        provider_start,
        provider_end,
    ) = (float(value) for value in timestamps)
    if snapshot_end <= snapshot_start or provider_end <= provider_start:
        return None
    if abs(collected_at - snapshot_start) > 2.0:
        return None
    if provider_start < snapshot_start - 2.0 or provider_end > snapshot_end + 2.0:
        return None
    overlap = min(provider_end, snapshot_end) - max(provider_start, snapshot_start)
    if overlap <= 0 or overlap / (provider_end - provider_start) < 0.5:
        return None
    return provider_start, provider_end


def _provider_overlaps_snapshot(snapshot: dict[str, Any], provider: Any) -> bool:
    return _provider_interval_in_snapshot(snapshot, provider) is not None


def _provider_windows_are_compatible(
    snapshot: dict[str, Any], first: Any, second: Any
) -> bool:
    """Require provider windows to overlap or be adjacent within scheduler tolerance."""
    first_interval = _provider_interval_in_snapshot(snapshot, first)
    second_interval = _provider_interval_in_snapshot(snapshot, second)
    if first_interval is None or second_interval is None:
        return False
    first_start, first_end = first_interval
    second_start, second_end = second_interval
    if first_end >= second_start and second_end >= first_start:
        return True
    gap = (
        second_start - first_end
        if first_end < second_start
        else first_start - second_end
    )
    return gap <= 2.0


def _target_nfs_live_check(snapshot: dict[str, Any], required: bool) -> Check:
    """Certify same-window NFS activity only for the snapshot target path."""
    target = snapshot.get("target")
    target_path = target.get("path") if isinstance(target, dict) else None
    if not isinstance(target_path, str) or not _normalized_path(target_path):
        return Check(
            "nfs-live-window",
            "FAIL" if required else "SKIP",
            "snapshot target.path is required to certify target-scoped NFS activity",
        )

    raw_mounts = snapshot.get("mounts")
    mounts = raw_mounts if isinstance(raw_mounts, list) else []
    matching_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict)
        and _path_under_mount(target_path, str(mount.get("mount_point", "")))
    ]
    if not matching_mounts:
        detail = f"target path {target_path} is not on a visible mount"
        return Check("nfs-live-window", "FAIL" if required else "SKIP", detail)

    _, target_mount = max(
        enumerate(matching_mounts),
        key=lambda item: (
            len(_normalized_path(item[1].get("mount_point"))),
            item[0],
        ),
    )
    if _normalized_nfs_fstype(target_mount.get("fstype")) != "nfs":
        detail = (
            f"target path {target_path} resolves to non-NFS mount "
            f"{target_mount.get('mount_point')} ({target_mount.get('fstype')})"
        )
        return Check("nfs-live-window", "FAIL" if required else "SKIP", detail)
    target_identity = _nfs_identity(target_mount, source_key="device")
    if not all(target_identity):
        return Check(
            "nfs-live-window",
            "FAIL" if required else "SKIP",
            "target NFS mount is missing source, mount_point, or fstype identity",
        )

    mounts_provider = snapshot.get("mounts_provider")
    provider = snapshot.get("nfs")
    if (
        not isinstance(mounts_provider, dict)
        or mounts_provider.get("status") != "ok"
        or not _provider_overlaps_snapshot(snapshot, mounts_provider)
    ):
        return Check(
            "nfs-live-window",
            "FAIL" if required else "SKIP",
            "target NFS mount list lacks a valid overlapping provider window",
        )
    if (
        not isinstance(provider, dict)
        or provider.get("status") != "ok"
        or not _provider_overlaps_snapshot(snapshot, provider)
    ):
        status = provider.get("status") if isinstance(provider, dict) else None
        return Check(
            "nfs-live-window",
            "FAIL" if required else "SKIP",
            f"target NFS delta metrics lack a valid overlapping provider window; status={status}",
        )
    if not _provider_windows_are_compatible(snapshot, mounts_provider, provider):
        return Check(
            "nfs-live-window",
            "FAIL" if required else "SKIP",
            "target NFS mount and delta metric provider windows do not overlap "
            "or occur within 2 seconds",
        )

    parsed = provider.get("parsed") if isinstance(provider, dict) else None
    raw_metrics = parsed.get("mount_metrics") if isinstance(parsed, dict) else None
    metrics = raw_metrics if isinstance(raw_metrics, list) else []
    target_metrics = [
        metric
        for metric in metrics
        if isinstance(metric, dict)
        and metric.get("windowing") == "delta"
        and _nfs_identity(metric, source_key="source") == target_identity
    ]
    if not target_metrics:
        source, mount_point, fstype = target_identity
        return Check(
            "nfs-live-window",
            "FAIL" if required else "SKIP",
            "no identity-matched delta metric for target NFS mount "
            f"{source} on {mount_point} ({fstype})",
        )

    operations = 0.0
    invalid_operations = False
    for metric in target_metrics:
        raw_value = metric.get("ops")
        if isinstance(raw_value, bool):
            invalid_operations = True
            continue
        try:
            value = float(raw_value or 0)
        except (TypeError, ValueError, OverflowError):
            invalid_operations = True
            continue
        if not math.isfinite(value) or value < 0:
            invalid_operations = True
            continue
        candidate = operations + value
        if not math.isfinite(candidate):
            invalid_operations = True
            continue
        operations = candidate
    detail = (
        f"target mount {target_identity[1]}: {len(target_metrics)} delta metric(s), "
        f"{operations:.0f} operation(s)"
    )
    if invalid_operations:
        return Check(
            "nfs-live-window",
            "FAIL" if required else "SKIP",
            detail + "; invalid ops values present",
        )
    if operations <= 0:
        return Check(
            "nfs-live-window",
            "FAIL" if required else "SKIP",
            detail + "; no representative target NFS activity observed",
        )
    return Check("nfs-live-window", "PASS", detail)


def _npu_hardware_check() -> tuple[Check, int]:
    executable = shutil.which("npu-smi")
    device_nodes = sorted(Path("/dev").glob("davinci[0-9]*"))
    if not executable:
        return Check("ascend-hardware", "SKIP", "npu-smi is not installed"), 0
    try:
        result = _run([executable, "info"], timeout=20)
    except subprocess.TimeoutExpired:
        return Check("ascend-hardware", "FAIL", "npu-smi info timed out"), 0
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        return Check("ascend-hardware", "FAIL", f"npu-smi failed: {detail}"), 0
    health_rows = re.findall(
        r"^\|\s*\d+\s+(\S+)\s+\|\s+(OK|Warning|Alarm|Fault)\b",
        result.stdout,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if not health_rows:
        return (
            Check("ascend-hardware", "FAIL", "npu-smi output has no health rows"),
            0,
        )
    unhealthy = [
        f"{name}:{health}" for name, health in health_rows if health.upper() != "OK"
    ]
    if unhealthy:
        return (
            Check(
                "ascend-hardware",
                "FAIL",
                f"unhealthy NPU rows: {', '.join(unhealthy)}",
            ),
            len(health_rows),
        )
    if not device_nodes:
        return (
            Check(
                "ascend-hardware",
                "SKIP",
                f"{len(health_rows)} healthy NPU chip(s), but no /dev/davinciN node",
            ),
            len(health_rows),
        )
    detail = (
        f"{len(health_rows)} healthy NPU chip(s); "
        f"{len(device_nodes)} /dev/davinci device node(s)"
    )
    return Check("ascend-hardware", "PASS", detail), len(health_rows)


def _find_toolkit_root() -> Path | None:
    candidates: list[Path] = []
    for name in ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME"):
        value = os.environ.get(name)
        if value:
            candidates.append(Path(value))
    candidates.extend(
        [
            Path("/usr/local/Ascend/ascend-toolkit/latest"),
            Path("/usr/local/Ascend/latest"),
        ]
    )
    profiler = shutil.which("msprof")
    if profiler:
        resolved = Path(profiler).resolve()
        if resolved.parent.name == "bin":
            candidates.append(resolved.parent.parent)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "set_env.sh").is_file() and (
            resolved / "lib64" / "libascendcl.so"
        ).is_file():
            return resolved
    return None


def _acl_runtime_probe() -> tuple[bool, str]:
    probe = """
import json
import acl

initialized = False
try:
    ret = acl.init()
    if ret != 0:
        raise RuntimeError(f"acl.init returned {ret}")
    initialized = True
    count, ret = acl.rt.get_device_count()
    if ret != 0:
        raise RuntimeError(f"acl.rt.get_device_count returned {ret}")
    if not isinstance(count, int) or count < 1:
        raise RuntimeError(f"invalid ACL device count: {count!r}")
    print(json.dumps({"device_count": count}))
finally:
    if initialized:
        ret = acl.finalize()
        if ret != 0:
            raise RuntimeError(f"acl.finalize returned {ret}")
"""
    try:
        result = _run([sys.executable, "-c", probe], timeout=30)
    except subprocess.TimeoutExpired:
        return False, "ACL initialization probe timed out"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        return False, f"ACL initialization probe failed: {detail}"
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        count = int(payload["device_count"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False, "ACL initialization probe returned malformed output"
    return True, f"acl.init/get_device_count/finalize passed; logical_devices={count}"


def _npu_runtime_check(required: bool) -> Check:
    toolkit = _find_toolkit_root()
    modules = [
        name
        for name in ("torch_npu", "mindspore", "acl")
        if importlib.util.find_spec(name) is not None
    ]
    profiler = shutil.which("msprof")
    if toolkit and "acl" in modules:
        passed, probe_detail = _acl_runtime_probe()
        if not passed:
            return Check("ascend-runtime", "FAIL" if required else "SKIP", probe_detail)
        detail = f"toolkit={toolkit}; {probe_detail}"
        if profiler:
            detail += f"; profiler={profiler}"
        return Check("ascend-runtime", "PASS", detail)
    missing = []
    if not toolkit:
        missing.append("CANN Toolkit set_env.sh")
    if "acl" not in modules:
        missing.append("importable acl Python runtime")
    status = "FAIL" if required else "SKIP"
    return Check("ascend-runtime", status, "missing " + " and ".join(missing))


def _provider_checks(snapshot: dict[str, Any], checks: list[Check]) -> None:
    for name in (
        "mounts_provider",
        "block_devices",
        "iostat",
        "pidstat",
        "process_io_map",
        "memory",
        "df",
        "nfs",
    ):
        provider = snapshot.get(name)
        if not isinstance(provider, dict):
            checks.append(Check(f"provider-{name}", "FAIL", "provider is missing"))
            continue
        status = provider.get("status")
        if not isinstance(status, str) or status not in VALID_PROVIDER_STATUS:
            checks.append(
                Check(f"provider-{name}", "FAIL", f"invalid status {status!r}")
            )
        elif status in {"command_failed", "parse_failed", "permission_denied"}:
            detail = str(provider.get("error") or provider.get("stderr") or status)
            checks.append(Check(f"provider-{name}", "FAIL", detail[-500:]))
        elif status == "ok":
            checks.append(Check(f"provider-{name}", "PASS", "status=ok"))
        else:
            checks.append(Check(f"provider-{name}", "SKIP", f"status={status}"))


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    checks: list[Check] = []
    if getattr(args, "require_r500_high", False):
        checks.append(
            Check(
                "r500-high-certification",
                "FAIL",
                "unavailable until a trusted profiler artifact verifier is implemented",
            )
        )
        args.require_r500_high = False
    snapshot_input = getattr(args, "snapshot", None)
    target_pid = getattr(args, "pid", None)
    if platform.system() != "Linux":
        checks.append(Check("linux-proc", "FAIL", f"platform={platform.system()}"))
    else:
        required_proc = [Path("/proc/diskstats"), Path("/proc/self/mountstats")]
        missing = [str(path) for path in required_proc if not os.access(path, os.R_OK)]
        checks.append(
            Check(
                "linux-proc",
                "FAIL" if missing else "PASS",
                f"unreadable: {', '.join(missing)}"
                if missing
                else "required /proc files readable",
            )
        )

    npu_check, npu_count = _npu_hardware_check()
    checks.append(npu_check)
    if args.require_npu and npu_check.status != "PASS":
        npu_check.status = "FAIL"
    checks.append(_npu_runtime_check(args.require_npu_runtime))
    profile_valid = True
    if args.profile:
        _profile, profile_error = _load_profile(args.profile)
        profile_valid = profile_error is None
        checks.append(
            Check(
                "profile-json",
                "PASS" if profile_valid else "FAIL",
                "recognized profile fields present" if profile_valid else profile_error,
            )
        )

    with tempfile.TemporaryDirectory(prefix="mindstudio-storage-live-") as temp_dir:
        snapshot_path = snapshot_input or Path(temp_dir) / "snapshot.json"
        findings_path = Path(temp_dir) / "findings.json"
        if snapshot_input:
            checks.append(
                Check(
                    "snapshot-input", "PASS", f"using supplied snapshot {snapshot_path}"
                )
            )
        else:
            collect_command = [
                sys.executable,
                str(COLLECTOR),
                "--duration",
                str(args.duration),
                "--out",
                str(snapshot_path),
                "--path",
                str(args.path),
            ]
            if target_pid is not None:
                collect_command.extend(["--pid", str(target_pid)])
            try:
                collected = _run(collect_command, timeout=args.duration + 45)
            except subprocess.TimeoutExpired:
                checks.append(Check("live-collector", "FAIL", "collector timed out"))
                collected = None
            if collected is not None and collected.returncode != 0:
                detail = (collected.stderr or collected.stdout).strip()[-1000:]
                checks.append(Check("live-collector", "FAIL", detail))
            elif collected is not None:
                checks.append(
                    Check("live-collector", "PASS", f"duration={args.duration}s")
                )

        snapshot: dict[str, Any] | None = None
        if not snapshot_path.is_file():
            checks.append(
                Check("snapshot-json", "FAIL", f"snapshot is missing: {snapshot_path}")
            )
        else:
            try:
                payload = _read_json_file(snapshot_path)
            except (OSError, ValueError, RecursionError, UnicodeDecodeError) as exc:
                checks.append(Check("snapshot-json", "FAIL", str(exc)))
            else:
                if isinstance(payload, dict):
                    snapshot = payload
                else:
                    checks.append(
                        Check(
                            "snapshot-json",
                            "FAIL",
                            "snapshot must be a JSON object, "
                            f"got {type(payload).__name__}",
                        )
                    )
        if isinstance(snapshot, dict):
            schema = str(snapshot.get("schema_version", ""))
            status = "PASS" if re.fullmatch(r"1\.\d+", schema) else "FAIL"
            checks.append(Check("snapshot-schema", status, f"schema_version={schema}"))
            _provider_checks(snapshot, checks)

            checks.append(_target_nfs_live_check(snapshot, args.require_nfs))

            analyze_command = [
                sys.executable,
                str(ANALYZER),
                str(snapshot_path),
                "--mode",
                "all",
                "--output",
                str(findings_path),
            ]
            if args.profile and profile_valid:
                analyze_command.extend(["--profile", str(args.profile)])
            try:
                analyzed = _run(analyze_command, timeout=30)
            except subprocess.TimeoutExpired:
                checks.append(Check("live-analyzer", "FAIL", "analyzer timed out"))
                analyzed = None
            if analyzed is not None and analyzed.returncode != 0:
                detail = (analyzed.stderr or analyzed.stdout).strip()[-1000:]
                checks.append(Check("live-analyzer", "FAIL", detail))
            if analyzed is not None:
                try:
                    findings = json.loads(findings_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    if analyzed.returncode == 0:
                        checks.append(Check("live-analyzer", "FAIL", str(exc)))
                    findings = None
                if not isinstance(findings, dict):
                    if findings is not None:
                        checks.append(
                            Check(
                                "live-analyzer",
                                "FAIL",
                                "analyzer output must be a JSON object, "
                                f"got {type(findings).__name__}",
                            )
                        )
                    findings = {"findings": []}
                    analyzer_output_valid = False
                elif not isinstance(findings.get("findings"), list):
                    checks.append(
                        Check(
                            "live-analyzer",
                            "FAIL",
                            "analyzer output findings must be a list",
                        )
                    )
                    findings = {"findings": []}
                    analyzer_output_valid = False
                else:
                    if analyzed.returncode == 0:
                        checks.append(
                            Check("live-analyzer", "PASS", "all rules completed")
                        )
                    analyzer_output_valid = True
                if analyzer_output_valid:
                    validation_errors = findings.get("validation_errors") or []
                    checks.append(
                        Check(
                            "snapshot-validation",
                            "FAIL" if validation_errors else "PASS",
                            (
                                f"analyzer validation errors={validation_errors}"
                                if validation_errors
                                else "no analyzer validation errors"
                            ),
                        )
                    )
                r500 = next(
                    (
                        item
                        for item in findings.get("findings", [])
                        if isinstance(item, dict) and item.get("rule_id") == "R500"
                    ),
                    None,
                )
                profile_errors = findings.get("profile_validation_errors") or []
                if args.profile and not profile_valid:
                    checks.append(
                        Check("r500-profile", "SKIP", "profile JSON is invalid")
                    )
                elif args.profile and r500:
                    confidence = str(r500.get("confidence"))
                    certified = _r500_is_certified(r500)
                    status = "PASS"
                    if profile_errors or (args.require_r500_high and not certified):
                        status = "FAIL"
                    validation_detail = (
                        f"; profile errors={profile_errors}" if profile_errors else ""
                    )
                    checks.append(
                        Check(
                            "r500-profile",
                            status,
                            f"confidence={confidence}; certified={certified}; "
                            f"{r500.get('summary', '')}" + validation_detail,
                        )
                    )
                elif args.require_r500_high:
                    checks.append(
                        Check(
                            "r500-profile",
                            "FAIL",
                            "a certified R500 finding was not produced",
                        )
                    )
                else:
                    checks.append(
                        Check(
                            "r500-profile",
                            "SKIP",
                            "supply --profile from a same-workload profiler window",
                        )
                    )

    failed = sum(check.status == "FAIL" for check in checks)
    snapshot_target = snapshot.get("target") if isinstance(snapshot, dict) else None
    snapshot_target_path = (
        snapshot_target.get("path") if isinstance(snapshot_target, dict) else None
    )
    report = {
        "schema_version": "1.0",
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "path": snapshot_target_path if snapshot_input else str(args.path),
            "pid": (
                snapshot_target.get("pid")
                if snapshot_input and isinstance(snapshot_target, dict)
                else target_pid
            ),
            "snapshot": str(snapshot_input) if snapshot_input else None,
            "duration_seconds": (
                snapshot.get("duration_seconds", args.duration)
                if snapshot_input and isinstance(snapshot, dict)
                else args.duration
            ),
            "npu_count": npu_count,
        },
        "summary": {
            "passed": sum(check.status == "PASS" for check in checks),
            "failed": failed,
            "skipped": sum(check.status == "SKIP" for check in checks),
        },
        "checks": [asdict(check) for check in checks],
    }
    return (1 if failed else 0), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only live validation for mindstudio-storage-analysis"
    )
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument(
        "--pid",
        type=int,
        help="workload root PID for bounded R400 process-to-device mapping",
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--path", type=Path)
    source_group.add_argument(
        "--snapshot",
        type=Path,
        help="analyze an existing snapshot captured in the same window as --profile",
    )
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--require-npu", action="store_true")
    parser.add_argument("--require-npu-runtime", action="store_true")
    parser.add_argument("--require-nfs", action="store_true")
    parser.add_argument(
        "--require-r500-high",
        action="store_true",
        help="unavailable: JSON-only profiles cannot certify R500 high",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.duration <= 3600:
        parser.error("--duration must be between 1 and 3600 seconds")
    if args.pid is not None and args.pid < 1:
        parser.error("--pid must be a positive integer")
    if args.snapshot:
        if args.pid is not None:
            parser.error("--pid is only valid with live --path collection")
        args.snapshot = args.snapshot.resolve()
        if not args.snapshot.is_file():
            parser.error(f"--snapshot does not exist: {args.snapshot}")
        args.path = None
    else:
        args.path = (args.path or Path.cwd()).resolve()
        if not args.path.exists():
            parser.error(f"--path does not exist: {args.path}")
    if args.profile and not args.snapshot:
        parser.error(
            "--profile requires --snapshot captured during the same workload window"
        )
    if args.require_r500_high:
        parser.error(
            "--require-r500-high is unavailable until a trusted profiler artifact verifier is implemented"
        )
    if args.profile and not args.profile.is_file():
        parser.error(f"--profile does not exist: {args.profile}")

    try:
        rc, report = run(args)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"live eval failed: {exc}", file=sys.stderr)
        return 2
    for check in report["checks"]:
        print(f"[{check['status']}] {check['id']}: {check['detail']}")
    summary = report["summary"]
    print(
        f"summary: {summary['passed']} passed, {summary['failed']} failed, "
        f"{summary['skipped']} skipped"
    )
    if args.report:
        try:
            _atomic_write_json(args.report, report)
        except OSError as exc:
            print(f"cannot write report {args.report}: {exc}", file=sys.stderr)
            return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
