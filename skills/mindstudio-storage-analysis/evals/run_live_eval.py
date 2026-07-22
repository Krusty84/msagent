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
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
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


def _load_profile(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
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
    """Accept only a positive, provenance-backed R500 conduction finding."""
    if not isinstance(finding, dict):
        return False
    evidence_fields = finding.get("evidence_fields") or []
    return bool(
        finding.get("confidence") == "high"
        and finding.get("severity") == "high"
        and not finding.get("handoff")
        and not finding.get("priority_downgrade")
        and "profile.conduction_evidence" in evidence_fields
    )


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
        if status not in VALID_PROVIDER_STATUS:
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
        snapshot_path = Path(temp_dir) / "snapshot.json"
        findings_path = Path(temp_dir) / "findings.json"
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
        try:
            collected = _run(collect_command, timeout=args.duration + 45)
        except subprocess.TimeoutExpired:
            checks.append(Check("live-collector", "FAIL", "collector timed out"))
            collected = None
        if collected is not None and collected.returncode != 0:
            detail = (collected.stderr or collected.stdout).strip()[-1000:]
            checks.append(Check("live-collector", "FAIL", detail))
        elif collected is not None:
            checks.append(Check("live-collector", "PASS", f"duration={args.duration}s"))

        snapshot: dict[str, Any] | None = None
        if snapshot_path.is_file():
            try:
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                checks.append(Check("snapshot-json", "FAIL", str(exc)))
        if isinstance(snapshot, dict):
            schema = str(snapshot.get("schema_version", ""))
            status = "PASS" if schema.startswith("1.") else "FAIL"
            checks.append(Check("snapshot-schema", status, f"schema_version={schema}"))
            _provider_checks(snapshot, checks)

            nfs_mounts = [
                mount
                for mount in snapshot.get("mounts", [])
                if isinstance(mount, dict)
                and str(mount.get("fstype", "")).lower().startswith("nfs")
            ]
            if not nfs_mounts:
                status = "FAIL" if args.require_nfs else "SKIP"
                checks.append(Check("nfs-live-window", status, "no NFS mount visible"))
            else:
                provider = snapshot.get("nfs") or {}
                parsed = provider.get("parsed") if isinstance(provider, dict) else None
                metrics = (
                    parsed.get("mount_metrics", []) if isinstance(parsed, dict) else []
                )
                delta = [
                    item
                    for item in metrics
                    if isinstance(item, dict) and item.get("windowing") == "delta"
                ]
                if provider.get("status") == "ok" and delta:
                    operations = sum(float(item.get("ops") or 0) for item in delta)
                    detail = (
                        f"{len(delta)} delta mount(s), {operations:.0f} operation(s)"
                    )
                    if operations > 0:
                        checks.append(Check("nfs-live-window", "PASS", detail))
                    else:
                        checks.append(
                            Check(
                                "nfs-live-window",
                                "FAIL" if args.require_nfs else "SKIP",
                                detail + "; no representative NFS activity observed",
                            )
                        )
                else:
                    checks.append(
                        Check(
                            "nfs-live-window",
                            "FAIL",
                            f"NFS mounted but usable delta metrics missing; status={provider.get('status')}",
                        )
                    )

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
            analyzed = _run(analyze_command, timeout=30)
            if analyzed.returncode != 0:
                detail = (analyzed.stderr or analyzed.stdout).strip()[-1000:]
                checks.append(Check("live-analyzer", "FAIL", detail))
            else:
                checks.append(Check("live-analyzer", "PASS", "all rules completed"))
                findings = json.loads(findings_path.read_text(encoding="utf-8"))
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
                        Check("r500-profile", "FAIL", "profile was not supplied")
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
    report = {
        "schema_version": "1.0",
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "path": str(args.path),
            "duration_seconds": args.duration,
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
    parser.add_argument("--path", type=Path, default=Path.cwd())
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--require-npu", action="store_true")
    parser.add_argument("--require-npu-runtime", action="store_true")
    parser.add_argument("--require-nfs", action="store_true")
    parser.add_argument("--require-r500-high", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.duration <= 3600:
        parser.error("--duration must be between 1 and 3600 seconds")
    args.path = args.path.resolve()
    if not args.path.exists():
        parser.error(f"--path does not exist: {args.path}")
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
