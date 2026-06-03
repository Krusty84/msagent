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

from __future__ import annotations

from types import SimpleNamespace

from msagent.utils import version as version_module


class _FakeResourceRoot:
    def __init__(self, text: str) -> None:
        self._text = text

    def joinpath(self, _path: str) -> "_FakeResourceRoot":
        return self

    def read_text(self, encoding: str = "utf-8") -> str:
        assert encoding == "utf-8"
        return self._text


def test_get_latest_features_reads_current_and_prior_minor_versions(monkeypatch) -> None:
    monkeypatch.setattr(version_module, "get_version", lambda: "1.3.2")
    monkeypatch.setattr(
        version_module.importlib.resources,
        "files",
        lambda _package: _FakeResourceRoot(
            """
max_display: 3
features_by_version:
  1.4.x: [future]
  1.3.x: [current-a, current-b]
  1.2.x: [previous-a, previous-b]
            """.strip()
        ),
    )

    assert version_module.get_latest_features() == ["current-a", "current-b", "previous-a"]


def test_get_latest_features_returns_empty_list_on_errors(monkeypatch) -> None:
    monkeypatch.setattr(version_module.importlib.resources, "files", lambda _package: (_ for _ in ()).throw(ValueError))

    assert version_module.get_latest_features() == []


def test_check_for_updates_handles_new_versions_and_failures(monkeypatch) -> None:
    monkeypatch.setattr(version_module, "get_version", lambda: "1.2.3")
    monkeypatch.setattr(
        version_module.httpx,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {"info": {"version": "1.3.0"}},
        ),
    )

    assert version_module.check_for_updates() == ("1.3.0", "uv tool install mindstudio-agent --upgrade")

    monkeypatch.setattr(version_module.httpx, "get", lambda *args, **kwargs: SimpleNamespace(status_code=500))
    assert version_module.check_for_updates() is None

    monkeypatch.setattr(version_module, "get_version", lambda: "unknown")
    assert version_module.check_for_updates() is None

    monkeypatch.setattr(version_module, "get_version", lambda: "1.3.0")
    monkeypatch.setattr(
        version_module.httpx,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {"info": {"version": "1.3.0"}},
        ),
    )
    assert version_module.check_for_updates() is None
