"""Validation and initialization for the global msAgent storage layout."""

from __future__ import annotations

import json
import os
import shlex
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
        if new_entries:
            raise StorageLayoutError(_mixed_layout_message(paths.home, new_entries, legacy_entries))
        raise StorageLayoutError(_legacy_layout_message(paths.home, legacy_entries))

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
    if type(version) is not int or version <= 0:
        raise StorageLayoutError(
            f"Invalid storage_layout_version in {metadata_file}; expected a positive integer."
        )
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


def _legacy_layout_message(home: Path, legacy_entries: tuple[str, ...]) -> str:
    found = "\n  ".join(legacy_entries)
    home_text = shlex.quote(str(home))
    backup_text = shlex.quote(f"{home}.backup")
    return (
        "msAgent 启动已停止：发现旧版本配置目录。\n\n"
        "你当前使用的是新版 msAgent，但系统中仍存在旧版本目录：\n\n"
        f"  {home}\n\n"
        "新版和旧版使用不同的目录结构，旧目录中的配置不会自动迁移。\n"
        "如果继续启动，可能会使用默认配置而不是你以前的配置，因此程序没有继续运行。\n\n"
        "检测到的旧版内容：\n\n"
        f"  {found}\n\n"
        "本次未修改旧目录中的任何文件。请先重命名旧目录进行备份：\n\n"
        f"  mv {home_text} {backup_text}\n\n"
        "然后重新运行 msAgent：\n\n"
        "  msagent config --show\n\n"
        "或者直接启动：\n\n"
        "  msagent\n\n"
        "确认新版配置和运行状态正常后，如果不再需要旧数据，可以删除备份目录：\n\n"
        f"  rm -rf {backup_text}"
    )


def _mixed_layout_message(
    home: Path,
    new_entries: tuple[str, ...],
    legacy_entries: tuple[str, ...],
) -> str:
    return (
        f"Mixed old and new msAgent storage layouts detected at {home}.\n"
        f"New layout entries: {', '.join(new_entries)}\n"
        f"Legacy entries: {', '.join(legacy_entries)}\n"
        "No files were changed. Back up the directory and review its contents before manually cleaning "
        "or rebuilding it; deleting the whole directory may lose new project state."
    )
