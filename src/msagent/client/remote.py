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

"""Client-side remote agent: talks to the ``langgraph dev`` agent server.

Wraps ``langgraph.pregel.remote.RemoteGraph`` (HTTP + SSE), which produces the
same Pregel-style ``(namespace, mode, data)`` stream tuples the in-process
graph used to yield, so ``MessageDispatcher`` keeps its consumption shape.
Server messages arrive as plain dicts; they are converted back into LangChain
message objects here so downstream rendering/audit logic is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server message dict -> LangChain message object conversion
# ---------------------------------------------------------------------------


def _convert_ai_message(data: dict[str, Any]) -> Any:
    """Convert a server AI message dict to an ``AIMessageChunk``.

    Handles both ``tool_call_chunks`` (streaming partial args) and
    ``tool_calls`` (parsed or legacy string-args) representations.
    """
    from langchain_core.messages import AIMessageChunk

    content = data.get("content", "")
    tool_call_chunks = data.get("tool_call_chunks", [])
    tool_calls = data.get("tool_calls", [])
    usage_metadata = data.get("usage_metadata")
    response_metadata = data.get("response_metadata", {})

    kwargs: dict[str, Any] = {
        "content": content,
        "id": data.get("id"),
        "response_metadata": response_metadata,
    }

    if tool_call_chunks:
        kwargs["tool_call_chunks"] = [
            {
                "name": tc.get("name"),
                "args": tc.get("args", ""),
                "id": tc.get("id"),
                "index": tc.get("index", i),
            }
            for i, tc in enumerate(tool_call_chunks)
        ]
    elif tool_calls:
        has_str_args = any(isinstance(tc.get("args"), str) for tc in tool_calls)
        if has_str_args:
            kwargs["tool_call_chunks"] = [
                {
                    "name": tc.get("name"),
                    "args": tc.get("args", ""),
                    "id": tc.get("id"),
                    "index": i,
                }
                for i, tc in enumerate(tool_calls)
            ]
        else:
            kwargs["tool_calls"] = tool_calls

    try:
        chunk = AIMessageChunk(**kwargs)
    except (TypeError, ValueError, KeyError):
        logger.warning(
            "Failed to construct AIMessageChunk from server data (id=%s)",
            data.get("id"),
            exc_info=True,
        )
        return None

    if usage_metadata:
        chunk.usage_metadata = usage_metadata
    return chunk


def _convert_human_message(data: dict[str, Any]) -> Any:
    """Convert a server human message dict to a ``HumanMessage``."""
    from langchain_core.messages import HumanMessage

    try:
        return HumanMessage(
            content=data.get("content", ""),
            id=data.get("id"),
        )
    except (TypeError, ValueError, KeyError):
        logger.warning(
            "Failed to construct HumanMessage from server data (id=%s)",
            data.get("id"),
            exc_info=True,
        )
        return None


def _convert_tool_message(data: dict[str, Any]) -> Any:
    """Convert a server tool message dict to a ``ToolMessage``."""
    from langchain_core.messages import ToolMessage

    try:
        return ToolMessage(
            content=data.get("content", ""),
            tool_call_id=data.get("tool_call_id", ""),
            name=data.get("name", ""),
            id=data.get("id"),
            status=data.get("status", "success"),
        )
    except (TypeError, ValueError, KeyError):
        logger.warning(
            "Failed to construct ToolMessage from server data (id=%s)",
            data.get("id"),
            exc_info=True,
        )
        return None


def _convert_system_message(data: dict[str, Any]) -> Any:
    """Convert a server system message dict to a ``SystemMessage``."""
    from langchain_core.messages import SystemMessage

    try:
        return SystemMessage(
            content=data.get("content", ""),
            id=data.get("id"),
        )
    except (TypeError, ValueError, KeyError):
        logger.warning(
            "Failed to construct SystemMessage from server data (id=%s)",
            data.get("id"),
            exc_info=True,
        )
        return None


_MESSAGE_CONVERTERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "ai": _convert_ai_message,
    "AIMessage": _convert_ai_message,
    "AIMessageChunk": _convert_ai_message,
    "human": _convert_human_message,
    "HumanMessage": _convert_human_message,
    "tool": _convert_tool_message,
    "ToolMessage": _convert_tool_message,
    "system": _convert_system_message,
    "SystemMessage": _convert_system_message,
}


def _convert_message_data(data: dict[str, Any]) -> Any:
    """Convert a server message dict into a LangChain message object."""
    msg_type = data.get("type", "")
    converter = _MESSAGE_CONVERTERS.get(msg_type)
    if converter is not None:
        return converter(data)
    logger.warning("Unknown message type in stream: %s", msg_type)
    return None


def _convert_interrupts(raw: Any) -> list[Any]:
    """Convert interrupt dicts from the server into ``Interrupt`` objects."""
    from langgraph.types import Interrupt

    if not isinstance(raw, list):
        logger.warning("Expected list for __interrupt__ data, got %s", type(raw).__name__)
        return [raw] if raw is not None else []
    results: list[Any] = []
    for item in raw:
        if isinstance(item, Interrupt):
            results.append(item)
        elif isinstance(item, dict) and "value" in item:
            results.append(Interrupt(value=item["value"], id=item.get("id", "")))
        else:
            results.append(item)
    return results


def _convert_message_list(values: Any) -> Any:
    """Convert message dicts inside a channel value back to message objects.

    Handles both a bare ``list`` of messages and the serialized ``Overwrite``
    wrapper shape (``{"value": [...]}``) that the server emits for replaced
    channels. Unconvertible dicts (e.g. an unregistered message type) are
    dropped rather than left as ``None``, which would crash downstream
    consumers such as summarization.
    """

    def _convert(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        converted = _convert_message_data(item)
        return converted if converted is not None else None

    if isinstance(values, dict) and "value" in values and isinstance(values.get("value"), list):
        converted_inner = [c for c in (_convert(item) for item in values["value"]) if c is not None]
        return {**values, "value": converted_inner}
    if isinstance(values, list):
        return [c for c in (_convert(item) for item in values) if c is not None]
    return values


def _convert_update_data(data: dict[str, Any]) -> dict[str, Any]:
    """Convert message dicts inside an ``updates`` payload to message objects.

    ``langgraph dev`` serializes channel values via ``model_dump()``, so the
    ``messages`` channel arrives as a list of plain dicts; the CLI rendering
    layer expects LangChain message objects (``isinstance(BaseMessage)``).
    """
    converted: dict[str, Any] = {}
    for node_name, node_data in data.items():
        if not isinstance(node_data, dict):
            converted[node_name] = node_data
            continue
        node_out = dict(node_data)
        if "messages" in node_out:
            node_out["messages"] = _convert_message_list(node_out["messages"])
        converted[node_name] = node_out
    return converted


def _convert_state_result(result: Any) -> Any:
    """Convert message dicts inside an ``ainvoke``/state result."""
    if isinstance(result, dict) and isinstance(result.get("messages"), list):
        result = {**result, "messages": _convert_message_list(result["messages"])}
    return result


def _serialize_context(context: Any) -> Any:
    """Turn a pydantic runtime context into a JSON-safe dict for the SDK.

    ``AgentContext`` is declared as the graph's ``context_schema``; the SDK
    serializes the ``context=`` parameter to JSON, so Path objects and the
    excluded non-serializable fields (tool/skill catalog, retry handler) must
    not cross the boundary as-is.
    """
    if context is not None and hasattr(context, "model_dump"):
        return context.model_dump(mode="json")
    return context


_RUN_CANCEL_WAIT_SECONDS = 30.0


def _prepare_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Shallow-copy config so callers' dicts are not mutated."""
    config = dict(config or {})
    configurable = dict(config.get("configurable", {}))
    config["configurable"] = configurable
    return config


async def _cancel_active_runs(graph: Any, thread_id: str) -> None:
    """Cancel pending/running runs on a thread and wait for them to settle.

    Best-effort: per-run failures are logged and swallowed. The SDK client is
    reached via the private ``RemoteGraph._validate_client()``; if upstream
    renames it, cancellation degrades to a no-op.
    """
    try:
        client = graph._validate_client()
    except Exception:
        logger.warning(
            "Could not obtain SDK client for thread %s", thread_id, exc_info=True
        )
        return

    run_ids: list[str] = []
    for status in ("running", "pending"):
        try:
            runs = await client.runs.list(thread_id, status=status, limit=10)
        except Exception:
            logger.debug(
                "Failed to list %s runs for thread %s", status, thread_id, exc_info=True
            )
            continue
        for run in runs:
            run_id = getattr(run, "run_id", None) or (run.get("run_id") if isinstance(run, dict) else None)
            if run_id:
                run_ids.append(run_id)

    if not run_ids:
        return

    async def _cancel_one(run_id: str) -> None:
        try:
            await asyncio.wait_for(
                client.runs.cancel(thread_id, run_id, wait=True, action="interrupt"),
                timeout=_RUN_CANCEL_WAIT_SECONDS,
            )
        except Exception:
            logger.debug(
                "Failed to cancel run %s on thread %s", run_id, thread_id, exc_info=True
            )

    await asyncio.gather(*(_cancel_one(run_id) for run_id in run_ids))


class RemoteAgent:
    """Client that talks to the msagent ``langgraph dev`` agent server."""

    def __init__(
        self,
        url: str,
        *,
        graph_name: str = "agent",
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = url
        self._graph_name = graph_name
        self._api_key = api_key
        self._headers = headers
        self._graph: Any = None

    def _get_graph(self) -> Any:
        if self._graph is None:
            from langgraph.pregel.remote import RemoteGraph

            self._graph = RemoteGraph(
                self._graph_name,
                url=self._url,
                api_key=self._api_key,
                headers=self._headers,
            )
        return self._graph

    async def astream(
        self,
        input: Any,  # noqa: A002
        config: Mapping[str, Any] | None = None,
        *,
        stream_mode: Sequence[str] | None = None,
        subgraphs: bool = False,
        context: Any | None = None,
    ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
        """Stream agent execution, yielding Pregel-style 3-tuples.

        Message dicts from the server are converted back into LangChain
        message objects; ``__interrupt__`` payloads are converted into
        ``Interrupt`` objects.
        """
        graph = self._get_graph()
        async for ns, mode, data in graph.astream(
            input,
            config=config,
            context=_serialize_context(context),
            stream_mode=stream_mode or ["messages", "updates"],
            subgraphs=subgraphs,
        ):
            if mode == "messages":
                msg_dict, meta = data
                if isinstance(msg_dict, dict):
                    msg_obj = _convert_message_data(msg_dict)
                    if msg_obj is not None:
                        yield (ns, "messages", (msg_obj, meta or {}))
                    else:
                        logger.warning("Dropped unconvertible message in stream")
                else:
                    yield (ns, "messages", (msg_dict, meta or {}))
                continue

            if mode == "updates" and isinstance(data, dict):
                update_data = _convert_update_data(data)
                if "__interrupt__" in update_data:
                    update_data = {
                        **update_data,
                        "__interrupt__": _convert_interrupts(update_data["__interrupt__"]),
                    }
                yield (ns, "updates", update_data)
                continue

            yield (ns, mode, data)

    async def ainvoke(
        self,
        input: Any,  # noqa: A002
        config: Mapping[str, Any] | None = None,
        context: Any | None = None,
    ) -> Any:
        result = await self._get_graph().ainvoke(
            input,
            config=config,
            context=_serialize_context(context),
        )
        return _convert_state_result(result)

    async def aget_state(self, config: Mapping[str, Any]) -> Any:
        """Get the current state of a thread.

        Returns ``None`` when the thread does not exist on the server (404) or
        exists but has no checkpoint yet (new thread). Other errors are logged
        and re-raised.
        """
        from langgraph_sdk.errors import NotFoundError

        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        graph = self._get_graph()
        try:
            return await graph.aget_state(config)
        except NotFoundError:
            logger.debug("Thread %s not found on server", thread_id)
            return None
        except TypeError as exc:  # langgraph SDK: checkpoint=null on new threads
            if "subscriptable" in str(exc).lower():
                logger.debug("Thread %s has no checkpoint yet; treating as empty", thread_id)
                return None
            logger.warning("Failed to get state for thread %s", thread_id, exc_info=True)
            raise
        except Exception:
            logger.warning("Failed to get state for thread %s", thread_id, exc_info=True)
            raise

    async def aupdate_state(
        self,
        config: Mapping[str, Any],
        values: Mapping[str, Any] | Any,
        *,
        as_node: str | None = None,
    ) -> Any:
        """Update the state of a thread.

        On HTTP 409 (``ConflictError``) the server still considers the thread
        busy -- typically because the client cancelled the SSE stream before
        the server finished the run. In that case, cancel pending/running runs
        and retry the state update once (bounded, failure-tolerant).
        """
        from langgraph_sdk.errors import ConflictError

        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        prepared = _prepare_config(config)
        graph = self._get_graph()

        try:
            return await graph.aupdate_state(prepared, values, as_node=as_node)
        except ConflictError:
            logger.debug(
                "update_state conflict for thread %s; cancelling active runs and retrying",
                thread_id,
            )
        except Exception:
            logger.debug(
                "Failed to update state for thread %s", thread_id, exc_info=True
            )
            raise

        if thread_id:
            await _cancel_active_runs(graph, thread_id)
        return await graph.aupdate_state(prepared, values, as_node=as_node)

    async def acancel_active_runs(self, config: Mapping[str, Any]) -> None:
        """Cancel pending/running runs on the configured thread (best-effort).

        Used when the CLI interrupts a stream: tearing down the SSE connection
        alone leaves the server run executing, so the active runs are listed
        and cancelled explicitly.
        """
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        if not thread_id:
            return
        await _cancel_active_runs(self._get_graph(), thread_id)

    async def aoffload(
        self,
        *,
        thread_id: str,
        keep_messages: int = 0,
        summary_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Request server-owned conversation offload for a thread.

        The server reads the thread state, summarizes, writes conversation
        history through its real backend, and writes back the event.
        """
        timeout = httpx.Timeout(connect=10.0, read=900.0, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._url}/offload",
                json={
                    "thread_id": thread_id,
                    "keep_messages": keep_messages,
                    "summary_prompt": summary_prompt,
                },
            )
            response.raise_for_status()
            return response.json()

    async def fetch_catalog(self) -> dict[str, Any]:
        """Fetch the authoritative tool/skill/MCP catalog from the server."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{self._url}/catalog")
            response.raise_for_status()
            return response.json()
