#!/usr/bin/env python3
"""Generate sustained read IO with stable PIDs for real integration tests."""

from __future__ import annotations

import argparse
import json
import mmap
import os
from pathlib import Path
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--block-size", type=int, default=1024 * 1024)
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--sleep-per-read", type=float, default=0.0)
    args = parser.parse_args()

    started = time.monotonic()
    deadline = time.monotonic() + args.duration
    bytes_read = 0
    flags = os.O_RDONLY | (os.O_DIRECT if args.direct else 0)
    fd = os.open(args.path, flags)
    buffer = mmap.mmap(-1, args.block_size) if args.direct else None
    try:
        while time.monotonic() < deadline:
            os.lseek(fd, 0, os.SEEK_SET)
            while time.monotonic() < deadline:
                data_read = (
                    os.readv(fd, [buffer])
                    if buffer
                    else len(os.read(fd, args.block_size))
                )
                if not data_read:
                    break
                bytes_read += data_read
                if args.sleep_per_read > 0:
                    time.sleep(args.sleep_per_read)
            if not args.direct and hasattr(os, "posix_fadvise"):
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        if buffer is not None:
            buffer.close()
        os.close(fd)
    elapsed = time.monotonic() - started
    if args.report:
        args.report.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "bytes_read": bytes_read,
                    "elapsed_seconds": elapsed,
                    "mib_per_second": bytes_read / elapsed / 1024 / 1024,
                    "block_size": args.block_size,
                    "direct": args.direct,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
