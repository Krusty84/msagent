#!/usr/bin/env python3
"""Safely copy the framework-neutral profiler adapter into a target repository."""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

ASSET = Path(__file__).resolve().parent.parent / "assets" / "profiler_adapter.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def destination_path(root: Path, destination: str) -> Path:
    root = root.resolve()
    target = (root / destination).resolve()
    if target == root or root not in target.parents:
        raise ValueError("destination must be a file inside the target repository")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--destination", default="profiler_adapter.py")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.repository.is_dir():
        parser.error(f"repository does not exist: {args.repository}")
    try:
        target = destination_path(args.repository, args.destination)
    except ValueError as exc:
        parser.error(str(exc))
    source_hash = sha256(ASSET)

    if target.exists():
        if not target.is_file():
            parser.error(f"destination is not a file: {target}")
        if sha256(target) == source_hash:
            print(f"unchanged {target} sha256={source_hash}")
            return 0
        parser.error(f"refusing to overwrite different file: {target}")

    action = "would-create" if args.dry_run else "created"
    if not args.dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ASSET, target)
    print(f"{action} {target} sha256={source_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
