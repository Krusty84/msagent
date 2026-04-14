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

from msagent.cli.theme import tokyo_day, tokyo_night  # noqa: F401
from msagent.cli.theme.console import ThemedConsole
from msagent.cli.theme.detect import detect_terminal_theme
from msagent.cli.theme.registry import get_theme
from msagent.core.settings import settings

# Map detected theme mode to theme name
_THEME_MAP = {
    "dark": "tokyo-night",
    "light": "tokyo-day",
}

# Use user setting if set, otherwise auto-detect
if settings.cli.theme is not None:
    _theme_name = settings.cli.theme
else:
    _detected_mode = detect_terminal_theme()
    _theme_name = _THEME_MAP.get(_detected_mode, "tokyo-night")

theme = get_theme(_theme_name)
console = ThemedConsole(theme)
