#!/usr/bin/env python3
"""Small torchrun workload combining CPU training with sustained dataset IO."""

from __future__ import annotations

import argparse
import mmap
import os
from pathlib import Path
import time

import torch
import torch.distributed as dist


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-file", required=True, type=Path)
    parser.add_argument("--duration", type=float, default=90.0)
    args = parser.parse_args()

    dist.init_process_group("gloo")
    rank = dist.get_rank()
    model = torch.nn.Linear(256, 256)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
    data = torch.randn(32, 256)
    target = torch.randn(32, 256)
    fd = os.open(args.data_file, os.O_RDONLY | os.O_DIRECT)
    buffer = mmap.mmap(-1, 4096)
    deadline = time.monotonic() + args.duration
    stop = torch.zeros(1, dtype=torch.int32)
    try:
        while True:
            os.readv(fd, [buffer])
            if os.lseek(fd, 0, os.SEEK_CUR) >= args.data_file.stat().st_size:
                os.lseek(fd, 0, os.SEEK_SET)
            optimizer.zero_grad(set_to_none=True)
            loss = torch.nn.functional.mse_loss(model(data), target)
            loss.backward()
            for parameter in model.parameters():
                dist.all_reduce(parameter.grad)
            optimizer.step()
            if rank == 0 and time.monotonic() >= deadline:
                stop.fill_(1)
            dist.broadcast(stop, src=0)
            if stop.item():
                break
    finally:
        buffer.close()
        os.close(fd)
        dist.destroy_process_group()
    print(f"rank={rank} complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
