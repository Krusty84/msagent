"""Locate the msagent state directory that holds a project's trajectories.

msagent stores per-project state under ``$MSAGENT_HOME/state/projects/<id>``
where the id is ``<sanitized-dir-name>-<sha256(normcase(abs path))[:12]>``.
Getting that wrong means silently finding no data, so the real implementation in
``msagent.core.paths`` is preferred whenever it is importable. The local
fallback mirrors it exactly and keeps this tool usable on a machine that only
has copied log directories.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


def _fallback_home() -> Path:
    configured = os.environ.get("MSAGENT_HOME", "").strip()
    home = Path(configured).expanduser() if configured else Path.home() / ".msagent"
    return home.resolve()


def _fallback_project_id(working_dir: Path) -> str:
    canonical = working_dir.expanduser().resolve()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", canonical.name).strip("-.") or "project"
    digest = hashlib.sha256(os.path.normcase(str(canonical)).encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


@dataclass(frozen=True, slots=True)
class ProjectLocation:
    """Filesystem layout of one msagent project's recorded state."""

    root: Path
    used_msagent: bool

    @property
    def checkpoints_db(self) -> Path:
        return self.root / "checkpoints.sqlite"

    @property
    def audit_dir(self) -> Path:
        return self.root / "audit_log"

    @property
    def conversation_history_dir(self) -> Path:
        return self.root / "conversation_history"

    @property
    def memory_file(self) -> Path:
        return self.root / "memory.md"


def resolve_project(working_dir: Path, *, home: Path | None = None) -> ProjectLocation:
    """Return the state directory msagent uses for ``working_dir``."""
    try:
        from msagent.core.paths import AppPaths

        app_paths = AppPaths.from_home(home) if home is not None else AppPaths.resolve()
        return ProjectLocation(root=app_paths.for_project(working_dir).root, used_msagent=True)
    except Exception:  # noqa: BLE001 - any failure here just means the local layout is used
        base = home.expanduser().resolve() if home is not None else _fallback_home()
        root = base / "state" / "projects" / _fallback_project_id(working_dir)
        return ProjectLocation(root=root, used_msagent=False)
