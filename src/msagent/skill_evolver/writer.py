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

"""Proposal writer: SKILL.md drafts that no skill scanner can see.

A rendered and validated skill is written to
``<root>/.proposals/<thread>/<name>/SKILL.md`` next to a mandatory
``provenance.json`` (threads, episodes, candidates, model, prompt variants,
detector version, timestamp). :meth:`SkillFactory.load_skills` skips
dot-directories, and the extra ``<thread>`` level keeps the files below the
depth at which the agent's skill sources are enumerated, so a proposal
reaches the library only when a human moves it. Stdlib only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from msagent.skill_evolver.classify import Candidate
from msagent.skill_evolver.features import FEATURES_VERSION, Episode
from msagent.skill_evolver.validator import NAME_RE

PROPOSALS_DIR = ".proposals"
SKILL_FILE = "SKILL.md"
PROVENANCE_FILE = "provenance.json"
REQUIRED_PROVENANCE_KEYS: frozenset[str] = frozenset(
    {
        "thread_ids",
        "episodes",
        "candidates",
        "model",
        "prompt_variants",
        "features_version",
        "generated_at",
    },
)
# Upper bound of the -2, -3, ... suffix search for one proposal name.
MAX_COLLISIONS = 1000
# Folder name of a thread's proposals, after unsafe characters are replaced.
_BATCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_provenance(
    *,
    thread_ids: Sequence[str],
    episodes: Sequence[Episode],
    candidates: Sequence[Candidate],
    model: str,
    prompt_variants: Mapping[str, str],
    category: str,
    target: Mapping[str, Any],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """The JSON record that says where a proposal came from.

    ``thread_ids`` keep their order (the analysed thread first) minus
    duplicates; ``category`` is the library folder a new skill is meant for
    and ``target`` names the skill an update revises. ``generated_at``
    defaults to now (UTC, ISO 8601).
    """
    ordered: list[str] = []
    for thread_id in thread_ids:
        if thread_id not in ordered:
            ordered.append(thread_id)
    episode_rows: list[dict[str, Any]] = []
    for episode in episodes:
        episode_rows.append(
            {
                "kind": episode.kind,
                "weight": episode.weight,
                "evidence_seq": list(episode.evidence_seq),
                "thread_id": episode.thread_id,
            },
        )
    return {
        "thread_ids": ordered,
        "episodes": episode_rows,
        "candidates": [candidate.model_dump() for candidate in candidates],
        "model": model,
        "prompt_variants": dict(prompt_variants),
        "features_version": FEATURES_VERSION,
        "generated_at": generated_at or _utc_now(),
        "category": category,
        "target": dict(target),
    }


def batch_dir_name(thread_id: str) -> str:
    """Folder that groups the proposals of one thread (its id, made path-safe)."""
    name = _UNSAFE_RE.sub("-", thread_id.strip())[:64]
    if not _BATCH_RE.fullmatch(name):
        raise ValueError(f"unsafe thread id for a proposal folder: {thread_id!r}")
    return name


def _reserve_dir(base: Path, name: str) -> Path:
    """Create ``<base>/<name>`` or the first free ``<name>-N``; mkdir is atomic."""
    base.mkdir(parents=True, exist_ok=True)
    for suffix in range(1, MAX_COLLISIONS + 1):
        candidate = base / (name if suffix == 1 else f"{name}-{suffix}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"too many proposals named {name!r} under {base}")


def write_proposal(
    content: str,
    *,
    root: Path,
    name: str,
    provenance: Mapping[str, Any],
    thread_id: str,
) -> Path:
    """Write SKILL.md + provenance.json to ``<root>/.proposals/<thread>/<name>/``.

    Name collisions get ``-2``, ``-3``, ... suffixes. ``provenance`` must carry
    every key of :data:`REQUIRED_PROVENANCE_KEYS` with non-empty
    ``thread_ids`` and ``candidates``; it is written before SKILL.md so a
    skill never exists without it. Returns the SKILL.md path.
    """
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"unsafe proposal name {name!r}")
    batch = batch_dir_name(thread_id)
    missing = sorted(REQUIRED_PROVENANCE_KEYS - set(provenance))
    if missing:
        raise ValueError(f"provenance is missing {missing}")
    if not provenance["thread_ids"] or not provenance["candidates"]:
        raise ValueError("provenance must list thread_ids and candidates")
    payload = json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True)
    skill_dir = _reserve_dir(root / PROPOSALS_DIR / batch, name)
    text = content if content.endswith("\n") else content + "\n"
    provenance_path = skill_dir / PROVENANCE_FILE
    provenance_path.write_text(payload + "\n", encoding="utf-8", newline="\n")
    (skill_dir / SKILL_FILE).write_text(text, encoding="utf-8", newline="\n")
    return skill_dir / SKILL_FILE
