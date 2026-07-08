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

"""Compatibility helpers for deepagents memory file handling."""

from __future__ import annotations

from pathlib import Path

from msagent.core.constants import CONFIG_MEMORY_FILE_NAME

DEFAULT_MEMORY_FILE_CONTENT = """# msagent memory

Store durable user preferences and project facts here.

- 
"""
_DEFAULT_EMPTY_BULLET = "- \n"


def get_memory_file_path(working_dir: Path | None = None) -> Path:
    """Return the canonical memory file path used by deepagents memory middleware."""
    if working_dir is None:
        working_dir = Path.cwd()

    return working_dir / CONFIG_MEMORY_FILE_NAME


def ensure_memory_file(working_dir: Path | None = None) -> Path:
    """Ensure the memory file exists and return its path."""
    memory_path = get_memory_file_path(working_dir)
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    if not memory_path.exists():
        memory_path.write_text(DEFAULT_MEMORY_FILE_CONTENT, encoding="utf-8")
    return memory_path


def append_memory_entry(content: str, working_dir: Path | None = None) -> Path:
    """Append a durable memory entry to the canonical memory file."""
    entry = content.strip()
    if not entry:
        raise ValueError("Memory content cannot be empty")

    memory_path = ensure_memory_file(working_dir)
    try:
        current = memory_path.read_text(encoding="utf-8")
    except Exception:
        current = DEFAULT_MEMORY_FILE_CONTENT

    line = entry if entry.startswith(("- ", "* ")) else f"- {entry}"
    if current == DEFAULT_MEMORY_FILE_CONTENT:
        updated = current.removesuffix(_DEFAULT_EMPTY_BULLET) + f"{line}\n"
    else:
        updated = current.rstrip() + f"\n{line}\n"

    memory_path.write_text(updated, encoding="utf-8")
    return memory_path


def read_memory_file(working_dir: Path | None = None) -> str:
    """Read user memory content from the canonical memory file."""
    memory_path = get_memory_file_path(working_dir)

    if not memory_path.exists():
        return ""

    try:
        return memory_path.read_text(encoding="utf-8")
    except Exception:
        return ""
