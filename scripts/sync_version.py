#!/usr/bin/env python3
"""Sync package version across pyproject.toml and uv.lock."""

from __future__ import annotations

import configparser
import os
import re
import sys
from pathlib import Path


PACKAGE_NAME = "mindstudio-agent"


def _resolve_version(repo_root: Path) -> str:
    raw = os.getenv("WHL_VERSION", "").strip()
    if raw:
        return raw

    parser = configparser.ConfigParser()
    version_info_path = repo_root / "version.info"
    parser.read(version_info_path, encoding="utf-8")
    try:
        package_name = parser.get("PACKAGE", "Name").strip()
        version = parser.get("PACKAGE", "Version").strip()
    except (configparser.Error, KeyError) as exc:
        raise SystemExit(f"Failed to parse {version_info_path}") from exc

    if package_name != PACKAGE_NAME:
        raise SystemExit(
            f"version.info package name mismatch: expected {PACKAGE_NAME!r}, got {package_name!r}"
        )
    if not version:
        raise SystemExit("Resolved version is empty")
    return version


def _update_pyproject(pyproject_path: Path, version: str) -> None:
    content = pyproject_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'(?m)^(version\s*=\s*")([^"]+)(")$',
        rf"\g<1>{version}\g<3>",
        content,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"Could not update version in {pyproject_path}")
    if updated != content:
        pyproject_path.write_text(updated, encoding="utf-8")


def _update_uv_lock(uv_lock_path: Path, version: str) -> None:
    content = uv_lock_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf'(\[\[package\]\]\nname = "{re.escape(PACKAGE_NAME)}"\nversion = ")([^"]+)(")',
        re.MULTILINE,
    )
    updated, count = pattern.subn(rf"\g<1>{version}\g<3>", content, count=1)
    if count != 1:
        raise SystemExit(f"Could not update root package version in {uv_lock_path}")
    if updated != content:
        uv_lock_path.write_text(updated, encoding="utf-8")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    version = _resolve_version(repo_root)

    pyproject_path = repo_root / "pyproject.toml"
    uv_lock_path = repo_root / "uv.lock"

    _update_pyproject(pyproject_path, version)
    _update_uv_lock(uv_lock_path, version)

    print(f"Synced package version to {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
