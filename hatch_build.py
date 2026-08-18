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

import shutil
from pathlib import Path

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - build dependency is not installed in tests

    class BuildHookInterface:  # type: ignore[no-redef]
        def __init__(
            self,
            *args,
            root: str = ".",
            directory: str = ".",
            target_name: str = "",
            config: dict[str, object] | None = None,
            **kwargs,
        ) -> None:
            del args, kwargs
            self.root = root
            self.directory = directory
            self.target_name = target_name
            self.config = config or {}


DEFAULT_SKILLS_SOURCE_DIR_NAME = "skills"
DEFAULT_SKILLS_TARGET_DIR = "resources/configs/default/skills"


class CustomBuildHook(BuildHookInterface):
    def clean(self, versions: list[str]) -> None:
        del versions
        generated_dir = Path(self.directory) / "msagent-build"
        shutil.rmtree(generated_dir, ignore_errors=True)

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        del version
        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise TypeError("build_data.force_include must be a dict[str, str]")

        bundled_skills_dir = self._ensure_bundled_skills_dir()
        if bundled_skills_dir is not None:
            force_include[str(bundled_skills_dir)] = DEFAULT_SKILLS_TARGET_DIR

    def _ensure_bundled_skills_dir(self) -> Path | None:
        if self.target_name != "wheel":
            return None

        source_dir = Path(self.root) / DEFAULT_SKILLS_SOURCE_DIR_NAME
        if not source_dir.is_dir():
            return None

        generated_dir = Path(self.directory) / "msagent-build" / "skills"
        shutil.rmtree(generated_dir, ignore_errors=True)

        def _ignore(_path: str, names: list[str]) -> set[str]:
            ignored = {name for name in names if name in {".git", "__pycache__"}}
            ignored.update(name for name in names if name.endswith((".pyc", ".pyo")))
            return ignored

        shutil.copytree(source_dir, generated_dir, ignore=_ignore)
        return generated_dir
