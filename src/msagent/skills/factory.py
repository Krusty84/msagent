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

"""Skills factory for loading SKILL.md metadata from configured directories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from yaml import YAMLError  # type: ignore[import-untyped]

from msagent.core.constants import DEFAULT_CONFIG_DIR

DEFAULT_SKILL_CATEGORY = "default"
SKILL_GROUP_PROFILING = "Profiling"
SKILL_GROUP_DOCUMENTATION = "Documentation and External References"
SKILL_GROUP_MSMODELING = "msmodeling"
SKILL_GROUP_QUANTIZATION = "msmodelslim / Quantization"
SKILL_GROUP_OTHER = "Other"
SKILL_GROUP_ORDER = {
    SKILL_GROUP_PROFILING: 0,
    SKILL_GROUP_DOCUMENTATION: 1,
    SKILL_GROUP_MSMODELING: 2,
    SKILL_GROUP_QUANTIZATION: 3,
    SKILL_GROUP_OTHER: 100,
}

_SKILL_GROUP_ALIASES = {
    "profiling": SKILL_GROUP_PROFILING,
    "document": SKILL_GROUP_DOCUMENTATION,
    "documentation": SKILL_GROUP_DOCUMENTATION,
    "docs": SKILL_GROUP_DOCUMENTATION,
    "reference": SKILL_GROUP_DOCUMENTATION,
    "references": SKILL_GROUP_DOCUMENTATION,
    "github": SKILL_GROUP_DOCUMENTATION,
    "gitcode": SKILL_GROUP_DOCUMENTATION,
    "msmodeling": SKILL_GROUP_MSMODELING,
    "msmodelslim": SKILL_GROUP_QUANTIZATION,
    "quant": SKILL_GROUP_QUANTIZATION,
    "quantization": SKILL_GROUP_QUANTIZATION,
}


def _normalize_skill_token(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _humanize_skill_group(token: str) -> str:
    parts = [part for part in token.replace("-", " ").split() if part]
    if not parts:
        return SKILL_GROUP_OTHER
    return " ".join(part.upper() if part.isupper() else part.capitalize() for part in parts)


def _skill_group_label_from_token(token: str) -> str:
    normalized_token = _normalize_skill_token(token)
    if not normalized_token or normalized_token == DEFAULT_SKILL_CATEGORY:
        return SKILL_GROUP_OTHER
    return _SKILL_GROUP_ALIASES.get(normalized_token, _humanize_skill_group(normalized_token))


def _skill_group_token(name: str, category: str) -> str:
    normalized_category = _normalize_skill_token(category)
    if normalized_category and normalized_category != DEFAULT_SKILL_CATEGORY:
        return normalized_category

    normalized_name = _normalize_skill_token(name)
    if not normalized_name:
        return DEFAULT_SKILL_CATEGORY

    for separator in ("-", "_"):
        if separator in normalized_name:
            return normalized_name.split(separator, 1)[0]
    return normalized_name


def classify_skill_group(
    name: str,
    *,
    category: str = DEFAULT_SKILL_CATEGORY,
) -> str:
    """Map a skill to a stable human-readable group label without hardcoded skill-name lists."""
    return _skill_group_label_from_token(_skill_group_token(name, category))


def skill_group_sort_key(
    name: str,
    *,
    category: str = DEFAULT_SKILL_CATEGORY,
) -> tuple[int, str, str]:
    """Return a stable sort key that groups related skills together."""
    group = classify_skill_group(name, category=category)
    return (
        SKILL_GROUP_ORDER.get(group, SKILL_GROUP_ORDER[SKILL_GROUP_OTHER] - 1),
        group,
        str(name).casefold(),
    )


@dataclass(frozen=True, slots=True)
class Skill:
    """Serializable view of a skill used by CLI and catalog tools."""

    name: str
    description: str
    category: str
    path: Path

    @property
    def root_dir(self) -> Path:
        return self.path.parent

    @property
    def display_name(self) -> str:
        if self.category == DEFAULT_SKILL_CATEGORY:
            return self.name
        return f"{self.category}/{self.name}"

    @property
    def group_label(self) -> str:
        return classify_skill_group(self.name, category=self.category)

    def get_script_relative_paths(self, limit: int = 8) -> list[str]:
        scripts_dir = self.root_dir / "scripts"
        if not scripts_dir.exists():
            return []
        return [
            str(path.relative_to(self.root_dir)).replace("\\", "/")
            for path in sorted(scripts_dir.rglob("*"))
            if path.is_file()
        ][:limit]


class SkillFactory:
    """Loads skills from one or more directories."""

    def __init__(self) -> None:
        self._module_map: dict[str, str] = {}

    async def load_skills(
        self,
        skills_dir: Path | list[Path],
    ) -> dict[str, dict[str, Skill]]:
        directories = [skills_dir] if isinstance(skills_dir, Path) else list(skills_dir)

        loaded: dict[str, dict[str, Skill]] = {}
        self._module_map.clear()

        for directory in directories:
            if not directory.exists():
                continue

            for skill_file in sorted(directory.rglob("SKILL.md")):
                skill = self._load_skill_file(skill_file, base_dir=directory)
                if skill is None:
                    continue

                loaded.setdefault(skill.category, {})
                # First one wins to avoid unstable duplicate ordering.
                loaded[skill.category].setdefault(skill.name, skill)
                self._module_map[f"{skill.category}:{skill.name}"] = skill.category

        return loaded

    def get_module_map(self) -> dict[str, str]:
        return dict(self._module_map)

    @staticmethod
    def get_repo_skills_dir() -> Path:
        return Path(__file__).resolve().parents[3] / "skills"

    @staticmethod
    def get_packaged_skills_dir() -> Path:
        return DEFAULT_CONFIG_DIR / "skills"

    @classmethod
    def get_default_skills_dir(cls) -> Path:
        repo_skills_dir = cls.get_repo_skills_dir()
        if repo_skills_dir.is_dir():
            return repo_skills_dir
        return cls.get_packaged_skills_dir()

    @staticmethod
    def parse_frontmatter(content: str) -> dict[str, object]:
        frontmatter: dict[str, object] = {}
        body = content.lstrip()
        if not body.startswith("---"):
            return frontmatter

        parts = body.split("---", 2)
        if len(parts) < 3:
            raise ValueError("Invalid frontmatter: missing closing delimiter")

        try:
            parsed = yaml.safe_load(parts[1]) or {}
        except YAMLError as exc:
            raise ValueError(f"Invalid frontmatter: {exc}") from exc

        if not isinstance(parsed, dict):
            raise ValueError("Invalid frontmatter: expected a YAML mapping")

        return parsed

    @staticmethod
    def _load_skill_file(skill_file: Path, *, base_dir: Path) -> Skill | None:
        try:
            content = skill_file.read_text(encoding="utf-8")
        except Exception:
            return None

        try:
            frontmatter = SkillFactory.parse_frontmatter(content)
        except ValueError:
            return None

        name = str(frontmatter.get("name") or skill_file.parent.name).strip()
        description = str(frontmatter.get("description") or "").strip()
        if not name:
            return None

        parent = skill_file.parent.parent
        category = parent.name if parent != base_dir and parent.exists() else DEFAULT_SKILL_CATEGORY

        return Skill(
            name=name,
            description=description,
            category=category,
            path=skill_file,
        )
