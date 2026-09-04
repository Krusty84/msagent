"""/direct-skill-generation: distill a finished session into a SKILL.md draft."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from msagent.cli.bootstrap.initializer import initializer
from msagent.cli.handlers.session_history import load_history, trim_history
from msagent.cli.theme import console, theme
from msagent.core.logging import get_logger
from msagent.skills.factory import SkillFactory

from msagent.core.constants import (
    CONFIG_SKILL_EVOLVER_FILE_NAME,
    SKILL_EVOLVER_CONFIG_FOLDER_NAME,
)

logger = get_logger(__name__)

DEFAULT_VARIANT = "default"
DEFAULT_CATEGORY = "default"
_VARIANT_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
_HISTORY_BUDGET_RATIO = 0.6  # part of context window for session reply

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# Output-contract sentinel: the model found nothing worth saving. A stray pair of
# backticks is tolerated because models tend to copy the phrase as inline code.
_NOTHING_TO_SAVE = re.compile(r"^\s*`?\s*Nothing to save\.?\s*`?\s*$", re.IGNORECASE)

_REPLAY_SYSTEM_PROMPT = (
    "You are reviewing a COMPLETED agent session. The conversation that follows is a "
    "replay of that session for analysis only. Do not continue it and do not execute "
    "its tasks. After the replay you will receive exactly one instruction message; "
    "follow only that instruction."
)

@dataclass(frozen=True, slots=True)
class DirectSkillGenerationConfig:
    """Settings loaded from ~/.msagent/config/config.skill.evolver.yml."""

    active: str = DEFAULT_VARIANT
    prompt_file: str | None = None
    category: str = DEFAULT_CATEGORY
    output_dir: Path | None = None


class DirectSkillGenerationHandler:
    """Generate a SKILL.md draft from a thread history without mutating the thread."""

    def __init__(self, session) -> None:
        self.session = session

    async def handle(self, args: list[str]) -> None:
        """Distill a session into SKILL.md. Usage: direct-skill-generation [last|<thread-id>]."""
        try:
            target = args[0].strip() if args else None
            thread_id, messages = await load_history(self.session, target)

            if not messages:
                if target is None:
                    console.print_warning(
                        "Current thread has no messages. Use `/direct-skill-generation last` "
                        "for the previous session."
                    )
                else:
                    console.print_warning(f"No messages found for thread '{thread_id}'")
                console.print("")
                return

            root = initializer.app_paths.home / SKILL_EVOLVER_CONFIG_FOLDER_NAME / "prompts"
            cfg = self._load_config()
            template, prompt_source = await self._load_prompt_template(root, cfg)

            with console.console.status(
                f"[{theme.spinner_color}]Generating skill from thread {thread_id} "
                f"({len(messages)} messages)...[/{theme.spinner_color}]"
            ):
                skill_md = await self._generate_skill_md(messages, template, thread_id)

            if _NOTHING_TO_SAVE.match(skill_md):
                console.print_info(f"Nothing to save: no durable learning found in thread {thread_id}")
                console.print("")
                return

            skill_path = await self._write_skill(skill_md, cfg, thread_id)
            console.print_success(f"Skill draft saved to {skill_path}")
            console.print(f"[muted]Prompt source: {prompt_source}[/muted]")
            console.print("")
        except Exception as exc:
            console.print_error(f"Error generating skill: {exc}")
            console.print("")
            logger.exception("Skill generation failed")

    async def _build_skill_library_snapshot(self) -> str:
            """Programmatic inventory injected as {skill_library} (replaces a tool call)."""
            ctx = self.session.context
            skills = await initializer.refresh_cached_skills(agent=ctx.agent, working_dir=ctx.working_dir)
            if not skills:
                return "The skill library is currently empty."
            return "\n".join(
                f"- {s.display_name}: {s.description or 'no description'}"
                for s in sorted(skills, key=lambda s: s.display_name.casefold())
            )

    @classmethod
    def _sanitize_model_output(cls, text: str) -> str:
        """Remove reasoning blocks the model may emit around the payload."""
        return _THINK_BLOCK.sub("", text).strip()

    @staticmethod
    def _load_config() -> DirectSkillGenerationConfig:
        """Read config.skill.evolver.yml; fall back to packaged default, then dataclass defaults."""
        from importlib.resources import files

        candidates = (
            initializer.app_paths.config_dir / CONFIG_SKILL_EVOLVER_FILE_NAME.name,
            Path(str(files("resources") / "configs" / "default" / CONFIG_SKILL_EVOLVER_FILE_NAME.name)),
        )
        config_path = next((path for path in candidates if path.is_file()), None)
        if config_path is None:
            return DirectSkillGenerationConfig()

        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.warning("Invalid %s; using defaults", config_path)
            return DirectSkillGenerationConfig()
        if not isinstance(data, dict):
            return DirectSkillGenerationConfig()

        active = str(data.get("active") or DEFAULT_VARIANT).strip()
        if not _VARIANT_NAME_PATTERN.fullmatch(active):
            logger.warning("Unsafe variant name '%s'; using '%s'", active, DEFAULT_VARIANT)
            active = DEFAULT_VARIANT

        raw_output_dir = data.get("output_dir")
        return DirectSkillGenerationConfig(
            active=active,
            prompt_file=(str(data["prompt_file"]).strip() if data.get("prompt_file") else None),
            category=str(data.get("category") or DEFAULT_CATEGORY).strip() or DEFAULT_CATEGORY,
            output_dir=Path(str(raw_output_dir)).expanduser() if raw_output_dir else None,
        )

    @classmethod
    def _prepare_replay_messages(
        cls,
        messages: list[AnyMessage],
        llm,
        context_window: int | None,
    ) -> tuple[list[AnyMessage], int]:
        """Normalize and trim session messages so they replay as a valid conversation."""
        normalized = [cls._normalize_replay_message(m) for m in messages]
        normalized = cls._drop_trailing_orphan_tool_calls(normalized)
        # head_tail: keep the task statement, cut the middle, keep the newest tail.
        return trim_history(normalized, llm, context_window, budget_ratio=_HISTORY_BUDGET_RATIO)

    @staticmethod
    def _normalize_replay_message(message: AnyMessage) -> AnyMessage:
        """Fold private reasoning into visible text; strip fields APIs reject on input."""
        if not isinstance(message, AIMessage):
            return message
        kwargs = dict(message.additional_kwargs or {})
        reasoning = kwargs.pop("reasoning_content", None)
        if not reasoning:
            return message
        text = message.text if isinstance(getattr(message, "text", None), str) else str(message.content)
        merged = f"<past_reasoning>\n{reasoning}\n</past_reasoning>\n\n{text}"
        return message.model_copy(update={"content": merged, "additional_kwargs": kwargs})

    @staticmethod
    def _drop_trailing_orphan_tool_calls(messages: list[AnyMessage]) -> list[AnyMessage]:
        """Drop a trailing tool call that never received its result (interrupted session)."""
        result = list(messages)
        while result:
            last = result[-1]
            if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
                result.pop()
                continue
            break
        return result
    
    async def _load_prompt_template(
        self,
        root: Path,
        cfg: DirectSkillGenerationConfig,
    ) -> tuple[str, str]:
        """Resolve the prompt: user variant folder first, then the packaged template."""
        from importlib.resources import files as resource_files

        packaged_root = (
            Path(str(resource_files("resources") / "configs" / "default"))
            / SKILL_EVOLVER_CONFIG_FOLDER_NAME
            / "prompts"
        )

        for base in (root, packaged_root):
            variant_dir = base / cfg.active
            if not variant_dir.is_dir():
                continue

            if cfg.prompt_file:
                candidate = variant_dir / cfg.prompt_file
                found = [candidate] if candidate.is_file() else []
                if not found:
                    console.print_warning(
                        f"prompt_file '{cfg.prompt_file}' not found in {variant_dir}; "
                        "using all *.md files of this variant instead"
                    )
                    found = sorted(variant_dir.glob("*.md"))
            else:
                found = sorted(variant_dir.glob("*.md"))

            if found:
                parts = [await asyncio.to_thread(p.read_text, encoding="utf-8") for p in found]
                source = str(found[0]) if len(found) == 1 else str(variant_dir)
                return "\n\n".join(parts), source

        raise ValueError(
            f"No prompt found for variant '{cfg.active}': checked {root} and packaged defaults. "
            "Verify config.skill.evolver.yml (active / prompt_file) and the seeded prompt folder."
        )

    async def _generate_skill_md(
        self,
        messages: list[AnyMessage],
        template: str,
        thread_id: str,
    ) -> str:
        """Replay the real session messages to the LLM, then send the instruction."""
        ctx = self.session.context
        llm_config = await initializer.load_llm_config(ctx.model, ctx.working_dir)
        llm = initializer.llm_factory.create(llm_config)

        replay, omitted = self._prepare_replay_messages(messages, llm, llm_config.context_window)

        instruction = template
        for placeholder, value in (
            ("{skill_library}", await self._build_skill_library_snapshot()),
            ("{agent}", ctx.agent),
            ("{thread_id}", thread_id),
            ("{working_dir}", str(ctx.working_dir)),
            ("{history}", "(the full session is replayed above)"),  # совместимость со старыми шаблонами
        ):
            instruction = instruction.replace(placeholder, value)
        if omitted:
            instruction = (
                f"[Note: {omitted} messages from the middle of the session were omitted "
                f"due to context limits.]\n\n{instruction}"
            )

        payload: list[AnyMessage] = [
            SystemMessage(content=_REPLAY_SYSTEM_PROMPT),
            *replay,
            HumanMessage(content=instruction),
        ]

        response = await llm.ainvoke(payload)
        text = getattr(response, "text", None)
        raw = self._sanitize_model_output(text if isinstance(text, str) else str(response.content))
        return self._strip_code_fence(raw)

    async def _write_skill(
        self,
        skill_md: str,
        cfg: DirectSkillGenerationConfig,
        thread_id: str,
    ) -> Path:
        """Write SKILL.md under <output>/<category>/<name>/ without overwriting."""
        content = self._ensure_frontmatter(skill_md, thread_id)
        name = self._extract_skill_name(content, thread_id)

        output_root = cfg.output_dir or (Path(self.session.context.working_dir) / "skills")
        base_dir = output_root / cfg.category
        skill_dir = base_dir / name
        suffix = 2
        while (skill_dir / "SKILL.md").exists():
            skill_dir = base_dir / f"{name}-{suffix}"
            suffix += 1

        await asyncio.to_thread(skill_dir.mkdir, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        footer = f"\n\n<!-- generated by /direct-skill-generation | thread: {thread_id} | {stamp} -->\n"
        await asyncio.to_thread(
            (skill_dir / "SKILL.md").write_text,
            content.rstrip() + footer,
            encoding="utf-8",
        )
        return skill_dir / "SKILL.md"

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """Unwrap the whole answer if the model fenced it despite instructions."""
        stripped = text.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            first_newline = stripped.find("\n")
            if first_newline != -1:
                return stripped[first_newline + 1 : -3].strip()
        return stripped

    @staticmethod
    def _ensure_frontmatter(content: str, thread_id: str) -> str:
        """Prepend minimal frontmatter when the model omitted or broke it."""
        try:
            frontmatter = SkillFactory.parse_frontmatter(content)
            has_name = bool(str(frontmatter.get("name") or "").strip())
        except ValueError:
            has_name = False
        if has_name:
            return content

        fallback = f"session-skill-{thread_id[:8]}"
        header = (
            f"---\nname: {fallback}\n"
            f"description: Skill distilled from session {thread_id[:8]}\n---\n\n"
        )
        return header + content.lstrip()

    @staticmethod
    def _extract_skill_name(content: str, thread_id: str) -> str:
        """Derive a filesystem-safe kebab-case folder name from frontmatter."""
        try:
            frontmatter = SkillFactory.parse_frontmatter(content)
        except ValueError:
            frontmatter = {}
        raw = str(frontmatter.get("name") or "").strip()
        slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
        return slug or f"session-skill-{thread_id[:8]}"