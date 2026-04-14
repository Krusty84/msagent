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

"""Completer router for slash commands and references."""

from collections.abc import AsyncGenerator, Iterable
from pathlib import Path

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from msagent.cli.completers.reference import ReferenceCompleter
from msagent.cli.completers.slash import SlashCommandCompleter


class CompleterRouter(Completer):
    """Routes completions to slash command or reference completers."""

    def __init__(
        self,
        commands: list[str],
        working_dir: Path,
        max_suggestions: int = 10,
    ):
        """Initialize completer router."""
        self.slash_completer = SlashCommandCompleter(commands)
        self.reference_completer = ReferenceCompleter(working_dir, max_suggestions)

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        """Sync stub - completions are async only."""
        return iter([])

    async def get_completions_async(
        self, document: Document, complete_event: CompleteEvent
    ) -> AsyncGenerator[Completion]:
        """Get completions asynchronously."""
        text = document.text_before_cursor

        if text.lstrip().startswith("/"):
            async for completion in self.slash_completer.get_completions_async(document, complete_event):
                yield completion
        else:
            async for completion in self.reference_completer.get_completions_async(document, complete_event):
                yield completion
