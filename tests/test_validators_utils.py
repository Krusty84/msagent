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

from typing import Annotated

import pytest
from pydantic import BaseModel, Field

from msagent.utils.validators import _resolve_default, json_list_parser, json_safe_tool


class _Item(BaseModel):
    name: str


def test_json_list_parser_accepts_lists_and_repairs_strings(monkeypatch) -> None:
    parser = json_list_parser(_Item)

    assert parser([_Item(name="existing")]) == [_Item(name="existing")]
    assert parser('[{"name": "alpha"}]') == [_Item(name="alpha")]

    monkeypatch.setattr("msagent.utils.validators.repair_loads", lambda _raw: [{"name": "beta"}])
    assert parser('[{"name": "beta"') == [_Item(name="beta")]


def test_json_list_parser_rejects_non_arrays_and_unrepairable_json(monkeypatch) -> None:
    parser = json_list_parser(_Item)

    with pytest.raises(TypeError):
        parser('{"name":"alpha"}')

    monkeypatch.setattr("msagent.utils.validators.repair_loads", lambda _raw: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ValueError, match="auto-repair failed"):
        parser('{"name"')


def test_resolve_default_prefers_field_metadata_and_parameter_default() -> None:
    def sample(required: Annotated[str, Field(description="desc")] = "fallback") -> None:
        return None

    param = next(iter(sample.__annotations__.items()))
    del param

    import inspect

    signature_param = inspect.signature(sample).parameters["required"]
    field_info = Field(description="desc", title="Title")

    resolved = _resolve_default(signature_param, field_info)

    assert resolved.default == "fallback"
    assert resolved.description == "desc"
    assert resolved.title == "Title"
    assert _resolve_default(signature_param.replace(default=inspect.Parameter.empty), None) is ...


def test_json_safe_tool_builds_args_schema_with_model_list_parsing() -> None:
    @json_safe_tool
    def sample_tool(
        items: Annotated[list[_Item], Field(description="Items")],
        name: Annotated[str, Field(description="Name")],
        count: int = 1,
    ) -> str:
        """Sample tool used to validate json_safe_tool schema generation."""
        return f"{name}:{count}:{len(items)}"

    schema = sample_tool.args_schema
    assert schema is not None
    schema.model_rebuild(_types_namespace={"_Item": _Item})

    parsed = schema.model_validate({"items": '[{"name":"alpha"}]', "name": "demo"})

    assert parsed.items == [_Item(name="alpha")]
    assert parsed.name == "demo"
    assert parsed.count == 1
    assert schema.model_fields["name"].description == "Name"
