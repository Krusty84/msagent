#!/usr/bin/python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
# MindStudio is licensed under Mulan PSL v2.
# -------------------------------------------------------------------------

"""Merge default: graph off unless YAML or MSAGENT_EXGRAPH_ENABLED says on."""

from __future__ import annotations

from msagent.exgraph.config import (
    ENV_DISABLED,
    ENV_ENABLED,
    ExgraphConfig,
    is_exgraph_enabled,
    load_exgraph_config,
    reset_config_cache,
)


def test_model_default_is_disabled() -> None:
    assert ExgraphConfig().enabled is False


def test_yaml_or_missing_config_is_off_without_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(ENV_ENABLED, raising=False)
    monkeypatch.delenv(ENV_DISABLED, raising=False)
    reset_config_cache()
    assert is_exgraph_enabled(force_reload=True) is False
    assert load_exgraph_config(force_reload=True).enabled is False


def test_enabled_env_opts_in(monkeypatch) -> None:
    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.delenv(ENV_DISABLED, raising=False)
    reset_config_cache()
    assert is_exgraph_enabled(force_reload=True) is True
    assert load_exgraph_config(force_reload=True).enabled is True


def test_disabled_wins_over_enabled(monkeypatch) -> None:
    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.setenv(ENV_DISABLED, "1")
    reset_config_cache()
    assert is_exgraph_enabled(force_reload=True) is False
    reset_config_cache()
