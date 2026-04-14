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

"""Theme registry for managing and loading themes."""

from collections.abc import Callable

from msagent.cli.theme.base import BaseTheme

# Global registry mapping theme names to theme classes
_THEME_REGISTRY: dict[str, Callable[[], BaseTheme]] = {}


def register_theme(name: str):
    """Decorator to register a theme class in the registry.

    Usage:
        @register_theme("my-theme")
        class MyTheme:
            ...

    Args:
        name: Theme name (used in configuration)

    Returns:
        Decorator function
    """

    def decorator(theme_class: type[BaseTheme]) -> type[BaseTheme]:
        _THEME_REGISTRY[name] = theme_class
        return theme_class

    return decorator


def get_theme(name: str) -> BaseTheme:
    """Get a theme instance by name.

    Args:
        name: Theme name from configuration

    Returns:
        Theme instance

    Raises:
        ValueError: If theme name is not registered
    """
    if name not in _THEME_REGISTRY:
        available = ", ".join(sorted(_THEME_REGISTRY.keys()))
        raise ValueError(f"Theme '{name}' not found. Available themes: {available or 'none'}")

    theme_class = _THEME_REGISTRY[name]
    return theme_class()
