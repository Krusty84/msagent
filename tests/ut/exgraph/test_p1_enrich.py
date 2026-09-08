#!/usr/bin/python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the Mulan PSL v2 at:
#
#    http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------


"""P1: episodes, FIXED_BY, overlay recipes, evolver pool isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from msagent.exgraph.config import ENV_DISABLED, ENV_ENABLED, reset_config_cache


@pytest.fixture(autouse=True)
def _exgraph_opt_in(monkeypatch):
    """Exgraph tests run opted-in. Product default remains off."""
    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.delenv(ENV_DISABLED, raising=False)
    reset_config_cache()
    yield
    reset_config_cache()


from msagent.exgraph.cases import build_from_path
from msagent.exgraph.enrich import enrich_graph
from msagent.exgraph.schema import case_id, recipe_id
from msagent.exgraph.workspace import load_overlay, rebuild_overlay
from msagent.skill_evolver.features import extract_episodes
from msagent.trajectory_recorder.reader import load_trajectory

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "trajectories"
SIGNALS = FIXTURES / "skill_evolver_signals.jsonl"


def test_episodes_come_from_features_not_a_fork() -> None:
    trajectory = load_trajectory(SIGNALS)
    graph = build_from_path(SIGNALS)
    enrich_graph(graph, trajectory)
    kinds = sorted(node.attrs["kind"] for node in graph.nodes.values() if node.type == "Episode")
    detected = sorted(episode.kind for episode in extract_episodes(trajectory))
    assert kinds == detected
    assert "error_recovery" in kinds
    assert "user_correction" in kinds


def test_fixed_by_user_correction_and_recovery() -> None:
    trajectory = load_trajectory(SIGNALS)
    graph = build_from_path(SIGNALS)
    enrich_graph(graph, trajectory)
    fixes = [edge for edge in graph.edges.values() if edge.type == "FIXED_BY"]
    vias = {edge.attrs.get("via") for edge in fixes}
    assert "user_correction" in vias
    assert "error_recovery" in vias
    assert any(
        edge.src == case_id("run-1") and edge.dst == case_id("run-2")
        for edge in fixes
        if edge.attrs.get("via") == "user_correction"
    )


def test_overlay_does_not_instantiate_other_agents(tmp_path: Path) -> None:
    """Accuracy ``read_file>grep`` must not attach to a Profiler recipe."""
    signals = load_trajectory(SIGNALS)
    reuse = load_trajectory(FIXTURES / "exgraph_reuse.jsonl")
    accuracy = load_trajectory(FIXTURES / "exgraph_accuracy.jsonl")
    graphs = [
        build_from_path(SIGNALS),
        build_from_path(FIXTURES / "exgraph_reuse.jsonl"),
        build_from_path(FIXTURES / "exgraph_accuracy.jsonl"),
    ]
    for graph, traj in zip(graphs, (signals, reuse, accuracy)):
        enrich_graph(graph, traj)
    rebuild_overlay([signals, reuse, accuracy], graphs, tmp_path)
    _nodes, edges = load_overlay(tmp_path)
    owners = {row.get("src") for row in edges if row.get("type") == "INSTANTIATES"}
    assert "case:run-a1" not in owners


def test_overlay_uses_mine_cross_session(tmp_path: Path) -> None:
    first = load_trajectory(SIGNALS)
    second = load_trajectory(FIXTURES / "normal_subagent.jsonl")
    graphs = [build_from_path(SIGNALS), build_from_path(FIXTURES / "normal_subagent.jsonl")]
    for graph, traj in zip(graphs, (first, second)):
        enrich_graph(graph, traj)
    overlay = rebuild_overlay([first, second], graphs, tmp_path)
    assert (overlay / "manifest.json").is_file()


def test_evolver_pool_limit_unchanged() -> None:
    mining = (REPO_ROOT / "src/msagent/skill_evolver/mining.py").read_text(encoding="utf-8")
    direct = (REPO_ROOT / "src/msagent/skill_evolver/direct_skill_generation.py").read_text(encoding="utf-8")
    assert "CROSS_SESSION_LIMIT = 20" in direct
    assert "pool = listing[:CROSS_SESSION_LIMIT]" in mining
    assert "select_trajectories(" in mining
