"""Paths and safe names for diagnostic artifacts."""

from __future__ import annotations

import re
from pathlib import Path


def safe_case_name(nodeid: str) -> str:
    """Convert a pytest node id into a stable filesystem directory name."""
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", nodeid).strip("._")
    return normalized[:180] or "unknown-test"


def create_case_artifact_dir(run_dir: Path, nodeid: str) -> Path:
    """Create the directory holding every msagent invocation for one case."""
    case_dir = run_dir / "cases" / safe_case_name(nodeid)
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir
