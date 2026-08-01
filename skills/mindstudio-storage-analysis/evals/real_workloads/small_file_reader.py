#!/usr/bin/env python3
"""Repeatedly open and directly read real small files for R300 live tests."""

from __future__ import annotations

import argparse
import mmap
import os
from pathlib import Path
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--prepare-count", type=int, default=0)
    args = parser.parse_args()

    args.root.mkdir(parents=True, exist_ok=True)
    payload = bytes(4096)
    for index in range(args.prepare_count):
        path = args.root / f"sample-{index:06d}.bin"
        if not path.exists():
            path.write_bytes(payload)
    files = sorted(args.root.glob("*.bin"))
    if not files:
        parser.error(f"no .bin files found under {args.root}")
    deadline = time.monotonic() + args.duration
    buffer = mmap.mmap(-1, 4096)
    try:
        while time.monotonic() < deadline:
            for path in files:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
                try:
                    os.readv(fd, [buffer])
                finally:
                    os.close(fd)
                if time.monotonic() >= deadline:
                    break
    finally:
        buffer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
