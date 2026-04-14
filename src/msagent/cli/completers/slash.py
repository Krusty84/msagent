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

"""Slash command completer."""

from collections.abc import AsyncGenerator, Iterable

from prompt_toolkit.completion import CompleteEvent, Completion, WordCompleter
from prompt_toolkit.document import Document


class SlashCommandCompleter(WordCompleter):
    """Auto-completer for slash commands."""

    def __init__(self, commands: list[str]):
        super().__init__(commands, ignore_case=True, sentence=True)

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        """Get completions for slash commands (sync)."""
        yield from super().get_completions(document, complete_event)

    async def get_completions_async(
        self, document: Document, complete_event: CompleteEvent
    ) -> AsyncGenerator[Completion]:
        """Get completions for slash commands (async)."""
        for completion in self.get_completions(document, complete_event):
            yield completion
