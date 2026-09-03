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

"""Append-only JSONL writer for trajectory events.

One file per conversation thread. Every line is a self-contained event:

    {"v": 1, "event": "...", "ts": "...", "seq": N, "rec": "<writer uuid>",
     "thread_id": "...", "agent": "...", ...payload}

``(rec, seq)`` is unique across process restarts, which gives downstream
consumers (knowledge graphs in particular) stable event identities.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from msagent.trajectory_recorder.config import TrajectoryRecorderConfig
from msagent.trajectory_recorder.serialize import compile_redaction_patterns, json_safe, sanitize

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TrajectoryRecorder:
    """Thread-safe append-only event writer for a single conversation thread."""

    def __init__(
        self,
        *,
        path: Path,
        config: TrajectoryRecorderConfig,
        base_fields: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.config = config
        self.writer_id = uuid.uuid4().hex
        self.base_fields = dict(base_fields or {})
        self.disabled_reason: str | None = None

        self._lock = threading.Lock()
        self._seq = 0
        self._max_bytes = config.limits.max_file_mb * 1024 * 1024
        self._patterns = compile_redaction_patterns(config.redaction.patterns)
        self._error_logged = False

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """Append one event line; never raises."""
        try:
            self._emit(event, payload or {})
        except Exception:
            if not self._error_logged:
                self._error_logged = True
                logger.warning("Trajectory recording failed for %s", self.path, exc_info=True)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.disabled_reason is not None:
            return

        with self._lock:
            if self._limit_exceeded():
                self.disabled_reason = "max_file_mb"
                self._append_line(
                    self._envelope(
                        "recorder.limit",
                        {"reason": "max_file_mb", "max_file_mb": self.config.limits.max_file_mb},
                    )
                )
                logger.warning("Trajectory file %s exceeded max_file_mb; recording stopped", self.path)
                return
            self._append_line(self._envelope(event, payload))

    def _envelope(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        body = sanitize(
            json_safe(payload),
            max_field_chars=self.config.limits.max_field_chars,
            patterns=self._patterns,
            replacement=self.config.redaction.replacement,
        )
        return {
            "v": SCHEMA_VERSION,
            "event": event,
            "ts": _utc_now(),
            "seq": self._seq,
            "rec": self.writer_id,
            **self.base_fields,
            **body,
        }

    def _append_line(self, envelope: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")

    def _limit_exceeded(self) -> bool:
        if self._max_bytes <= 0:
            return False
        try:
            return self.path.stat().st_size >= self._max_bytes
        except OSError:
            return False