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

"""Full-fidelity trajectory recording for msAgent sessions.

The package is self-contained on purpose: integration with the rest of the
codebase happens exclusively through :mod:`msagent.trajectory_recorder.hooks`, and every
public hook is exception-safe (recording problems never break the agent).

This ``__init__`` intentionally imports nothing so that lightweight consumers
(e.g. ``python -m msagent.trajectory_recorder.export``) do not pull langchain into the
process. Import ``msagent.trajectory_recorder.hooks`` directly from integration points.
"""