#!/usr/bin/env python3
"""Compile and run a bounded AscendCL AICore smoke test on logical NPUs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(__file__).with_name("npu_acl_smoke.cpp")


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
    for candidate in candidates:
        try:
            root = candidate.resolve()
        except OSError:
            continue
        if all(
            path.is_file()
            for path in (
                root / "include" / "acl" / "acl.h",
                root / "include" / "aclnnop" / "aclnn_add.h",
                root / "lib64" / "libascendcl.so",
                root / "lib64" / "libopapi.so",
            )
        ):
            return root
    return None


def _acl_device_count() -> int:
    try:
        import acl  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        raise RuntimeError(f"cannot import acl: {exc}") from exc
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
        return count
    finally:
        if initialized:
            ret = acl.finalize()
            if ret != 0:
                raise RuntimeError(f"acl.finalize returned {ret}")


def _compile(toolkit: Path, output: Path) -> list[str]:
    compiler = shutil.which(os.environ.get("CXX", "c++"))
    if not compiler:
        raise RuntimeError("C++ compiler is not installed")
    command = [
        compiler,
        "-std=c++17",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror=return-type",
        "-D_FORTIFY_SOURCE=2",
        "-fstack-protector-strong",
        f"-I{toolkit / 'include'}",
        str(SOURCE),
        f"-L{toolkit / 'lib64'}",
        f"-Wl,-rpath,{toolkit / 'lib64'}",
        "-Wl,-z,relro,-z,now,-z,noexecstack",
        "-lascendcl",
        "-lnnopbase",
        "-lopapi",
        "-o",
        str(output),
    ]
    try:
        result = _run(command, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("NPU smoke test compilation timed out") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"NPU smoke test compilation failed: {detail}")
    return command


def _parse_result(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("NPU smoke test returned malformed output") from exc
    if not isinstance(payload, dict) or payload.get("status") != "PASS":
        raise RuntimeError("NPU smoke test did not return PASS")
    return payload


def _execute(
    binary: Path,
    devices: list[int],
    elements: int,
    iterations: int,
    timeout: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for device in devices:
        command = [str(binary), str(device), str(elements), str(iterations)]
        try:
            completed = _run(command, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"device {device} smoke test timed out") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-1000:]
            raise RuntimeError(f"device {device} smoke test failed: {detail}")
        payload = _parse_result(completed.stdout)
        if payload.get("device") != device:
            raise RuntimeError(f"device {device} smoke test reported the wrong device")
        results.append(payload)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile and run a bounded AscendCL AICore smoke test"
    )
    parser.add_argument("--device", type=int, action="append")
    parser.add_argument("--elements", type=int, default=262144)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not 8 <= args.elements <= 16 * 1024 * 1024:
        parser.error("--elements must be between 8 and 16777216")
    if not 1 <= args.iterations <= 10000:
        parser.error("--iterations must be between 1 and 10000")
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout must be between 1 and 3600 seconds")

    try:
        toolkit = _find_toolkit_root()
        if toolkit is None:
            raise RuntimeError(
                "CANN Toolkit with ACLNN headers/libraries is unavailable; source set_env.sh"
            )
        device_count = _acl_device_count()
        devices = sorted(
            set(args.device if args.device is not None else range(device_count))
        )
        if not devices or any(
            device < 0 or device >= device_count for device in devices
        ):
            raise RuntimeError(
                f"requested devices {devices} are outside ACL device_count={device_count}"
            )

        temporary: tempfile.TemporaryDirectory[str] | None = None
        if args.build_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix="mindstudio-npu-runtime-")
            build_dir = Path(temporary.name)
        else:
            build_dir = args.build_dir.resolve()
            build_dir.mkdir(parents=True, exist_ok=True)
        try:
            binary = build_dir / "npu_acl_smoke"
            compile_command = _compile(toolkit, binary)
            results = _execute(
                binary, devices, args.elements, args.iterations, args.timeout
            )
        finally:
            if temporary is not None:
                temporary.cleanup()
    except (OSError, RuntimeError) as exc:
        print(f"NPU runtime eval failed: {exc}", file=sys.stderr)
        return 1

    report = {
        "schema_version": "1.0",
        "toolkit": str(toolkit),
        "device_count": device_count,
        "compile_command": compile_command,
        "summary": {"passed": len(results), "failed": 0},
        "results": results,
    }
    for result in results:
        print(
            f"[PASS] device={result['device']} elements={result['elements']} "
            f"iterations={result['iterations']} elapsed_ms={result['elapsed_ms']} "
            f"max_abs_error={result['max_abs_error']}"
        )
    if args.report:
        try:
            _atomic_write_json(args.report.resolve(), report)
        except OSError as exc:
            print(f"cannot write report {args.report}: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
