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

from pathlib import Path

from msagent.agents.factory import _FilteredSkillsMiddleware
from msagent.skills.factory import DEFAULT_SKILL_CATEGORY, Skill


def test_skill_discovers_scripts(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "analysis" / "demo-skill"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\n", encoding="utf-8")
    (scripts_dir / "run_demo.py").write_text("print('ok')\n", encoding="utf-8")

    skill = Skill(
        name="demo-skill",
        description="demo",
        category=DEFAULT_SKILL_CATEGORY,
        path=skill_dir / "SKILL.md",
    )

    assert skill.get_script_relative_paths() == ["scripts/run_demo.py"]

def test_filtered_skills_middleware_filters_by_skill_name(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "analysis" / "demo-skill"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\n", encoding="utf-8")
    (scripts_dir / "run_demo.py").write_text("print('ok')\n", encoding="utf-8")

    skill = Skill(
        name="demo-skill",
        description="demo description",
        category="analysis",
        path=skill_dir / "SKILL.md",
    )

    middleware = _FilteredSkillsMiddleware(
        backend=None,
        sources=[],
        allowed_skills=[skill],
    )

    skills_metadata = [
        {"name": "demo-skill", "description": "demo description", "path": "/skills/analysis/demo-skill/SKILL.md"},
        {"name": "other-skill", "description": "other description", "path": "/skills/analysis/other-skill/SKILL.md"},
    ]

    filtered = middleware._filter_skills_metadata(skills_metadata)

    assert filtered == [
        {"name": "demo-skill", "description": "demo description", "path": "/skills/analysis/demo-skill/SKILL.md"}
    ]

def test_filtered_skills_middleware_returns_empty_when_no_allowed_skills() -> None:
    middleware = _FilteredSkillsMiddleware(
        backend=None,
        sources=[],
        allowed_skills=[],
    )

    filtered = middleware._filter_skills_metadata(
        [{"name": "demo-skill", "description": "demo"}]
    )

    assert filtered == []