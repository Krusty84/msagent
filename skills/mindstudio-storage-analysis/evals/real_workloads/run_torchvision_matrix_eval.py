#!/usr/bin/env python3
"""Run ten bounded TorchVision workloads through the storage-analysis pipeline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from train_torchvision_io import MODEL_NAMES


SKILL_ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("train_torchvision_io.py")
DISCOVER = SKILL_ROOT / "scripts" / "discover_io_target.py"
COLLECT = SKILL_ROOT / "scripts" / "collect_io_snapshot.py"
ANALYZE = SKILL_ROOT / "scripts" / "analyze_io_snapshot.py"
RENDER = SKILL_ROOT / "scripts" / "render_io_report.py"

BATCH_SIZES = {
    "resnet18": 96,
    "resnet50": 64,
    "mobilenet_v3_small": 128,
    "mobilenet_v3_large": 96,
    "efficientnet_b0": 96,
    "densenet121": 48,
    "shufflenet_v2_x1_0": 128,
    "squeezenet1_1": 128,
    "convnext_tiny": 48,
    "swin_t": 48,
}


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON object required: {path}")
    return value


def _run(command: list[str], log: Path, timeout: float) -> None:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    log.write_text(
        f"$ {' '.join(command)}\n\nSTDOUT\n{completed.stdout}\nSTDERR\n{completed.stderr}",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}; see {log}"
        )


def _prepare_dataset(work_root: Path, sample_count: int) -> Path:
    dataset_root = work_root / "data" / f"generated-imagefolder-{sample_count}"
    marker = dataset_root / "dataset.json"
    if marker.is_file():
        metadata = _read_json(marker)
        if metadata.get("sample_count") == sample_count:
            return dataset_root
        raise RuntimeError(f"dataset marker does not match request: {marker}")
    if dataset_root.exists():
        raise RuntimeError(
            f"incomplete dataset directory exists; inspect it before retrying: {dataset_root}"
        )

    dataset_root.mkdir(parents=True)
    class_names = [f"class-{index:02d}" for index in range(10)]
    class_counts = {name: 0 for name in class_names}
    for index in range(sample_count):
        class_name = class_names[index % len(class_names)]
        class_dir = dataset_root / class_name
        class_dir.mkdir(exist_ok=True)
        destination = class_dir / f"{index:06d}.bmp"
        generator = np.random.default_rng(20260731 + index)
        pixels = generator.integers(0, 256, size=(224, 224, 3), dtype=np.uint8)
        Image.fromarray(pixels, mode="RGB").save(destination)
        class_counts[class_name] += 1
        if (index + 1) % 500 == 0:
            print(f"dataset export: {index + 1}/{sample_count}", flush=True)
    _atomic_json(
        marker,
        {
            "schema_version": "1.0",
            "source": "deterministic generated RGB images",
            "sample_count": sample_count,
            "format": "224x224 BMP ImageFolder",
            "class_counts": class_counts,
        },
    )
    return dataset_root


def _wait_ready(process: subprocess.Popen[str], ready_file: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_file.is_file():
            return
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"training exited before ready: returncode={return_code}")
        time.sleep(0.2)
    raise RuntimeError(f"training did not become ready within {timeout:.0f}s")


def _sample_gpu() -> dict[str, float] | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    completed = subprocess.run(
        [
            executable,
            "--query-gpu=utilization.gpu,memory.used",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
    )
    if completed.returncode != 0:
        return None
    try:
        utilization, memory = completed.stdout.strip().splitlines()[0].split(",")
        return {
            "timestamp": time.time(),
            "utilization_percent": float(utilization.strip()),
            "memory_used_mib": float(memory.strip()),
        }
    except (IndexError, ValueError):
        return None


def _run_collector(
    command: list[str], log: Path, gpu_samples_path: Path, timeout: float
) -> None:
    with log.open("w", encoding="utf-8") as stream:
        stream.write(f"$ {' '.join(command)}\n\n")
        stream.flush()
        process = subprocess.Popen(
            command,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
        samples: list[dict[str, float]] = []
        deadline = time.monotonic() + timeout
        while process.poll() is None and time.monotonic() < deadline:
            sample = _sample_gpu()
            if sample:
                samples.append(sample)
            time.sleep(1.0)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            raise RuntimeError(f"collector exceeded timeout; see {log}")
        return_code = process.returncode
    _atomic_json(gpu_samples_path, {"samples": samples})
    if return_code != 0:
        raise RuntimeError(f"collector failed ({return_code}); see {log}")


def _agent_report(findings: dict[str, Any], model: str) -> dict[str, Any]:
    recommendations = []
    for finding in findings.get("findings", []):
        if not isinstance(finding, dict):
            continue
        checks = finding.get("recommended_next_checks")
        if not isinstance(checks, list) or not checks:
            continue
        recommendations.append(
            {
                "priority": "high" if finding.get("severity") == "high" else "info",
                "title": f"{finding.get('rule_id')} 的下一步检查",
                "detail": str(checks[0]),
                "source_rule_ids": [str(finding.get("rule_id"))],
                "requires_confirmation": False,
            }
        )
    return {
        "summary": (
            f"这是 {model} 在 NVIDIA H200 上的短训练兼容性实测。"
            f"规则摘要：{findings.get('summary', '未提供')}。"
            "本报告验证 Host IO 流程，不把 GPU 指标伪装成 Ascend msprof 证据。"
        ),
        "recommendations": recommendations[:6],
        "limitations": [
            "当前机器不是 Ascend NPU，无法验证 msprof 和正式 R500 传导链。",
            "当前环境缺少 iostat/pidstat，部分动态指标使用 /proc 降级采集。",
            "这是短时功能测试，不代表模型训练收敛或长期性能。",
        ],
    }


def _run_model(
    *,
    model: str,
    dataset_root: Path,
    model_dir: Path,
    train_seconds: float,
    collect_seconds: int,
    workers: int,
) -> dict[str, Any]:
    model_dir.mkdir(parents=True)
    ready_file = model_dir / "ready.json"
    training_report = model_dir / "training.json"
    training_log = model_dir / "training.log"
    training_command = [
        sys.executable,
        str(WORKER),
        "--model",
        model,
        "--data-root",
        str(dataset_root),
        "--seconds",
        str(train_seconds),
        "--batch-size",
        str(BATCH_SIZES[model]),
        "--workers",
        str(workers),
        "--ready-file",
        str(ready_file),
        "--report",
        str(training_report),
    ]
    with training_log.open("w", encoding="utf-8") as stream:
        stream.write(f"$ {' '.join(training_command)}\n\n")
        stream.flush()
        training = subprocess.Popen(
            training_command,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_ready(training, ready_file, timeout=180)
            pid = training.pid
            targets = model_dir / "target_candidates.json"
            snapshot = model_dir / "io_snapshot.json"
            findings_path = model_dir / "findings.json"
            agent_path = model_dir / "agent_report.json"
            html_path = model_dir / "io_report.html"
            _run(
                [
                    sys.executable,
                    str(DISCOVER),
                    "--pid",
                    str(pid),
                    "-o",
                    str(targets),
                ],
                model_dir / "discover.log",
                timeout=30,
            )
            _run_collector(
                [
                    sys.executable,
                    str(COLLECT),
                    "--duration",
                    str(collect_seconds),
                    "--pid",
                    str(pid),
                    "--path",
                    str(dataset_root),
                    "--out",
                    str(snapshot),
                ],
                model_dir / "collect.log",
                model_dir / "gpu_samples.json",
                timeout=collect_seconds + 60,
            )
            _run(
                [
                    sys.executable,
                    str(ANALYZE),
                    str(snapshot),
                    "--mode",
                    "all",
                    "-o",
                    str(findings_path),
                ],
                model_dir / "analyze.log",
                timeout=30,
            )
            findings = _read_json(findings_path)
            _atomic_json(agent_path, _agent_report(findings, model))
            _run(
                [
                    sys.executable,
                    str(RENDER),
                    "--snapshot",
                    str(snapshot),
                    "--findings",
                    str(findings_path),
                    "--targets",
                    str(targets),
                    "--agent-report",
                    str(agent_path),
                    "--title",
                    f"{model} 短训练存储诊断报告",
                    "--output",
                    str(html_path),
                ],
                model_dir / "render.log",
                timeout=30,
            )
            return_code = training.wait(timeout=train_seconds + 180)
            if return_code != 0:
                raise RuntimeError(
                    f"training failed ({return_code}); see {training_log}"
                )
        finally:
            if training.poll() is None:
                training.terminate()
                try:
                    training.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    training.kill()
                    training.wait(timeout=10)

    training_result = _read_json(training_report)
    targets_result = _read_json(targets)
    findings_result = _read_json(findings_path)
    gpu_samples = _read_json(model_dir / "gpu_samples.json").get("samples", [])
    utilization = [
        float(item["utilization_percent"])
        for item in gpu_samples
        if isinstance(item, dict) and isinstance(item.get("utilization_percent"), (int, float))
    ]
    positive = [
        item
        for item in findings_result.get("findings", [])
        if isinstance(item, dict)
        and item.get("severity") in {"medium", "high"}
        and item.get("confidence") in {"medium", "high"}
    ]
    recommendation = targets_result.get("recommendation") or {}
    return {
        "model": model,
        "status": "PASS",
        "training": training_result,
        "discovery": {
            "recommended_pid": recommendation.get("pid"),
            "recommended_path": recommendation.get("path"),
            "requires_confirmation": recommendation.get("requires_confirmation"),
        },
        "analysis_summary": findings_result.get("summary"),
        "positive_rules": [item.get("rule_id") for item in positive],
        "gpu_utilization_mean": sum(utilization) / len(utilization) if utilization else None,
        "gpu_utilization_max": max(utilization) if utilization else None,
        "artifacts": {
            "directory": str(model_dir),
            "snapshot": str(snapshot),
            "findings": str(findings_path),
            "html": str(html_path),
        },
    }


def _write_summary_markdown(path: Path, results: list[dict[str, Any]]) -> None:
    lines = [
        "# TorchVision 真实短训练矩阵",
        "",
        "| 模型 | 状态 | 步数 | 样本/秒 | GPU 平均/峰值 | 正向规则 | HTML |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for item in results:
        training = item.get("training") if isinstance(item.get("training"), dict) else {}
        artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
        mean = item.get("gpu_utilization_mean")
        maximum = item.get("gpu_utilization_max")
        gpu = "—" if mean is None else f"{mean:.1f}% / {maximum:.1f}%"
        rules = ", ".join(str(rule) for rule in item.get("positive_rules", [])) or "无"
        html_path = artifacts.get("html", "")
        lines.append(
            f"| {item.get('model')} | {item.get('status')} | {training.get('steps', '—')} | "
            f"{training.get('samples_per_second', 0):.1f} | {gpu} | {rules} | "
            f"[报告]({html_path}) |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--models", nargs="+", choices=MODEL_NAMES, default=list(MODEL_NAMES))
    parser.add_argument("--sample-count", type=int, default=4000)
    parser.add_argument("--train-seconds", type=float, default=15.0)
    parser.add_argument("--collect-seconds", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if not 100 <= args.sample_count <= 50000:
        parser.error("--sample-count must be between 100 and 50000")
    if not 5.0 <= args.train_seconds <= 600.0:
        parser.error("--train-seconds must be between 5 and 600")
    if not 2 <= args.collect_seconds <= int(args.train_seconds - 2):
        parser.error("--collect-seconds must fit inside the active training window")
    if not 0 <= args.workers <= 32:
        parser.error("--workers must be between 0 and 32")

    work_root = args.work_root.resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    dataset_root = _prepare_dataset(work_root, args.sample_count)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = work_root / "runs" / run_id
    run_root.mkdir(parents=True)
    print(f"run_root={run_root}", flush=True)
    print(f"dataset_root={dataset_root}", flush=True)

    results: list[dict[str, Any]] = []
    failures = 0
    for index, model in enumerate(args.models, start=1):
        print(f"[{index}/{len(args.models)}] starting {model}", flush=True)
        model_dir = run_root / f"{index:02d}-{model}"
        try:
            result = _run_model(
                model=model,
                dataset_root=dataset_root,
                model_dir=model_dir,
                train_seconds=args.train_seconds,
                collect_seconds=args.collect_seconds,
                workers=args.workers,
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            result = {
                "model": model,
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
                "artifacts": {"directory": str(model_dir)},
            }
            print(f"[{index}/{len(args.models)}] FAIL {model}: {exc}", flush=True)
        else:
            print(
                f"[{index}/{len(args.models)}] PASS {model}: "
                f"rules={result['positive_rules']}",
                flush=True,
            )
        results.append(result)
        _atomic_json(run_root / "matrix-summary.json", {"results": results})

    summary = {
        "schema_version": "1.0",
        "run_id": run_id,
        "dataset_root": str(dataset_root),
        "requested_models": list(args.models),
        "passed": sum(item.get("status") == "PASS" for item in results),
        "failed": failures,
        "results": results,
    }
    _atomic_json(run_root / "matrix-summary.json", summary)
    _write_summary_markdown(run_root / "matrix-summary.md", results)
    print(json.dumps({key: summary[key] for key in ("passed", "failed")}), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
