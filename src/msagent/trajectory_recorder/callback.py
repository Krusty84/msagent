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

"""LangChain callback handler that records the full agent trajectory.

Attached to the top-level graph invocation, it observes every LLM call and
every tool execution in the whole run tree — including subagents spawned by the
deepagents ``task`` tool, because langchain propagates callbacks to nested
``ainvoke`` calls through the ambient run context. ``parent_span_id`` therefore
reconstructs the exact delegation tree, and langgraph's ``checkpoint_ns``
metadata identifies which subagent an event belongs to.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import LLMResult

from msagent.trajectory_recorder.config import CaptureConfig, CaptureLevel
from msagent.trajectory_recorder.recorder import TrajectoryRecorder
from msagent.trajectory_recorder.serialize import json_safe, serialize_message, serialize_messages

logger = logging.getLogger(__name__)

_GRAPH_METADATA_PREFIXES = ("langgraph_", "ls_")
_GRAPH_METADATA_KEYS = ("checkpoint_ns", "checkpoint_id")


def _graph_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    selected = {
        key: value
        for key, value in metadata.items()
        if key in _GRAPH_METADATA_KEYS or key.startswith(_GRAPH_METADATA_PREFIXES)
    }
    return json_safe(selected)


class TrajectoryCallbackHandler(BaseCallbackHandler):
    """Record llm/tool events of one graph invocation into a recorder."""

    run_inline = True
    raise_error = False

    # Chain events are one per langgraph node and add little signal; turn
    # boundaries are recorded explicitly by the hooks module instead.
    ignore_chain = True
    # langchain_core dispatches on_tool_start/on_tool_end/on_tool_error under the
    # ``ignore_agent`` gate (callbacks/manager.py), so agent events must stay
    # enabled or no tool event is ever recorded.
    ignore_agent = False
    ignore_retriever = True
    ignore_custom_event = True

    def __init__(
        self,
        *,
        recorder: TrajectoryRecorder,
        capture: CaptureConfig,
        turn_run_id: str,
    ) -> None:
        self.recorder = recorder
        self.capture = capture
        self.turn_run_id = turn_run_id

        self._lock = threading.Lock()
        self._started_at: dict[UUID, float] = {}
        self._run_context: dict[UUID, dict[str, Any]] = {}

    # ------------------------------------------------------------------ util

    def _span_fields(self, run_id: UUID, parent_run_id: UUID | None) -> dict[str, Any]:
        return {
            "run_id": self.turn_run_id,
            "span_id": str(run_id),
            "parent_span_id": str(parent_run_id) if parent_run_id else None,
        }

    def _track_start(self, run_id: UUID, context: dict[str, Any]) -> None:
        with self._lock:
            self._started_at[run_id] = time.perf_counter()
            self._run_context[run_id] = context

    def _finish_run(self, run_id: UUID) -> tuple[int | None, dict[str, Any]]:
        with self._lock:
            started = self._started_at.pop(run_id, None)
            context = self._run_context.pop(run_id, {})
        duration_ms = round((time.perf_counter() - started) * 1000) if started is not None else None
        return duration_ms, context

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        self.recorder.emit(event, payload)

    # ------------------------------------------------------------------- llm

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            graph_meta = _graph_metadata(metadata) if self.capture.graph_metadata else {}
            self._track_start(run_id, {"kind": "llm", "graph": graph_meta})
            if self.capture.level != CaptureLevel.LLM_IO:
                return

            window: list[dict[str, Any]] = []
            for batch in messages:
                window.extend(serialize_messages(batch))
            payload = {
                **self._span_fields(run_id, parent_run_id),
                "model": (metadata or {}).get("ls_model_name"),
                "messages": window,
                "message_count": len(window),
            }
            if graph_meta:
                payload["graph"] = graph_meta
            self._emit("llm.request", payload)
        except Exception:
            logger.debug("trajectory on_chat_model_start failed", exc_info=True)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            graph_meta = _graph_metadata(metadata) if self.capture.graph_metadata else {}
            self._track_start(run_id, {"kind": "llm", "graph": graph_meta})
            if self.capture.level != CaptureLevel.LLM_IO:
                return
            payload = {
                **self._span_fields(run_id, parent_run_id),
                "model": (metadata or {}).get("ls_model_name"),
                "prompts": json_safe(prompts),
            }
            if graph_meta:
                payload["graph"] = graph_meta
            self._emit("llm.request", payload)
        except Exception:
            logger.debug("trajectory on_llm_start failed", exc_info=True)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            duration_ms, context = self._finish_run(run_id)
            base = self._span_fields(run_id, parent_run_id)
            if duration_ms is not None:
                base["duration_ms"] = duration_ms
            graph_meta = context.get("graph") or {}
            if graph_meta:
                base["graph"] = graph_meta

            emitted = False
            for batch in response.generations or []:
                for generation in batch:
                    message = getattr(generation, "message", None)
                    if message is None:
                        continue
                    emitted = True
                    payload = dict(base)
                    payload["message"] = serialize_message(message)
                    payload["message_id"] = getattr(message, "id", None)
                    usage = getattr(message, "usage_metadata", None)
                    if usage:
                        payload["usage"] = json_safe(usage)
                    response_metadata = getattr(message, "response_metadata", None) or {}
                    model_name = response_metadata.get("model_name") or response_metadata.get("model")
                    if model_name:
                        payload["model"] = model_name
                    tool_calls = getattr(message, "tool_calls", None) or []
                    payload["tool_call_count"] = len(tool_calls)
                    self._emit("message.ai", payload)

            if not emitted:
                payload = dict(base)
                payload["llm_output"] = json_safe(response.llm_output)
                self._emit("llm.response", payload)
        except Exception:
            logger.debug("trajectory on_llm_end failed", exc_info=True)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            duration_ms, context = self._finish_run(run_id)
            payload = {
                **self._span_fields(run_id, parent_run_id),
                "error_type": type(error).__name__,
                "error": str(error),
            }
            if duration_ms is not None:
                payload["duration_ms"] = duration_ms
            if context.get("graph"):
                payload["graph"] = context["graph"]
            self._emit("llm.error", payload)
        except Exception:
            logger.debug("trajectory on_llm_error failed", exc_info=True)

    def on_retry(
        self,
        retry_state: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if not self.capture.retries:
            return
        try:
            outcome = getattr(retry_state, "outcome", None)
            exception = None
            if outcome is not None:
                try:
                    exception = outcome.exception()
                except Exception:
                    exception = None
            payload = {
                **self._span_fields(run_id, parent_run_id),
                "attempt": getattr(retry_state, "attempt_number", None),
                "error_type": type(exception).__name__ if exception else None,
                "error": str(exception) if exception else None,
                "wait_seconds": getattr(getattr(retry_state, "next_action", None), "sleep", None),
            }
            self._emit("llm.retry", payload)
        except Exception:
            logger.debug("trajectory on_retry failed", exc_info=True)

    # ------------------------------------------------------------------ tools

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            name = str((serialized or {}).get("name") or "tool")
            graph_meta = _graph_metadata(metadata) if self.capture.graph_metadata else {}
            self._track_start(run_id, {"kind": "tool", "name": name, "graph": graph_meta})
            if not self.capture.tool_starts:
                return

            inputs = kwargs.get("inputs")
            payload = {
                **self._span_fields(run_id, parent_run_id),
                "name": name,
                "input": json_safe(inputs) if isinstance(inputs, dict) else json_safe(input_str),
            }
            if graph_meta:
                payload["graph"] = graph_meta
            self._emit("tool.start", payload)
        except Exception:
            logger.debug("trajectory on_tool_start failed", exc_info=True)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            duration_ms, context = self._finish_run(run_id)
            payload = {
                **self._span_fields(run_id, parent_run_id),
                "name": context.get("name") or getattr(output, "name", None) or "tool",
                "status": "ok",
            }
            if duration_ms is not None:
                payload["duration_ms"] = duration_ms
            if context.get("graph"):
                payload["graph"] = context["graph"]

            payload.update(self._serialize_tool_output(output))
            self._emit("tool.result", payload)
        except Exception:
            logger.debug("trajectory on_tool_end failed", exc_info=True)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            duration_ms, context = self._finish_run(run_id)
            payload = {
                **self._span_fields(run_id, parent_run_id),
                "name": context.get("name") or "tool",
                "status": "error",
                "error_type": type(error).__name__,
                "error": str(error),
            }
            if duration_ms is not None:
                payload["duration_ms"] = duration_ms
            if context.get("graph"):
                payload["graph"] = context["graph"]
            self._emit("tool.error", payload)
        except Exception:
            logger.debug("trajectory on_tool_error failed", exc_info=True)

    @staticmethod
    def _serialize_tool_output(output: Any) -> dict[str, Any]:
        """Serialize a tool result: ToolMessage, langgraph Command, or plain value."""
        if isinstance(output, ToolMessage):
            fields: dict[str, Any] = {"message": serialize_message(output)}
            status = getattr(output, "status", None)
            if status:
                fields["status"] = str(status)
            return fields

        try:
            from langgraph.types import Command
        except Exception:
            Command = None  # noqa: N806

        if Command is not None and isinstance(output, Command):
            update = getattr(output, "update", None)
            fields = {"command": True}
            if isinstance(update, dict):
                messages = update.get("messages")
                fields["command_update_keys"] = [str(key) for key in update.keys()]
                if isinstance(messages, (list, tuple)):
                    fields["messages"] = serialize_messages(messages)
            return fields

        return {"output": json_safe(output)}
