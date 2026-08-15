#!/usr/bin/python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#    http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import pytest

import hatch_build


def _make_hook(tmp_path: Path) -> hatch_build.CustomBuildHook:
    hook = object.__new__(hatch_build.CustomBuildHook)
    hook.root = str(tmp_path)
    hook.directory = str(tmp_path / ".hatch")
    hook.target_name = "wheel"
    hook.config = {}
    return hook


def test_initialize_bundles_skills_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = _make_hook(tmp_path)
    bundled_skills_dir = tmp_path / "generated-skills"
    bundled_skills_dir.mkdir()
    generated_version_info = tmp_path / "generated-version.info"
    generated_version_info.write_text("[PACKAGE]\nVersion=26.1.0-alpha.2\n", encoding="utf-8")

    monkeypatch.delenv(hatch_build.ENV_BUNDLE_WEB_UI, raising=False)
    monkeypatch.setattr(
        hatch_build.CustomBuildHook,
        "_ensure_version_info_file",
        lambda self: generated_version_info,
    )
    monkeypatch.setattr(hatch_build.CustomBuildHook, "_ensure_bundled_skills_dir", lambda self: bundled_skills_dir)

    build_data: dict[str, object] = {}
    hook.initialize("0.1.0", build_data)

    assert build_data["force_include"] == {
        str(generated_version_info): "msagent/version.info",
        str(bundled_skills_dir): hatch_build.DEFAULT_SKILLS_TARGET_DIR,
    }


def test_initialize_skips_skills_payload_when_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hook = _make_hook(tmp_path)
    source_archive = tmp_path / hatch_build.DEFAULT_UI_ARCHIVE_NAME
    standalone_archive = tmp_path / hatch_build.DEFAULT_UI_STANDALONE_ARCHIVE_NAME
    generated_version_info = tmp_path / "generated-version.info"
    source_archive.write_bytes(b"archive")
    standalone_archive.write_bytes(b"standalone")
    generated_version_info.write_text("[PACKAGE]\nVersion=26.1.0-alpha.2\n", encoding="utf-8")

    monkeypatch.setenv(hatch_build.ENV_BUNDLE_WEB_UI, "1")
    monkeypatch.setattr(
        hatch_build.CustomBuildHook,
        "_ensure_version_info_file",
        lambda self: generated_version_info,
    )
    monkeypatch.setattr(hatch_build.CustomBuildHook, "_ensure_bundled_skills_dir", lambda self: None)
    monkeypatch.setattr(hatch_build.CustomBuildHook, "_ensure_ui_archive", lambda self: source_archive)
    monkeypatch.setattr(
        hatch_build.CustomBuildHook,
        "_ensure_ui_standalone_archive",
        lambda self, _source_archive: standalone_archive,
    )

    build_data: dict[str, object] = {}
    hook.initialize("0.1.0", build_data)

    assert build_data["force_include"] == {
        str(generated_version_info): "msagent/version.info",
        str(source_archive): "msagent/web/vendor/deep-agents-ui.tar.gz",
        str(standalone_archive): "msagent/web/vendor/deep-agents-ui-standalone.tar.gz",
    }


def test_ensure_version_info_file_uses_repo_version_and_build_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hook = _make_hook(tmp_path)
    repo_version_info = tmp_path / "version.info"
    repo_version_info.write_text(
        "[PACKAGE]\nVersion=26.1.0-alpha.2\nCommit=unknown\nDate=unknown\nRepo=https://gitcode.com/Ascend/msagent\n",
        encoding="utf-8",
    )

    monkeypatch.setenv(hatch_build.ENV_GIT_COMMIT, "bab2dc9")
    monkeypatch.setenv(hatch_build.ENV_BUILD_DATE, "2026-08-15T16:12:09+08:00")
    monkeypatch.setenv(hatch_build.ENV_REPO_URL, "https://gitcode.com/Ascend/msagent")

    generated = hook._ensure_version_info_file()
    content = generated.read_text(encoding="utf-8")

    assert "version = 26.1.0-alpha.2" in content
    assert "commit = bab2dc9" in content
    assert "date = 2026-08-15T16:12:09+08:00" in content
