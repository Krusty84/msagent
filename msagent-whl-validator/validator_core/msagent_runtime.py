"""Reusable isolated runtime for one pytest validation case."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from validator_core.agent_runner import RunResult, run_msagent


logger = logging.getLogger(__name__)


@dataclass
class MsagentRuntime:
    """A workspace and MSAGENT_HOME shared by calls in one test case."""

    provider: str
    model: str
    workspace_dir: Path
    msagent_home: Path
    artifact_dir: Path
    extra_env: dict[str, str]
    executable: str = "msagent"
    _invocation_index: int = field(default=0, init=False, repr=False)

    def run(self, prompt: str, *, agent_name: str | None = None) -> RunResult:
        self._invocation_index += 1
        invocation_dir = self.artifact_dir / f"invocation-{self._invocation_index:02d}"
        result = run_msagent(
            prompt=prompt,
            workspace_dir=str(self.workspace_dir),
            extra_env=self.extra_env,
            agent_name=agent_name,
            executable=self.executable,
            artifact_dir=invocation_dir,
        )
        logger.info("msagent trace retained at %s", result.trace_path)
        return result
