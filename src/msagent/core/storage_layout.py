"""Validation and initialization for the global msAgent storage layout."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from msagent.core.paths import AppPaths

STORAGE_LAYOUT_VERSION = 2

LEGACY_HOME_ENTRIES = (
    "config.llms.yml",
    "config.mcp.json",
    "config.approval.json",
    "config.agents.yml",
    "config.subagents.yml",
    "config.checkpointers.yml",
    "config.checkpoints.db",
    "langgraph.json",
    ".history",
    "memory.md",
    "agents/",
    "subagents/",
    "llms/",
    "checkpointers/",
    "sandboxes/",
)

_MANAGED_DIRECTORIES = (
    "config",
    "prompts",
    "skills",
    "state",
    "state/projects",
    "cache",
    "cache/mcp",
    "cache/sandboxes",
    "oauth",
    "oauth/mcp",
    "logs",
)


class StorageLayoutError(RuntimeError):
    """Raised when the global storage directory cannot be used safely."""


def validate_and_initialize_storage_layout(paths: AppPaths) -> None:
    """Validate the configured home and initialize storage layout version 2."""
    _inspect_storage_layout(paths)
    # Inspect again immediately before the first write to narrow the race with
    # another process or an external tool changing the directory.
    metadata_version = _inspect_storage_layout(paths)
    _ensure_layout_directories(paths)
    if metadata_version is not None:
        return

    if _path_present(paths.metadata_file):
        _read_metadata_version(paths.metadata_file, {paths.metadata_file.name})
        return
    _write_metadata_atomically(paths.metadata_file)


def _inspect_storage_layout(paths: AppPaths) -> int | None:
    entries = _read_home_entries(paths.home)
    metadata_version = _read_metadata_version(paths.metadata_file, entries)
    legacy_entries = _find_legacy_entries(entries)
    new_entries = _find_new_entries(paths, entries, metadata_version)
    if legacy_entries:
        skills_present = "skills" in entries
        if new_entries:
            raise StorageLayoutError(_mixed_layout_message(paths.home, legacy_entries, skills_present=skills_present))
        raise StorageLayoutError(_legacy_layout_message(paths.home))

    _validate_managed_directories(paths.home)
    return metadata_version


def _ensure_layout_directories(paths: AppPaths) -> None:
    try:
        paths.ensure()
    except OSError as exc:
        raise StorageLayoutError(f"Cannot initialize msAgent storage at {paths.home}: {exc}") from exc


def _read_home_entries(home: Path) -> set[str]:
    if not _path_present(home):
        return set()
    if home.is_symlink() or not home.is_dir():
        raise StorageLayoutError(f"msAgent home must be a directory: {home}")
    try:
        return {entry.name for entry in home.iterdir()}
    except OSError as exc:
        raise StorageLayoutError(f"Cannot inspect msAgent home {home}: {exc}") from exc


def _read_metadata_version(metadata_file: Path, entries: set[str]) -> int | None:
    if metadata_file.name not in entries:
        return None
    if metadata_file.is_symlink() or not metadata_file.is_file():
        raise StorageLayoutError(f"msAgent storage metadata must be a regular file: {metadata_file}")
    try:
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageLayoutError(
            f"Cannot read valid msAgent storage metadata from {metadata_file}: {exc}. "
            "Back up the file and repair or remove the storage directory."
        ) from exc
    if not isinstance(payload, dict):
        raise StorageLayoutError(f"msAgent storage metadata must contain a JSON object: {metadata_file}")

    version = payload.get("storage_layout_version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise StorageLayoutError(f"Invalid storage_layout_version in {metadata_file}; expected a positive integer.")
    if version < STORAGE_LAYOUT_VERSION:
        raise StorageLayoutError(
            f"Storage layout version {version} at {metadata_file.parent} is no longer supported. "
            "Back up and move the directory, then run msAgent again."
        )
    if version > STORAGE_LAYOUT_VERSION:
        raise StorageLayoutError(
            f"Storage layout version {version} at {metadata_file.parent} is newer than this msAgent "
            "version supports. Upgrade msAgent before using this directory."
        )
    return version


def _find_legacy_entries(entries: set[str]) -> tuple[str, ...]:
    return tuple(label for label in LEGACY_HOME_ENTRIES if label.rstrip("/") in entries)


def _find_new_entries(paths: AppPaths, entries: set[str], metadata_version: int | None) -> tuple[str, ...]:
    found: list[str] = []
    if metadata_version == STORAGE_LAYOUT_VERSION:
        found.append("metadata.json")
    if "config" in entries and paths.config_dir.is_dir():
        found.append("config/")
    if paths.projects_dir.is_dir():
        found.append("state/projects/")
    return tuple(found)


def _validate_managed_directories(home: Path) -> None:
    for relative_path in _MANAGED_DIRECTORIES:
        path = home / relative_path
        if not _path_present(path):
            continue
        if path.is_symlink() or not path.is_dir():
            raise StorageLayoutError(f"msAgent storage path must be a regular directory: {path}")


def _write_metadata_atomically(metadata_file: Path) -> None:
    temp_file = metadata_file.with_name(f".{metadata_file.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = {"storage_layout_version": STORAGE_LAYOUT_VERSION}
    try:
        descriptor = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp_file.replace(metadata_file)
    except OSError as exc:
        raise StorageLayoutError(f"Cannot write msAgent storage metadata {metadata_file}: {exc}") from exc
    finally:
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _legacy_layout_message(home: Path) -> str:
    return (
        "msAgent 启动已停止：发现旧版本配置目录。\n\n"
        "新版 msAgent 无法使用以下旧版本目录：\n\n"
        f"  {home}\n\n"
        "请手动删除整个目录，然后重新启动 msAgent。\n\n"
        "本次启动未修改该目录中的任何文件。"
    )


def _mixed_layout_message(
    home: Path,
    legacy_entries: tuple[str, ...],
    *,
    skills_present: bool,
) -> str:
    legacy_summary = ", ".join(legacy_entries)
    skills_note = _skills_backup_note(home) if skills_present else ""

    return (
        "msAgent 启动已停止：检测到新旧版本配置混用。\n\n"
        f"目录：\n\n  {home}\n\n"
        "该目录已经包含新版配置和项目状态，但仍存在旧版残留：\n\n"
        f"  {legacy_summary}\n\n"
        "要继续使用新版 msAgent，建议手动删除：\n\n"
        f"  {home}\n\n"
        "删除会清除其中的新版配置、项目状态、对话历史和检查点。"
        "如有需要保留的数据，请在删除前自行备份。\n"
        f"{skills_note}"
        "删除完成后，请重新启动 msAgent；程序会自动创建新版目录。\n\n"
        "本次启动未修改该目录中的任何文件。\n"
    )


def _skills_backup_note(home: Path) -> str:
    return (
        f"\n特别注意：{home / 'skills'} 可能包含你自己添加的 Skill。\n"
        "如需保留，请在删除前单独备份这些用户 Skill；"
        "新版内置 Skill 由安装包提供，无需备份或恢复。\n"
    )
