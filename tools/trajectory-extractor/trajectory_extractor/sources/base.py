"""Shared shape returned by every trajectory source."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Phase, RawEvent


@dataclass(slots=True)
class SourceResult:
    """What one reader contributes to the merged trajectory."""

    name: str
    events: list[RawEvent] = field(default_factory=list)
    user_messages: list[str] = field(default_factory=list)
    phases: list[Phase] = field(default_factory=list)
    offloaded_context: list[str] = field(default_factory=list)
    thread_id: str = ""
    agent: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.events or self.user_messages or self.phases or self.offloaded_context)
