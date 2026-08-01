#!/usr/bin/env python3
"""Run one bounded TorchVision training workload against an on-disk ImageFolder."""

from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Any

import torch
from torch import nn
from torch.utils.data import DataLoader
from torchvision import datasets, models, transforms


MODEL_NAMES = (
    "resnet18",
    "resnet50",
    "mobilenet_v3_small",
    "mobilenet_v3_large",
    "efficientnet_b0",
    "densenet121",
    "shufflenet_v2_x1_0",
    "squeezenet1_1",
    "convnext_tiny",
    "swin_t",
)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
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


def _build_model(name: str, classes: int) -> nn.Module:
    return models.get_model(name, weights=None, num_classes=classes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, choices=MODEL_NAMES)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    if not 5.0 <= args.seconds <= 600.0:
        parser.error("--seconds must be between 5 and 600")
    if not 1 <= args.batch_size <= 512:
        parser.error("--batch-size must be between 1 and 512")
    if not 0 <= args.workers <= 32:
        parser.error("--workers must be between 0 and 32")
    if not torch.cuda.is_available():
        parser.error("CUDA is required for this real workload")

    data_root = args.data_root.resolve()
    if not data_root.is_dir():
        parser.error(f"ImageFolder does not exist: {data_root}")

    random.seed(20260731)
    torch.manual_seed(20260731)
    torch.cuda.manual_seed_all(20260731)
    torch.set_float32_matmul_precision("high")
    torch.backends.cudnn.benchmark = True

    setup_started = time.perf_counter()
    transform = transforms.Compose(
        [
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
        ]
    )
    dataset = datasets.ImageFolder(data_root, transform=transform)
    if len(dataset) < args.batch_size:
        parser.error(
            f"dataset has {len(dataset)} samples, fewer than batch size {args.batch_size}"
        )
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.workers,
        pin_memory=True,
        persistent_workers=args.workers > 0,
        drop_last=True,
    )
    device = torch.device("cuda:0")
    model = _build_model(args.model, len(dataset.classes)).to(device)
    model.train()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    criterion = nn.CrossEntropyLoss()
    parameter_count = sum(parameter.numel() for parameter in model.parameters())

    iterator = iter(loader)

    def next_batch() -> tuple[torch.Tensor, torch.Tensor]:
        nonlocal iterator
        try:
            images, labels = next(iterator)
        except StopIteration:
            iterator = iter(loader)
            images, labels = next(iterator)
        return images, labels

    def train_step() -> tuple[float, int]:
        images, labels = next_batch()
        images = images.to(device, non_blocking=True)
        labels = labels.to(device, non_blocking=True)
        optimizer.zero_grad(set_to_none=True)
        with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            logits = model(images)
            loss = criterion(logits, labels)
        loss.backward()
        optimizer.step()
        return float(loss.detach()), int(images.shape[0])

    warmup_loss, warmup_samples = train_step()
    torch.cuda.synchronize()
    ready_at = time.perf_counter()
    _atomic_json(
        args.ready_file.resolve(),
        {
            "status": "ready",
            "pid": os.getpid(),
            "model": args.model,
            "data_root": str(data_root),
            "setup_seconds": ready_at - setup_started,
        },
    )
    print(
        f"READY model={args.model} pid={os.getpid()} data_root={data_root}",
        flush=True,
    )

    steps = 1
    samples = warmup_samples
    losses = [warmup_loss]
    while time.perf_counter() - ready_at < args.seconds:
        loss, count = train_step()
        steps += 1
        samples += count
        losses.append(loss)
    torch.cuda.synchronize()
    ended_at = time.perf_counter()
    elapsed = ended_at - ready_at
    report = {
        "schema_version": "1.0",
        "status": "PASS",
        "model": args.model,
        "pid": os.getpid(),
        "device": torch.cuda.get_device_name(0),
        "data_root": str(data_root),
        "dataset_samples": len(dataset),
        "classes": len(dataset.classes),
        "parameters": parameter_count,
        "batch_size": args.batch_size,
        "workers": args.workers,
        "requested_seconds": args.seconds,
        "training_seconds": elapsed,
        "steps": steps,
        "samples": samples,
        "samples_per_second": samples / elapsed,
        "first_loss": losses[0],
        "last_loss": losses[-1],
        "max_cuda_memory_bytes": torch.cuda.max_memory_allocated(),
    }
    _atomic_json(args.report.resolve(), report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
