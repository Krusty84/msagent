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
    assert "请手动删除整个目录，然后重新启动 msAgent" in message
    assert {entry.name for entry in paths.home.iterdir()} == before
    assert not paths.metadata_file.exists()


def test_legacy_error_requires_removing_the_whole_directory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    marker = paths.home / "config.llms.yml"
    marker.parent.mkdir(parents=True)
    marker.write_text("legacy", encoding="utf-8")

    with pytest.raises(StorageLayoutError) as caught:
        validate_and_initialize_storage_layout(paths)

    message = str(caught.value)
    assert message.startswith("msAgent 启动已停止：发现旧版本配置目录。")
    assert "新版 msAgent 无法使用以下旧版本目录" in message
    assert str(paths.home) in message
    assert "请手动删除整个目录，然后重新启动 msAgent" in message
    assert "本次启动未修改该目录中的任何文件" in message
    _assert_no_shell_commands(message)


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
    assert "要继续使用新版 msAgent，建议手动删除" in message
    assert str(paths.home) in message
    assert "删除会清除其中的新版配置、项目状态、对话历史和检查点" in message
    assert "如有需要保留的数据，请在删除前自行备份" in message
    assert "删除完成后，请重新启动 msAgent；程序会自动创建新版目录" in message
    assert "本次启动未修改该目录中的任何文件" in message
    _assert_no_shell_commands(message)
    assert not paths.metadata_file.exists()
    assert not paths.logs_dir.exists()


def test_mixed_layout_warning_lists_all_legacy_entries_and_user_skills_risk(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.skills_dir.mkdir()
    (paths.home / "config.llms.yml").write_text("legacy", encoding="utf-8")
    (paths.home / "agents").mkdir()

    with pytest.raises(StorageLayoutError) as caught:
        validate_and_initialize_storage_layout(paths)

    message = str(caught.value)
    assert "config.llms.yml, agents/" in message
    assert f"特别注意：{paths.skills_dir} 可能包含你自己添加的 Skill" in message
    assert "请在删除前单独备份这些用户 Skill" in message
    assert "新版内置 Skill 由安装包提供，无需备份或恢复" in message


def _assert_no_shell_commands(message: str) -> None:
    for command in ("rm -rf", "mv --", "Remove-Item", "Move-Item", "New-Item", "$backupDir"):
        assert command not in message


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
