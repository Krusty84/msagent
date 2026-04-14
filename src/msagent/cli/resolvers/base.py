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

"""Base classes for reference resolvers."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from prompt_toolkit.completion import Completion


class RefType(str, Enum):
    """Reference types."""

    FILE = "file"
    IMAGE = "image"


class Resolver(ABC):
    """Abstract base for reference resolvers."""

    type: RefType

    @abstractmethod
    def resolve(self, ref: str, ctx: dict) -> str:
        """Resolve reference to final value."""

    @abstractmethod
    async def complete(self, fragment: str, ctx: dict, limit: int) -> list[Completion]:
        """Get completions for fragment."""

    def is_standalone_reference(self, text: str) -> bool:
        """Check if text is a standalone reference (e.g., absolute path)."""
        return False

    def build_content_block(self, path: str) -> dict[str, Any] | None:
        """Build content block for multimodal message. Returns None for text-only."""
        return None

    def get_removal_pattern(self) -> str:
        """Get regex pattern for removing this reference type from text."""
        return rf"@:{self.type.value}:(?:[^\s?,!.;]|[.,!?;](?!\s|$))+"
