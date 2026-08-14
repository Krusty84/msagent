#!/usr/bin/env python3
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
"""Shared target-path relevance and normalization helpers."""

from __future__ import annotations

import re

_NON_DATA_PATH_PREFIXES = (
    "/usr",
    "/lib",
    "/lib64",
    "/opt",
    "/var/log",
    "/var/lib",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/etc",
    "/run",
    "/snap",
    "/conda",
    "/python",
    "/bin",
    "/sbin",
)


_NON_DATA_PATH_SUFFIXES = (".so", ".so.", ".pyc", ".pyo", ".pyd")


def _canonicalize_path(p: str) -> str:
    """规范化绝对路径：折叠重复 `/`、解析 `.`/`..`、去 trailing slash。

    相对路径或含越界 `..` 的路径原样返回（不臆测），由调用方按需处理。
    """
    if not isinstance(p, str) or not p:
        return ""
    # 折叠重复斜杠
    norm = re.sub(r"/+", "/", p)
    # 解析 . / ..（仅对绝对路径，词法解析，不触碰符号链接）
    if not norm.startswith("/"):
        return norm.rstrip("/") or "."
    parts = norm.split("/")
    stack: list[str] = []
    for part in parts:
        if part == "" or part == ".":
            continue
        if part == "..":
            if stack:
                stack.pop()
            # 越界 .. 保留（不臆测到 / 之上）
            else:
                stack.append("..")
            continue
        stack.append(part)
    return "/" + "/".join(stack)


def _is_data_relevant_path(path: str | None, target_path: str | None = None) -> bool:
    """判断一条映射路径是否"数据相关"（排除共享库/日志/解释器/系统文件）。

    使用路径组件边界匹配（`path == prefix` 或 `path` 位于 `prefix/` 下），
    避免 `/usrdata` 被 `/usr` 前缀误伤。
    target 先规范化（折叠 `//`、解析 `.`/`..`）。规则优先级：
      1. 系统后缀（.so/.pyc...）与 site-packages 永远排除（即便在 target 下）。
      2. **具体** target（非 `/`/空/`.`）→ 其子树覆盖系统目录前缀排除
         （用户明确指定 /opt/dataset 为数据根，即便 /opt 是系统前缀）。
      3. 过宽 target（`/`、空、`.`）或无 target → 系统目录前缀排除生效
         （避免 target='/' 把 /usr/lib/*.so 重新认作数据）。
    """
    if not path or not isinstance(path, str):
        return False
    p = _canonicalize_path(path)
    low = p.lower()
    # 1. 后缀/site-packages 永远排除
    if any(low.endswith(suf) for suf in _NON_DATA_PATH_SUFFIXES):
        return False
    if "/site-packages/" in low or "/dist-packages/" in low:
        return False
    # 2. 具体 target → 子树覆盖系统前缀
    effective_target = ""
    if target_path and isinstance(target_path, str):
        tp = _canonicalize_path(target_path)
        if tp and tp not in ("/", "."):
            effective_target = tp
    if effective_target:
        return p == effective_target or p.startswith(effective_target + "/")
    # 3. 无/过宽 target → 系统目录前缀排除
    for prefix in _NON_DATA_PATH_PREFIXES:
        if p == prefix or p.startswith(prefix + "/"):
            return False
    return True
