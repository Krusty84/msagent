"""Extract normalized step graphs from msagent trajectories.

Reads what an msagent session already recorded -- LangGraph checkpoints, the
audit log, offloaded conversation history and optional ``--trace-jsonl`` files --
and turns it into a redacted, retry-collapsed, parameterized step graph with
script candidates and error/recovery pairs marked.

The output is input material for SKILL.md synthesis, not a skill itself: it does
the mechanical work (deduplication, placeholder lifting, secret scrubbing) so
that the judgement work is done against clean data.
"""

from .models import (
    Parameter,
    Phase,
    RawEvent,
    Recovery,
    ScriptCandidate,
    Step,
    TrajectoryDocument,
)
from .pipeline import ALL_SOURCES, ExtractionRequest, discover_threads, extract

__version__ = "0.1.0"

__all__ = [
    "ALL_SOURCES",
    "ExtractionRequest",
    "Parameter",
    "Phase",
    "RawEvent",
    "Recovery",
    "ScriptCandidate",
    "Step",
    "TrajectoryDocument",
    "discover_threads",
    "extract",
]
