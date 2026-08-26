"""Initializer for assembling deepagents runtime dependencies."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from msagent.agents.context import AgentContext
from msagent.agents.factory import AgentFactory
from msagent.cli.bootstrap.timer import timer
from msagent.client.remote import RemoteAgent
from msagent.configs import (
    AgentConfig,
    BatchAgentConfig,
    BatchCheckpointerConfig,
    BatchLLMConfig,
    CheckpointerConfig,
    CheckpointerProvider,
    ConfigRegistry,
    LLMConfig,
    MCPConfig,
    ToolApprovalConfig,
)
from msagent.core.constants import (
    CONFIG_CHECKPOINTS_URL_FILE_NAME,
    CONFIG_MCP_CACHE_DIR,
    CONFIG_MCP_FILE_NAME,
    CONFIG_MCP_OAUTH_DIR,
    CONFIG_SKILLS_DIR,
)
from msagent.llms.factory import LLMFactory
from msagent.mcp.factory import MCPFactory
from msagent.server.config import ServerConfig
from msagent.server.process import ServerProcess
from msagent.skills.factory import Skill, SkillFactory
from msagent.testing.fake_graph import FakeGraph
from msagent.tools.factory import ToolFactory, ToolPreview

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


class Initializer:
    """Centralized service for initializing and caching runtime resources."""

    def __init__(self) -> None:
        self.tool_factory = ToolFactory()
        self.skill_factory = SkillFactory()
        self.llm_factory = LLMFactory()
        self.mcp_factory = MCPFactory(tool_factory=self.tool_factory)
        self.agent_factory = AgentFactory(
            llm_factory=self.llm_factory,
            tool_factory=self.tool_factory,
        )

        self.cached_llm_tools: list[BaseTool] = []
        self.cached_tools_in_catalog: list[BaseTool | object] = []
        self.cached_agent_skills: list[Skill] = []
        self.cached_mcp_server_names: list[str] = []

        self._registries: dict[Path, ConfigRegistry] = {}

    def get_registry(self, working_dir: Path) -> ConfigRegistry:
        if working_dir not in self._registries:
            self._registries[working_dir] = ConfigRegistry(working_dir)
        return self._registries[working_dir]

    async def load_llms_config(self, working_dir: Path) -> BatchLLMConfig:
        return await self.get_registry(working_dir).load_llms()

    async def load_llm_config(self, model: str, working_dir: Path) -> LLMConfig:
        return await self.get_registry(working_dir).get_llm(model)

    async def load_checkpointers_config(self, working_dir: Path) -> BatchCheckpointerConfig:
        return await self.get_registry(working_dir).load_checkpointers()

    async def load_agents_config(self, working_dir: Path) -> BatchAgentConfig:
        return await self.get_registry(working_dir).load_agents()

    async def load_agent_config(self, agent: str | None, working_dir: Path) -> AgentConfig:
        return await self.get_registry(working_dir).get_agent(agent)

    async def load_mcp_config(self, working_dir: Path) -> MCPConfig:
        return await self.get_registry(working_dir).load_mcp()

    async def save_mcp_config(self, mcp_config: MCPConfig, working_dir: Path) -> None:
        await self.get_registry(working_dir).save_mcp(mcp_config)

    async def update_agent_llm(self, agent_name: str, new_llm_name: str, working_dir: Path) -> None:
        await self.get_registry(working_dir).update_agent_llm(agent_name, new_llm_name)

    async def update_default_agent(self, agent_name: str, working_dir: Path) -> None:
        await self.get_registry(working_dir).update_default_agent(agent_name)

    async def add_agent_skill_pattern(self, agent_name: str, skill_pattern: str, working_dir: Path) -> bool:
        return await self.get_registry(working_dir).add_agent_skill_pattern(agent_name, skill_pattern)

    async def load_user_memory(self, working_dir: Path) -> str:
        return await self.get_registry(working_dir).load_user_memory()

    def resolve_skills_dirs(self, working_dir: Path) -> list[Path]:
        """Resolve skill search directories in precedence order."""
        return self._resolve_skills_dirs(working_dir)

    @asynccontextmanager
    async def get_checkpointer(self, agent: str, working_dir: Path) -> AsyncIterator[BaseCheckpointSaver]:
        """Open the configured checkpointer for a given agent."""
        agent_config = await self.load_agent_config(agent, working_dir)
        checkpointer_ctx = self._create_checkpointer(
            cast(CheckpointerConfig | None, agent_config.checkpointer),
            str(working_dir / CONFIG_CHECKPOINTS_URL_FILE_NAME),
        )
        checkpointer = await checkpointer_ctx.__aenter__()
        try:
            yield checkpointer
        finally:
            await checkpointer_ctx.__aexit__(None, None, None)

    async def create_graph(
        self,
        agent: str | None,
        model: str | None,
        working_dir: Path,
    ) -> tuple[CompiledStateGraph | FakeGraph, Callable[[], Awaitable[None]]]:
        if os.getenv("MSAGENT_FAKE_BACKEND", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            fake_graph = FakeGraph()
            self.cached_llm_tools = []
            self.cached_tools_in_catalog = []
            self.cached_agent_skills = []
            self.cached_mcp_server_names = []

            async def fake_cleanup() -> None:
                return None

            return fake_graph, fake_cleanup

        registry = self.get_registry(working_dir)

        with timer("Load configs"):
            if model:
                agent_config, llm_config, mcp_config = await asyncio.gather(
                    registry.get_agent(agent),
                    registry.get_llm(model),
                    registry.load_mcp(),
                )
            else:
                agent_config, mcp_config = await asyncio.gather(
                    registry.get_agent(agent),
                    registry.load_mcp(),
                )
                llm_config = None

        with timer("Load approval config"):
            load_approval = getattr(registry, "load_approval", None)
            if callable(load_approval):
                approval_config = load_approval()
            else:
                approval_config = ToolApprovalConfig()
            interrupt_on = approval_config.to_interrupt_on_payload()

        with timer("Create checkpointer"):
            checkpointer_ctx = self._create_checkpointer(
                cast(CheckpointerConfig | None, agent_config.checkpointer),
                str(working_dir / CONFIG_CHECKPOINTS_URL_FILE_NAME),
            )
            checkpointer = await checkpointer_ctx.__aenter__()

        with timer("Create MCP client"):
            default_timeout = (
                float(agent_config.tools.execution_timeout_seconds) if agent_config.tools is not None else None
            )
            mcp_client = await self.mcp_factory.create(
                config=mcp_config,
                cache_dir=working_dir / CONFIG_MCP_CACHE_DIR,
                oauth_dir=working_dir / CONFIG_MCP_OAUTH_DIR,
                sandbox_bindings=None,
                default_invoke_timeout=default_timeout,
            )
            mcp_module_map = dict(getattr(mcp_client, "module_map", {}) or {})

        with timer("Load skills metadata"):
            skills_dirs = self._resolve_skills_dirs(working_dir)
            skill_map = await self.skill_factory.load_skills(skills_dirs)
            cached_skills = [skill for category in skill_map.values() for skill in category.values()]
            skills_config = getattr(agent_config, "skills", None)
            skill_patterns = list(skills_config.patterns or []) if skills_config is not None else []
            filtered_skills = self._filter_skills_by_patterns(
                cached_skills,
                patterns=skill_patterns,
            )
            runtime_skills_dirs = (
                skills_dirs if any(pattern and not pattern.startswith("!") for pattern in skill_patterns) else None
            )

        with timer("Create and compile graph"):
            graph = await self.agent_factory.create(
                config=agent_config,
                working_dir=working_dir,
                context_schema=AgentContext,
                checkpointer=checkpointer,
                mcp_client=mcp_client,
                llm_config=llm_config,
                skills_dir=runtime_skills_dirs,
                allowed_skills=filtered_skills,
                sandbox_bindings=None,
                interrupt_on=interrupt_on,
            )

        self.cached_llm_tools = list(getattr(graph, "_llm_tools", []))
        self.cached_tools_in_catalog = list(
            getattr(graph, "_tools_in_catalog", self.cached_llm_tools) or self.tool_factory.get_catalog_tools()
        )
        self.cached_agent_skills = filtered_skills
        self.cached_mcp_server_names = self._resolve_cached_mcp_server_names(
            tools=self.cached_llm_tools,
            mcp_config=mcp_config,
            mcp_module_map=mcp_module_map,
        )

        async def cleanup() -> None:
            await mcp_client.close()
            await checkpointer_ctx.__aexit__(None, None, None)

        return graph, cleanup

    def _resolve_skills_dirs(self, working_dir: Path) -> list[Path]:
        candidates = [
            working_dir / "skills",
            self.skill_factory.get_default_skills_dir(),
            working_dir / CONFIG_SKILLS_DIR,
        ]

        unique_paths: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            normalized = str(path.resolve())
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_paths.append(path)
        return unique_paths

    def _resolve_cached_mcp_server_names(
        self,
        *,
        tools: list[BaseTool],
        mcp_config: MCPConfig,
        mcp_module_map: dict[str, str],
    ) -> list[str]:
        enabled_servers = {name for name, server in mcp_config.servers.items() if server.enabled}
        if not enabled_servers:
            return []

        visible_servers: set[str] = set()
        for tool in tools:
            tool_name = self.agent_factory._tool_name(tool)
            module, _raw_name = self.agent_factory._resolve_mcp_tool_identity(
                tool_name=tool_name,
                mcp_module_map=mcp_module_map,
                mcp_servers=enabled_servers,
            )
            if module != "unknown":
                visible_servers.add(module)

        return [name for name in mcp_config.servers.keys() if name in visible_servers]

    @asynccontextmanager
    async def _create_checkpointer(
        self,
        config: CheckpointerConfig | None,
        db_path: str | None = None,
    ) -> AsyncIterator[BaseCheckpointSaver]:
        if config is None or config.type == CheckpointerProvider.MEMORY:
            yield InMemorySaver()
            return

        if config.type == CheckpointerProvider.SQLITE:
            sqlite_path = config.connection_string or db_path
            if sqlite_path:
                import aiosqlite

                conn = await aiosqlite.connect(sqlite_path)
                try:
                    yield AsyncSqliteSaver(conn)
                finally:
                    await conn.close()
                return

        yield InMemorySaver()

    @staticmethod
    def _filter_skills_by_patterns(skills: list[Skill], patterns: list[str]) -> list[Skill]:
        if not patterns:
            return []

        positive_patterns = [p for p in patterns if p and not p.startswith("!")]
        negative_patterns = [p[1:] for p in patterns if p.startswith("!")]
        if not positive_patterns:
            return []

        def matches(pattern: str, *, category: str, name: str) -> bool:
            parts = pattern.split(":")
            if len(parts) != 2:
                return False
            category_p, name_p = parts
            return fnmatch(category, category_p) and fnmatch(name, name_p)

        filtered: list[Skill] = []
        for skill in skills:
            if not any(matches(pattern, category=skill.category, name=skill.name) for pattern in positive_patterns):
                continue
            if any(matches(pattern, category=skill.category, name=skill.name) for pattern in negative_patterns):
                continue
            filtered.append(skill)
        return filtered

    @asynccontextmanager
    async def get_graph(
        self,
        agent: str | None,
        model: str | None,
        working_dir: Path,
    ) -> AsyncIterator[CompiledStateGraph | FakeGraph]:
        graph, cleanup = await self.create_graph(agent, model, working_dir)
        try:
            yield graph
        finally:
            await cleanup()

    async def start_server(
        self,
        agent: str | None,
        model: str | None,
        working_dir: Path,
    ) -> tuple[RemoteAgent, ServerProcess]:
        """Start the agent server subprocess and return a connected client.

        Also populates the client-side catalog caches (tools/skills/MCP) from
        the local configuration, since the compiled graph now lives in the
        server process.
        """
        registry = self.get_registry(working_dir)
        agent_config = await registry.get_agent(agent)
        server_config = ServerConfig(
            agent=agent,
            model=model,
            working_dir=str(working_dir),
            recursion_limit=getattr(agent_config, "recursion_limit", None),
            checkpointer_path=str(working_dir / CONFIG_CHECKPOINTS_URL_FILE_NAME),
            mcp_config_path=str(working_dir / CONFIG_MCP_FILE_NAME),
            audit_enabled=True,
        )
        server = ServerProcess(server_config=server_config)
        await server.start()
        try:
            await server.wait_for_graph_ready("agent")
        except BaseException:
            # Includes asyncio.CancelledError: never leak a half-started server.
            server.stop()
            raise
        remote_agent = RemoteAgent(url=server.url)
        await self._refresh_client_catalogs(agent, model, working_dir, remote_agent=remote_agent)
        return remote_agent, server

    async def _refresh_client_catalogs(
        self,
        agent: str | None,
        model: str | None,
        working_dir: Path,
        remote_agent: RemoteAgent | None = None,
    ) -> None:
        """Populate client-side tool/skill/MCP catalog caches.

        In serverized mode the authoritative tool/MCP listing comes from the
        server's ``/catalog`` route (what the graph actually loaded); skills
        stay locally derived (same scan + pattern filter as the server) so the
        cached objects keep their full metadata. Falls back to full local
        config derivation when the server is unreachable.
        """
        server_data: dict[str, Any] | None = None
        if remote_agent is not None:
            try:
                server_data = await remote_agent.fetch_catalog()
            except Exception:
                logger.warning(
                    "Failed to fetch server catalog; falling back to config derivation",
                    exc_info=True,
                )

        await self._derive_skills_catalog(agent, working_dir)

        if server_data:
            previews = [
                ToolPreview(
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                )
                for item in server_data.get("tools") or []
                if isinstance(item, dict)
            ]
            self.cached_llm_tools = previews
            self.cached_tools_in_catalog = previews
            self.cached_mcp_server_names = list(server_data.get("mcp_servers") or [])
            return

        registry = self.get_registry(working_dir)
        agent_config = await registry.get_agent(agent)
        mcp_config = await registry.load_mcp()

        tool_patterns = list(agent_config.tools.patterns or []) if agent_config.tools is not None else []
        self.cached_tools_in_catalog = self._derive_tool_catalog(tool_patterns=tool_patterns)
        self.cached_llm_tools = list(self.cached_tools_in_catalog)

        enabled_servers = {name for name, server in mcp_config.servers.items() if server.enabled}
        visible: list[str] = []
        for name in sorted(enabled_servers):
            if self._pattern_allows(f"mcp:{name}:*", tool_patterns) or self._pattern_allows(
                "mcp:*:*",
                tool_patterns,
            ):
                visible.append(name)
        self.cached_mcp_server_names = visible

    async def _derive_skills_catalog(self, agent: str | None, working_dir: Path) -> None:
        """Scan and pattern-filter the local skills catalog."""
        registry = self.get_registry(working_dir)
        agent_config = await registry.get_agent(agent)

        skills_config = getattr(agent_config, "skills", None)
        skill_patterns = list(skills_config.patterns or []) if skills_config is not None else []
        skills_dirs = self._resolve_skills_dirs(working_dir)
        skill_map = await self.skill_factory.load_skills(skills_dirs)
        all_skills = [skill for category in skill_map.values() for skill in category.values()]
        self.cached_agent_skills = self._filter_skills_by_patterns(all_skills, patterns=skill_patterns)

    @staticmethod
    def _pattern_allows(candidate: str, patterns: list[str]) -> bool:
        """Check a ``category:module:name`` candidate against +/- patterns."""
        positives = [p for p in patterns if p and not p.startswith("!")]
        if not positives:
            return False
        if not any(fnmatch(candidate, p) for p in positives):
            return False
        return not any(fnmatch(candidate, p[1:]) for p in patterns if p.startswith("!"))

    def _derive_tool_catalog(self, *, tool_patterns: list[str]) -> list[Any]:
        """Return the client-visible tool catalog derived from tool patterns."""
        from msagent.tools.catalog import (
            fetch_skills,
            fetch_tools,
            get_skill,
            get_tool,
            run_tool,
        )
        from msagent.tools.web_search import web_search

        runtime_tools: list[Any] = [fetch_tools, get_tool, run_tool, fetch_skills, get_skill, web_search]
        visible: list[Any] = []
        for tool in runtime_tools:
            tool_name = getattr(tool, "name", "unknown")
            if self._pattern_allows(f"impl:deepagents:{tool_name}", tool_patterns):
                visible.append(tool)
        # Fall back to the built-in previews so /tools stays informative even
        # when the pattern set only matches a subset we cannot derive locally.
        return visible or list(self.tool_factory.get_catalog_tools())


initializer = Initializer()
