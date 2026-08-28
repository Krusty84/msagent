"""Turn raw trajectory events into normalized workflow steps.

Three transformations happen here, in order:

1. **Pairing** -- a ``tool_call`` event and its ``tool_result`` become one
   ``Step``. Results arrive by call id; when a source does not carry ids, the
   next unmatched result for the same tool is used.
2. **Retry collapsing** -- the model frequently re-issues an identical call
   after a transient failure. Five identical calls are one workflow step, not
   five, so consecutive duplicates collapse into a single step carrying
   ``repeat_count``.
3. **Recovery detection** -- a failed step followed by a corrected call of the
   same tool is the single most valuable pattern in a trajectory: it is the
   only place a successful run tells you what *not* to do.
"""

from __future__ import annotations

import json
from typing import Any

from .models import MAIN_ORIGIN, RawEvent, Recovery, Step

_DEFAULT_RECOVERY_WINDOW = 4
_ERROR_PREVIEW_LIMIT = 400


def _args_signature(args: dict[str, Any]) -> str:
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(sorted(args.items()))


def _error_from(text: str) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= _ERROR_PREVIEW_LIMIT:
        return stripped
    return f"{stripped[:_ERROR_PREVIEW_LIMIT]}..."


def build_steps(events: list[RawEvent]) -> list[Step]:
    """Pair tool calls with their results and return ordered steps."""
    results_by_id: dict[str, RawEvent] = {}
    loose_results: list[RawEvent] = []
    for event in events:
        if event.kind != "tool_result":
            continue
        if event.call_id:
            results_by_id[event.call_id] = event
        else:
            loose_results.append(event)

    consumed_loose: set[int] = set()
    steps: list[Step] = []
    index = 0

    for event in events:
        if event.kind != "tool_call":
            continue

        result = results_by_id.get(event.call_id) if event.call_id else None
        if result is None:
            for position, candidate in enumerate(loose_results):
                if position in consumed_loose:
                    continue
                if candidate.tool and candidate.tool != event.tool:
                    continue
                consumed_loose.add(position)
                result = candidate
                break

        index += 1
        text = result.text if result is not None else ""
        ok = result.ok if result is not None else True
        steps.append(
            Step(
                index=index,
                tool=event.tool,
                args_raw=dict(event.args or {}),
                args=dict(event.args or {}),
                origin=event.origin or MAIN_ORIGIN,
                namespace=event.namespace,
                ok=ok,
                error=None if ok else _error_from(text),
                result_preview=text,
                result_chars=len(text),
                result_truncated=(result.truncated if result is not None else False),
                duration_ms=(result.duration_ms if result is not None else None) or event.duration_ms,
                source_indices=[event.index],
            )
        )

    return steps


def collapse_retries(steps: list[Step]) -> list[Step]:
    """Collapse runs of consecutive identical calls into one step each."""
    collapsed: list[Step] = []
    for step in steps:
        previous = collapsed[-1] if collapsed else None
        same_call = (
            previous is not None
            and previous.tool == step.tool
            and previous.origin == step.origin
            and _args_signature(previous.args_raw) == _args_signature(step.args_raw)
        )
        if not same_call:
            collapsed.append(step)
            continue

        assert previous is not None  # narrowed by same_call
        previous.repeat_count += 1
        previous.source_indices.extend(step.source_indices)
        # A run that eventually succeeded is reported as a success, keeping the
        # outcome that the workflow actually depends on.
        if step.ok and not previous.ok:
            previous.ok = True
            previous.error = None
            previous.result_preview = step.result_preview
            previous.result_chars = step.result_chars
            previous.result_truncated = step.result_truncated

    for position, step in enumerate(collapsed, start=1):
        step.index = position
    return collapsed


def detect_recoveries(
    steps: list[Step],
    *,
    window: int = _DEFAULT_RECOVERY_WINDOW,
) -> list[Recovery]:
    """Find failed steps that a later corrected call of the same tool fixed."""
    recoveries: list[Recovery] = []
    for position, step in enumerate(steps):
        if step.ok:
            continue
        for candidate in steps[position + 1 : position + 1 + window]:
            if candidate.tool != step.tool or not candidate.ok:
                continue
            changed = sorted(
                key
                for key in set(step.args_raw) | set(candidate.args_raw)
                if step.args_raw.get(key) != candidate.args_raw.get(key)
            )
            if not changed:
                continue
            recoveries.append(
                Recovery(
                    tool=step.tool,
                    failed_step=step.index,
                    fixed_step=candidate.index,
                    error=step.error or "",
                    changed_args=changed,
                    before={key: step.args.get(key) for key in changed if key in step.args},
                    after={key: candidate.args.get(key) for key in changed if key in candidate.args},
                )
            )
            break
    return recoveries
