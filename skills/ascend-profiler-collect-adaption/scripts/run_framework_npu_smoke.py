#!/usr/bin/env python3
"""Run tiny real-NPU PTA profiles for Transformers, Accelerate, and Diffusers."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path

ADAPTER_PATH = Path(__file__).resolve().parent.parent / "assets" / "profiler_adapter.py"


def load_adapter():
    spec = importlib.util.spec_from_file_location("profiler_adapter", ADAPTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load adapter: {ADAPTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def transformers_workload() -> Callable[[], float]:
    import torch
    from transformers import BertConfig, BertModel

    torch.manual_seed(11)
    model = (
        BertModel(
            BertConfig(
                vocab_size=64,
                hidden_size=16,
                num_hidden_layers=1,
                num_attention_heads=2,
                intermediate_size=32,
            )
        )
        .eval()
        .npu()
    )
    input_ids = torch.arange(16, device="npu").reshape(1, 16) % 64

    def run() -> float:
        with torch.no_grad():
            output = model(input_ids=input_ids).last_hidden_state
        return float(output.sum().cpu())

    return run


def accelerate_workload() -> Callable[[], float]:
    import torch
    from accelerate import Accelerator

    torch.manual_seed(13)
    accelerator = Accelerator()
    model = torch.nn.Sequential(
        torch.nn.Linear(16, 16), torch.nn.GELU(), torch.nn.Linear(16, 4)
    )
    model = accelerator.prepare(model).eval()
    sample = torch.arange(32, dtype=torch.float32).reshape(2, 16).to(accelerator.device)

    def run() -> float:
        with torch.no_grad():
            output = model(sample)
        return float(output.sum().cpu())

    return run


def diffusers_workload() -> Callable[[], float]:
    import torch
    from diffusers import UNet2DModel

    torch.manual_seed(17)
    model = (
        UNet2DModel(
            sample_size=8,
            in_channels=3,
            out_channels=3,
            layers_per_block=1,
            block_out_channels=(16,),
            down_block_types=("DownBlock2D",),
            up_block_types=("UpBlock2D",),
            norm_num_groups=4,
        )
        .eval()
        .npu()
    )
    sample = torch.ones((1, 3, 8, 8), device="npu")
    timestep = torch.tensor([1], device="npu")

    def run() -> float:
        with torch.no_grad():
            output = model(sample, timestep).sample
        return float(output.sum().cpu())

    return run


WORKLOADS = {
    "transformers": transformers_workload,
    "accelerate": accelerate_workload,
    "diffusers": diffusers_workload,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("framework", choices=tuple(WORKLOADS))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--steps", type=int, default=2)
    args = parser.parse_args()
    if args.steps < 2:
        parser.error("--steps must be at least 2")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error(f"output directory must be absent or empty: {args.output_dir}")

    import torch
    import torch_npu  # noqa: F401

    adapter = load_adapter()
    config = adapter.ProfilerConfig(
        enabled=True,
        output_dir=str(args.output_dir),
        active=args.steps,
        export_type="text",
        level="level0",
        with_cpu=True,
        worker_name=args.framework,
    )
    adapter.validate_step_budget(config, args.steps)
    workload = WORKLOADS[args.framework]()
    disabled_output = args.output_dir / "disabled-must-not-exist"
    baseline_config = adapter.ProfilerConfig(output_dir=str(disabled_output))
    baseline_checksums: list[float] = []
    with adapter.ProfilerController(baseline_config) as controller:
        for _ in range(args.steps):
            baseline_checksums.append(workload())
            controller.step()
    if disabled_output.exists():
        raise RuntimeError("disabled profiler unexpectedly created an output directory")

    checksums: list[float] = []
    with adapter.ProfilerController(config) as controller:
        for _ in range(args.steps):
            checksums.append(workload())
            controller.step()
    torch.npu.synchronize()
    if not all(abs(value - checksums[0]) < 1e-4 for value in checksums[1:]):
        raise RuntimeError(f"unstable model outputs: {checksums}")
    if not all(
        abs(value - baseline_checksums[0]) < 1e-4 for value in baseline_checksums[1:]
    ):
        raise RuntimeError(f"unstable baseline model outputs: {baseline_checksums}")
    if abs(checksums[0] - baseline_checksums[0]) >= 1e-4:
        raise RuntimeError(
            f"profiler changed model output: baseline={baseline_checksums[0]}, profiled={checksums[0]}"
        )
    print(
        json.dumps(
            {
                "framework": args.framework,
                "steps": args.steps,
                "baseline_checksum": baseline_checksums[0],
                "profiled_checksum": checksums[0],
                "outputs_match": True,
                "output_dir": str(args.output_dir.resolve()),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
