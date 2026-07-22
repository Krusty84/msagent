#!/usr/bin/env python3
"""Run gitleaks when a local or PATH binary is available."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _find_gitleaks(repo_root: Path) -> str | None:
    for candidate in (repo_root / "gitleaks.exe", repo_root / "gitleaks"):
        if candidate.is_file():
            return str(candidate)

    return shutil.which("gitleaks") or shutil.which("gitleaks.exe")


def main() -> int:
    repo_root = _repo_root()
    gitleaks = _find_gitleaks(repo_root)
    if gitleaks is None:
        print(
            "gitleaks binary not found; skipping local secret scan.",
            file=sys.stderr,
        )
        return 0

    completed = subprocess.run([gitleaks, *sys.argv[1:]], cwd=repo_root, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
