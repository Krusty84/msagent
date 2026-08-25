from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from msagent.core.paths import AppPaths
from msagent.core.storage_layout import (
    LEGACY_HOME_ENTRIES,
    STORAGE_LAYOUT_VERSION,
    StorageLayoutError,
    validate_and_initialize_storage_layout,
)


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths.from_home(tmp_path / "home")


def _write_metadata(paths: AppPaths, payload: object) -> None:
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.metadata_file.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("precreate_home", [False, True])
def test_initializes_absent_or_empty_home(tmp_path: Path, precreate_home: bool) -> None:
    paths = _paths(tmp_path)
    if precreate_home:
        paths.home.mkdir()

    validate_and_initialize_storage_layout(paths)

    assert json.loads(paths.metadata_file.read_text(encoding="utf-8")) == {
        "storage_layout_version": STORAGE_LAYOUT_VERSION
    }
    assert paths.config_dir.is_dir()
    assert paths.projects_dir.is_dir()
    assert paths.logs_dir.is_dir()


@pytest.mark.parametrize("relative_dir", ["config", "state/projects"])
def test_backfills_metadata_for_existing_new_layout(tmp_path: Path, relative_dir: str) -> None:
    paths = _paths(tmp_path)
    (paths.home / relative_dir).mkdir(parents=True)

    validate_and_initialize_storage_layout(paths)

    assert paths.metadata_file.is_file()


def test_current_layout_is_idempotent_and_repairs_missing_directories(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_metadata(paths, {"storage_layout_version": STORAGE_LAYOUT_VERSION})

    validate_and_initialize_storage_layout(paths)
    first_content = paths.metadata_file.read_bytes()
    validate_and_initialize_storage_layout(paths)

    assert paths.projects_dir.is_dir()
    assert paths.metadata_file.read_bytes() == first_content


@pytest.mark.parametrize("legacy_entry", LEGACY_HOME_ENTRIES)
def test_rejects_every_legacy_marker_without_writing(tmp_path: Path, legacy_entry: str) -> None:
    paths = _paths(tmp_path)
    marker = paths.home / legacy_entry.rstrip("/")
    if legacy_entry.endswith("/"):
        marker.mkdir(parents=True)
    else:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("legacy", encoding="utf-8")
    before = {entry.name for entry in paths.home.iterdir()}

    with pytest.raises(StorageLayoutError) as caught:
        validate_and_initialize_storage_layout(paths)

    message = str(caught.value)
    assert str(paths.home) in message
    assert legacy_entry in message
    assert {entry.name for entry in paths.home.iterdir()} == before
    assert not paths.metadata_file.exists()


def test_legacy_error_explains_reason_and_recovery_steps(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    marker = paths.home / "config.llms.yml"
    marker.parent.mkdir(parents=True)
    marker.write_text("legacy", encoding="utf-8")

    with pytest.raises(StorageLayoutError) as caught:
        validate_and_initialize_storage_layout(paths)

    message = str(caught.value)
    assert message.startswith("msAgent 启动已停止：发现旧版本配置目录。")
    assert "新版和旧版使用不同的目录结构，旧目录中的配置不会自动迁移。" in message
    assert "如果继续启动，可能会使用默认配置而不是你以前的配置" in message
    assert "1. 重命名旧目录进行备份" in message
    assert f"mv -- {paths.home} {paths.home}.backup" in message
    assert "2. 重新运行 msAgent" in message
    assert "msagent config --show" in message
    assert "  msagent\n" in message
    assert "3. 确认新版配置和运行状态正常后" in message
    assert f"rm -rf -- {paths.home}.backup" in message


def test_legacy_error_explains_how_to_preserve_user_skills(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.home.mkdir(parents=True)
    (paths.home / "config.llms.yml").write_text("legacy", encoding="utf-8")
    paths.skills_dir.mkdir()

    with pytest.raises(StorageLayoutError) as caught:
        validate_and_initialize_storage_layout(paths)

    message = str(caught.value)
    assert f"3. 检查备份目录中的 Skill：\n\n  {paths.home}.backup/skills" in message
    assert "程序无法自动区分" in message
    assert "新版内置 Skill 由安装包提供，无需恢复" in message
    assert "请只复制你自己添加的 Skill" in message
    assert "不要恢复整个 skills 目录" in message
    assert "4. 确认新版配置和运行状态正常后" in message


@pytest.mark.parametrize("shared_dir", ["skills", "prompts", "cache", "oauth", "logs"])
def test_shared_directory_alone_is_not_legacy(tmp_path: Path, shared_dir: str) -> None:
    paths = _paths(tmp_path)
    (paths.home / shared_dir).mkdir(parents=True)

    validate_and_initialize_storage_layout(paths)

    assert paths.metadata_file.is_file()


def test_rejects_mixed_layout_without_writing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    legacy = paths.home / "config.llms.yml"
    legacy.write_text("legacy", encoding="utf-8")

    with pytest.raises(StorageLayoutError, match="检测到新旧版本配置混用") as caught:
        validate_and_initialize_storage_layout(paths)

    message = str(caught.value)
    assert "config.llms.yml" in message
    assert "方案一：删除整个目录并重新初始化" in message
    assert "方案二：保留新版数据，只清理旧版残留" in message
    assert f"rm -rf -- {paths.home}" in message
    assert f"(cd {paths.home} && mv -- config.llms.yml" in message
    assert "1. 删除整个目录" in message
    assert "2. 重新运行 msAgent" in message
    assert "1. 创建备份目录" in message
    assert "2. 将检测到的旧版内容移到备份目录" in message
    assert "3. 处理完成后重新运行" in message
    assert "Skill 和用户 Skill" not in message
    assert not paths.metadata_file.exists()
    assert not paths.logs_dir.exists()


def test_mixed_layout_warning_lists_all_legacy_entries_and_skills_directory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.skills_dir.mkdir()
    (paths.home / "config.llms.yml").write_text("legacy", encoding="utf-8")
    (paths.home / "agents").mkdir()

    with pytest.raises(StorageLayoutError) as caught:
        validate_and_initialize_storage_layout(paths)

    message = str(caught.value)
    assert "config.llms.yml, agents/" in message
    assert "mv -- config.llms.yml agents" in message
    assert f"3. 检查 {paths.skills_dir}" in message
    assert "skills 目录由新旧版本共用，因此没有列入上面的旧版残留" in message
    assert "程序无法自动区分" in message
    assert "新版内置 Skill 由安装包提供，无需保留" in message
    assert "请只保留你自己添加的 Skill" in message
    assert "4. 处理完成后重新运行" in message


def test_legacy_and_mixed_errors_use_powershell_commands_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("msagent.core.storage_layout.platform.system", lambda: "Windows")

    legacy_paths = AppPaths.from_home(tmp_path / "legacy home")
    legacy_paths.home.mkdir(parents=True)
    (legacy_paths.home / "config.llms.yml").write_text("legacy", encoding="utf-8")
    with pytest.raises(StorageLayoutError) as legacy_error:
        validate_and_initialize_storage_layout(legacy_paths)

    legacy_message = str(legacy_error.value)
    assert "Move-Item -LiteralPath" in legacy_message
    assert "Remove-Item -LiteralPath" in legacy_message
    assert "mv --" not in legacy_message
    assert "rm -rf" not in legacy_message

    mixed_paths = AppPaths.from_home(tmp_path / "mixed home")
    mixed_paths.config_dir.mkdir(parents=True)
    (mixed_paths.home / "config.llms.yml").write_text("legacy", encoding="utf-8")
    with pytest.raises(StorageLayoutError) as mixed_error:
        validate_and_initialize_storage_layout(mixed_paths)

    mixed_message = str(mixed_error.value)
    assert "Remove-Item -LiteralPath" in mixed_message
    assert "$backupDir" in mixed_message
    assert "New-Item -ItemType Directory" in mixed_message
    assert "Move-Item -LiteralPath (Join-Path" in mixed_message
    assert "rm -rf" not in mixed_message


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"storage_layout_version": True},
        {"storage_layout_version": "2"},
        {"storage_layout_version": 0},
        {"storage_layout_version": -1},
    ],
)
def test_rejects_invalid_metadata_without_overwriting(tmp_path: Path, payload: object) -> None:
    paths = _paths(tmp_path)
    _write_metadata(paths, payload)
    original = paths.metadata_file.read_bytes()

    with pytest.raises(StorageLayoutError):
        validate_and_initialize_storage_layout(paths)

    assert paths.metadata_file.read_bytes() == original
    assert not paths.logs_dir.exists()


def test_rejects_invalid_json_without_overwriting(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.home.mkdir()
    paths.metadata_file.write_text("{broken", encoding="utf-8")

    with pytest.raises(StorageLayoutError, match="valid msAgent storage metadata"):
        validate_and_initialize_storage_layout(paths)

    assert paths.metadata_file.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("version", [1, STORAGE_LAYOUT_VERSION + 1])
def test_rejects_unsupported_layout_versions(tmp_path: Path, version: int) -> None:
    paths = _paths(tmp_path)
    _write_metadata(paths, {"storage_layout_version": version})

    with pytest.raises(StorageLayoutError, match=f"version {version}"):
        validate_and_initialize_storage_layout(paths)

    assert json.loads(paths.metadata_file.read_text(encoding="utf-8"))["storage_layout_version"] == version


def test_rejects_home_file_and_metadata_directory(tmp_path: Path) -> None:
    file_paths = _paths(tmp_path)
    file_paths.home.write_text("not a directory", encoding="utf-8")
    with pytest.raises(StorageLayoutError, match="home must be a directory"):
        validate_and_initialize_storage_layout(file_paths)

    directory_paths = AppPaths.from_home(tmp_path / "other-home")
    directory_paths.metadata_file.mkdir(parents=True)
    with pytest.raises(StorageLayoutError, match="metadata must be a regular file"):
        validate_and_initialize_storage_layout(directory_paths)


def test_rejects_symlink_for_managed_directory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    paths.home.mkdir()
    paths.config_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(StorageLayoutError, match="regular directory"):
        validate_and_initialize_storage_layout(paths)

    assert not paths.metadata_file.exists()
    assert not (external / "config.llms.yml").exists()


def test_concurrent_initialization_produces_complete_metadata(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(validate_and_initialize_storage_layout, paths) for _ in range(8)]
        for future in futures:
            future.result()

    assert json.loads(paths.metadata_file.read_text(encoding="utf-8")) == {
        "storage_layout_version": STORAGE_LAYOUT_VERSION
    }
    assert not list(paths.home.glob(".metadata.json.*.tmp"))


def test_metadata_write_failure_leaves_no_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)

    def fail_replace(_source: Path, _target: Path) -> Path:
        raise PermissionError("read-only storage")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(StorageLayoutError, match="Cannot write.*read-only storage"):
        validate_and_initialize_storage_layout(paths)

    assert not paths.metadata_file.exists()
    assert not list(paths.home.glob(".metadata.json.*.tmp"))
