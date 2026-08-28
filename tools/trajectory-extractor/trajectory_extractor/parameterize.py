"""Lift concrete values out of a trajectory into named placeholders.

A trajectory records one run, so every path, host and version is already
resolved. A skill needs the opposite: the *shape* of the call plus a statement
of where each value comes from. Parameterization produces both -- the rewritten
steps and the table of lifted values that seeds a skill's input contract.

Two mechanisms are used, because they fail differently:

Value parameters
    Paths, URLs, addresses, versions and long opaque ids are distinctive enough
    to replace by value, consistently across every step. Because the same value
    maps to the same placeholder everywhere, data flow between steps becomes
    visible (a path produced by step 2 and consumed by step 7).
Context parameters
    Device ordinals and similar bare integers are meaningless out of context --
    replacing the literal ``3`` everywhere would corrupt the trajectory. These
    are matched together with their surrounding syntax and replaced in place.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import Parameter, Step

# Ordered alternation: the first alternative that matches at a position wins,
# and finditer yields non-overlapping matches. That single pass resolves both
# priority and overlap, e.g. `/opt/cann/8.0.RC3/lib` is one path, not a path
# plus a version.
#
# Path segments also accept an already-substituted placeholder, so a redacted
# `/home/<USER>/bench/run.sh` stays one path instead of splitting into a
# fragment glued to `<USER>`.
_VALUE_SCAN = re.compile(
    r"""
    (?P<url>[a-z][a-z0-9+.\-]*://[^\s"'<>,;)\]]+)
  | (?P<ip>\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{2,5})?\b)
  | (?P<path>(?:[A-Za-z]:\\(?:(?:[\w.\-]+|<[A-Z][A-Z0-9_]*>)\\?)+|(?:/(?:[\w.\-]+|<[A-Z][A-Z0-9_]*>)){2,}/?))
  | (?P<version>\bv?\d+\.\d+(?:\.[\w\-]+)+\b)
  | (?P<hexid>\b[0-9a-fA-F]{16,}\b)
    """,
    re.VERBOSE,
)

_KIND_PREFIX = {
    "url": "URL",
    "ip": "HOST",
    "path": "PATH",
    "version": "VERSION",
    "hexid": "ID",
}

# Context-bound values: replaced together with the syntax that gives them
# meaning, never by bare value.
_CONTEXT_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "device",
        re.compile(
            r"(?i)(--device[-_a-z]*[= ]|device_ids?[\"']?\s*[:=]\s*|ASCEND_RT_VISIBLE_DEVICES\s*=)"
            r"(\d[\d,\s]*)"
        ),
        "<DEVICE_IDS>",
    ),
    (
        "rank",
        re.compile(r"(?i)(--(?:rank|local[-_]rank|node[-_]rank)[= ])(\d+)"),
        "<RANK>",
    ),
)

_GENERIC_KEYS = frozenset({"arg", "args", "cmd", "command", "input", "path", "value", "text", "content", "data"})

_MIN_VALUE_LENGTH = 6
_PATH_SEPARATORS = "/\\"


def _sanitize_key(key: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", key).strip("_").upper()
    return cleaned or ""


def _iter_strings(value: Any, key: str = "") -> Iterable[tuple[str, str]]:
    """Yield ``(key, string)`` pairs for every string nested in ``value``."""
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for child_key, child in value.items():
            yield from _iter_strings(child, str(child_key))
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_strings(child, key)


def _map_strings(value: Any, transform) -> Any:
    """Rebuild ``value`` with ``transform`` applied to every nested string."""
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, dict):
        return {key: _map_strings(child, transform) for key, child in value.items()}
    if isinstance(value, list):
        return [_map_strings(child, transform) for child in value]
    if isinstance(value, tuple):
        return tuple(_map_strings(child, transform) for child in value)
    return value


def _is_path_prefix(prefix: str, value: str) -> bool:
    """Whether ``prefix`` is ``value``'s parent directory at a path boundary."""
    trimmed = prefix.rstrip(_PATH_SEPARATORS)
    if not trimmed or len(value) <= len(trimmed):
        return False
    return value.startswith(trimmed) and value[len(trimmed)] in _PATH_SEPARATORS


@dataclass(slots=True)
class _Observation:
    """Every sighting of one distinct literal value across the trajectory."""

    value: str
    kind: str
    order: int
    count: int = 0
    keys: list[str] = field(default_factory=list)
    whole_keys: list[str] = field(default_factory=list)


class _Collector:
    """Collect literal values, then assign each a stable placeholder name."""

    def __init__(self) -> None:
        self._observations: dict[str, _Observation] = {}
        self._used_names: set[str] = set()
        self._counters: dict[str, int] = {}

    def observe(self, value: str, kind: str, key: str, whole_value: bool) -> None:
        observation = self._observations.get(value)
        if observation is None:
            observation = _Observation(value=value, kind=kind, order=len(self._observations))
            self._observations[value] = observation
        observation.count += 1
        if key and key not in observation.keys:
            observation.keys.append(key)
        if whole_value and key and key not in observation.whole_keys:
            observation.whole_keys.append(key)

    def _unique(self, name: str) -> str:
        candidate = name
        suffix = 2
        while candidate in self._used_names:
            candidate = f"{name}_{suffix}"
            suffix += 1
        self._used_names.add(candidate)
        return candidate

    def _name_for(self, observation: _Observation) -> str:
        for key in observation.whole_keys:
            if key.lower() not in _GENERIC_KEYS:
                return self._unique(_sanitize_key(key))
        prefix = _KIND_PREFIX.get(observation.kind, observation.kind.upper())
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return self._unique(f"{prefix}_{self._counters[prefix]}")

    def build(self) -> dict[str, Parameter]:
        """Return ``value -> Parameter`` for the values worth lifting."""
        observations = sorted(self._observations.values(), key=lambda item: item.order)
        # A value that appeared as a whole argument belongs to the input
        # contract and is always kept.
        anchors = [item for item in observations if item.whole_keys and item.kind == "path"]

        parameters: dict[str, Parameter] = {}
        for observation in observations:
            # A path seen only inside a longer string, below a path already
            # lifted, is left to the parent's placeholder, so the rewritten step
            # reads `<MODEL_PATH>/config.json` and the relationship survives.
            if (
                not observation.whole_keys
                and observation.kind == "path"
                and any(_is_path_prefix(anchor.value, observation.value) for anchor in anchors)
            ):
                continue
            parameters[observation.value] = Parameter(
                placeholder=f"<{self._name_for(observation)}>",
                kind=observation.kind,
                values=[observation.value],
                occurrences=observation.count,
                arg_keys=list(observation.keys),
            )
        return parameters


def _scan(text: str, key: str, collector: _Collector) -> None:
    for match in _VALUE_SCAN.finditer(text):
        kind = match.lastgroup or "path"
        value = match.group()
        if len(value) < _MIN_VALUE_LENGTH:
            continue
        collector.observe(value, kind, key, whole_value=match.span() == (0, len(text)))


def _apply_context_rules(text: str, seen: dict[str, list[str]]) -> str:
    result = text
    for name, pattern, placeholder in _CONTEXT_RULES:

        def _replace(match: re.Match[str], _name: str = name, _ph: str = placeholder) -> str:
            seen.setdefault(_name, []).append(match.group(2).strip())
            return f"{match.group(1)}{_ph}"

        result = pattern.sub(_replace, result)
    return result


def parameterize(steps: list[Step], *, include_results: bool = True) -> list[Parameter]:
    """Rewrite ``steps`` in place with placeholders and return the lifted values.

    ``Step.args_raw`` keeps the original arguments; ``Step.args`` receives the
    parameterized form. Result previews are rewritten too (unless disabled) so
    that a value produced by one step and consumed by another is recognisably
    the same placeholder.
    """
    collector = _Collector()
    for step in steps:
        for key, text in _iter_strings(step.args_raw):
            _scan(text, key, collector)
        if include_results and step.result_preview:
            _scan(step.result_preview, "", collector)

    by_value = collector.build()
    # Longest first: `/data/run` must not be substituted inside `/data/run/logs`.
    ordered = sorted(by_value.items(), key=lambda item: len(item[0]), reverse=True)
    context_values: dict[str, list[str]] = {}

    def _substitute(text: str) -> str:
        result = text
        for value, parameter in ordered:
            if value in result:
                result = result.replace(value, parameter.placeholder)
        return _apply_context_rules(result, context_values)

    for step in steps:
        step.args = _map_strings(step.args_raw, _substitute)
        if include_results and step.result_preview:
            step.result_preview = _substitute(step.result_preview)

    parameters = list(by_value.values())
    for name, _pattern, placeholder in _CONTEXT_RULES:
        hits = context_values.get(name, [])
        if not hits:
            continue
        parameters.append(
            Parameter(
                placeholder=placeholder,
                kind=f"context:{name}",
                values=sorted(set(hits)),
                occurrences=len(hits),
            )
        )
    return parameters
