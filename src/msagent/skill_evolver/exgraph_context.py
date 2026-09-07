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

"""Fail-open bridge: stored experience graph → classify bundle appendix.

The last-N trajectory pool (``CROSS_SESSION_LIMIT`` / ``select_trajectories``)
is untouched. This module never calls ``load_trajectories``. If the graph
package is missing or a shard cannot be written, the original bundle is
returned unchanged so skill mining keeps its current behaviour.
"""

from __future__ import annotations

import logging
from pathlib import Path

from msagent.trajectory_recorder.model import Trajectory

logger = logging.getLogger(__name__)


def attach_stored_graph(
    bundle_text: str,
    trajectory: Trajectory,
    *,
    working_dir: Path | None = None,
    state_dir: Path | None = None,
) -> str:
    """Persist this thread's graph (no pool) and append a stored-graph section."""
    try:
        from msagent.exgraph.enrich import remember_thread

        remember_thread(trajectory, working_dir=working_dir, state_dir=state_dir)
        from msagent.exgraph.consumer import render_thread_context

        extra = render_thread_context(
            trajectory.thread_id,
            working_dir=working_dir,
            state_dir=state_dir,
        )
    except Exception:
        logger.debug("exgraph context skipped", exc_info=True)
        return bundle_text
    if not extra:
        return bundle_text
    return bundle_text + "\n\n" + extra
