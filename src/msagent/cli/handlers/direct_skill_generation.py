"""/direct-skill-generation: distill a finished session into a SKILL.md draft."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from langchain_core.messages import AnyMessage, HumanMessage, get_buffer_string

from msagent.cli.bootstrap.initializer import initializer
from msagent.cli.handlers.session_history import load_history, trim_history
from msagent.cli.theme import console, theme
from msagent.core.logging import get_logger
from msagent.skills.factory import SkillFactory

logger = get_logger(__name__)

from msagent.core.constants import (
    CONFIG_SKILL_EVOLVER_FILE_NAME,
    SKILL_EVOLVER_CONFIG_FOLDER_NAME,
)

DEFAULT_VARIANT = "default"
DEFAULT_CATEGORY = "default"
_VARIANT_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")

DEFAULT_SKILL_REVIEW_PROMPT = """
Review the conversation and tool trajectory below and update the reusable skill library only when there is strong evidence of durable learning.

Be conservative. The goal is not to document what happened. The goal is to preserve only knowledge that would materially improve a future agent's behavior on another task of the same class.

When no qualifying change is needed, reply exactly:

Nothing to save.

Do not summarize or recap the session.

# Core principle

Extract the **knowledge delta**, not the task narrative.

For every candidate learning, ask:

> If this knowledge had already existed in the relevant skill before this session, would it likely have changed the agent's behavior in a useful way?

If not, do not save it.

Successful problem solving by itself is not evidence of reusable learning.

A sequence of actions that happened to end in success is not automatically a reusable workflow.

# MANDATORY REVIEW SEQUENCE

Follow these phases in order.

## Phase 1 — Inventory

Before any mutation, call `skills_list` exactly once to inspect the existing skill library.

Do not create, update, rename, or write support files before this inventory.

## Phase 2 — Identify candidate learnings

Review the trajectory for possible reusable knowledge.

Candidates may come from:

- explicit user corrections to workflow, output format, style, sequencing, or task approach;
- non-obvious debugging or recovery techniques;
- durable environment or repository-specific procedures;
- tool constraints, parameters, or invocation ordering that proved materially important;
- repeated implementation or testing patterns;
- missing, incomplete, wrong, or outdated guidance in a skill used during the session;
- evidence that an existing skill's trigger description is genuinely ambiguous or omits a common way the task is expressed;
- durable domain, API, provider, repository, or environment facts that future agents are unlikely to derive quickly on their own.

In autonomous or daemon-triggered sessions, the absence of human correction is normal. The agent's own trajectory can provide evidence, but autonomous discoveries must meet the same quality threshold as user-provided corrections.

Do not treat ordinary competent execution as a candidate merely because it worked.

## Phase 3 — Inspect overlaps

For each candidate that survives initial review, identify existing skills whose name or description could plausibly govern the same class of work.

Call `skill_view` for those skills before deciding whether to mutate anything.

Inspect the most relevant existing umbrella skills rather than creating a new skill from the task wording.

If a candidate is already adequately covered, do not save it again.

## Phase 4 — Apply the eligibility test

A candidate is save-worthy only when **all five conditions** below are satisfied.

### 1. Future applicability

The learning is likely to apply to another future task or session in the same class.

It must not be merely a fact about one ticket, one file, one PR, one command invocation, one incident, one report, or one execution.

### 2. Non-obviousness

A capable future agent is unlikely to derive the learning immediately from:

- the task itself;
- repository structure;
- source code that will still be available;
- ordinary documentation;
- a clear error message;
- standard tool behavior;
- common engineering practice.

Do not save routine reasoning as a skill.

### 3. Evidence

The trajectory provides enough evidence that the learning is actually correct or useful.

Good evidence includes one or more of:

- explicit user confirmation that the rule should apply to future tasks of this class;
- repetition across independent parts of the trajectory;
- a clear technical mechanism explaining why the step matters;
- diagnostics that directly establish the cause;
- a controlled comparison showing that one procedure is required or materially more reliable;
- an existing skill being demonstrably incomplete, incorrect, or outdated.

Do not infer causality merely because action B occurred before success.

Do not convert coincidence into procedure.

### 4. Durability

The learning is expected to remain useful across future sessions.

Do not save:

- temporary process state;
- transient cache or network conditions;
- today's IDs or values;
- ephemeral service state;
- temporary external outages;
- one-time generated output;
- values specific to the current run.

Environment-specific knowledge is valid when it describes a persistent property of the working environment, repository, build process, toolchain, provider, or infrastructure.

### 5. Novelty

The learning is not already adequately represented in an existing skill or support file.

Prefer improving existing guidance over duplicating it.

If any of these five conditions fails, do not save the candidate.

# HIGH-VALUE SIGNALS

The following can justify an update when they also pass the full eligibility test.

## User workflow corrections

A user correction is high-value when it changes how future instances of the same task class should be performed.

Examples:

- reproduce the failure before refactoring;
- inspect generated artifacts before editing source;
- run a required generation step before testing;
- use a particular verification sequence for this project.

When the correction supersedes existing skill guidance, rewrite the old instruction so that only the corrected workflow remains.

Do not append the new workflow beside the superseded one as historical commentary.

## Persistent style or output corrections

Save a style, formatting, verbosity, or presentation correction only when the user states or strongly implies that it should apply to future instances of the same class of task.

Examples of durable signals:

- "For code reviews, always show blocking issues first."
- "When writing these reports, do not include an executive summary."
- "For future build-debugging answers, give the exact command before the explanation."

Do not save purely local steering such as:

- "just give me the command this time";
- "shorter";
- "skip the explanation here";
- "don't use a table for this one."

Local response steering is not durable preference.

## Autonomous debugging discoveries

A debugging path can be reusable when the trajectory establishes a non-obvious diagnostic or remediation rule for a class of failures.

Prefer rules such as:

> When generated symbols are missing, verify the code-generation step and toolchain version before modifying source.

Do not save narratives such as:

> The agent tried A, then B, then C, and C worked.

The reusable content is the decision rule, not the chronology.

## Tool-usage discoveries

Tool usage is a valid signal only when the trajectory shows that a parameter, constraint, invocation order, or combination is necessary or materially more reliable than an obvious alternative.

Do not save incidental tool sequences.

Using `grep`, then `find`, then `sed` successfully is not a skill.

Discovering that a particular command must run before another because the second consumes generated state may be a skill.

## Repeated code-change patterns

A repeated implementation pattern can be reusable when:

- it appears in multiple independent edits;
- there is a clear reason for the pattern;
- it is not already obvious from the codebase conventions.

Do not generalize from a single defensive edit unless there is additional evidence that the pattern is structurally required.

# DO NOT SAVE

Reply `Nothing to save.` when the session was routine and produced no durable knowledge.

Specifically, do not save the following.

## Routine execution

Do not save normal engineering behavior such as:

- reading an error;
- locating the referenced file;
- fixing an obvious typo;
- running tests after editing;
- checking documentation;
- adding a null check where the type clearly permits null;
- following an error message's explicit remediation.

## One-off task narrative

Do not create skills from:

- a specific PR;
- one issue;
- today's report;
- a single customer case;
- an exact error string;
- one file repair;
- one incident timeline;
- one feature codename.

A skill must represent a class of work.

## Weak post-hoc conclusions

Do not save a rule merely because:

1. the agent did something;
2. the task succeeded afterward.

Require evidence of mechanism, necessity, repetition, diagnostics, or explicit confirmation.

## Transient failures

Do not save temporary conditions such as:

- a cache happened to be stale;
- a server temporarily returned an error;
- a dependency download briefly failed;
- one process was stuck;
- a retry happened to succeed.

A transient error becomes save-worthy only when its resolution exposes a durable and non-obvious procedure that future sessions in the same environment are likely to need.

## Negative tool folklore

Never encode durable standalone claims such as:

- "tool X is broken";
- "command Y does not work";
- "never use provider Z";
- "feature A is unreliable."

These statements age badly and can cause future agents to refuse valid approaches.

When there is a durable setup or usage requirement, capture the **positive working procedure** instead.

Example:

GOOD:

> Before invoking X in this repository, initialize Y with `<command>`.

BAD:

> X does not work unless manually fixed.

## Information already present in durable source material

Do not copy repository files, documentation, schemas, or source code into the skill library when a future agent can simply read the authoritative source.

Save only the non-obvious guidance required to locate, interpret, sequence, or correctly use that material.

# AUTONOMOUS / DAEMON-TRIGGERED SESSIONS

This review may run after a trajectory containing no human user interaction.

Do not assume that "no user correction" means "Nothing to save."

Instead, inspect whether the agent discovered durable knowledge through:

- a non-obvious diagnostic path;
- a persistent environment constraint;
- a required tool order;
- repeated implementation behavior with a clear mechanism;
- an undocumented repository or provider convention;
- a skill that proved incomplete or misleading.

However, do not lower the quality threshold for autonomous trajectories.

Autonomous trajectories are especially vulnerable to false causal inference.

Before saving an autonomous discovery, ask:

- Did the trajectory establish why the technique worked?
- Was the behavior necessary or materially advantageous?
- Could success have resulted from an earlier unrelated action?
- Would a future capable agent likely discover this immediately anyway?
- Is the condition likely to recur?

If evidence is weak, do not save it.

# SPARSE SKILL LIBRARIES

Do not create weak skills merely because the library is empty or small.

A sparse library is acceptable.

When few skills exist, consider class-level reusable patterns broadly, but keep the same eligibility threshold.

Never seed the library with low-confidence knowledge just to make it less empty.

# WHERE TO SAVE QUALIFYING KNOWLEDGE

When a candidate passes the eligibility test, use this preference order.

## 1. Update a relevant skill already used or loaded

If a skill consulted during the trajectory governs the new learning, update that skill first.

If its existing instruction is wrong or superseded, replace the relevant rule rather than adding contradictory guidance.

## 2. Update an existing umbrella skill

Use an existing class-level skill when its scope naturally contains the learning.

Prefer the skill whose **decision scope** most closely matches the new rule, not merely the skill sharing the most keywords.

Example:

A lesson about mocking HTTP dependencies during integration tests belongs under the skill governing integration testing if that is where the relevant decisions are made, even if another general HTTP skill shares more vocabulary.

## 3. Add a support file under an existing umbrella

Use support files when the information is durable but too detailed, environment-specific, or reference-oriented for the main SKILL.md.

Use:

- `references/<topic>.md` for repository facts, environment procedures, provider quirks, condensed domain knowledge, reproduction recipes, protocol details, API behavior, or interpretation guidance;
- `templates/<name>.<ext>` for starter artifacts intended to be copied and modified;
- `scripts/<name>.<ext>` for deterministic probes, checks, fixture generation, verification, or statically reusable automation;
- `examples/<name>.<ext>` for examples intended to be consulted rather than copied directly.

Write support files with `skill_manage action=write_file`.

Add a short pointer from the governing SKILL.md so a future agent knows the support file exists and when to read it.

Do not dump session transcripts into support files.

## 4. Create a new class-level umbrella skill

Create a new skill only when:

- the learning clearly defines a reusable class of work;
- no existing skill reasonably governs it;
- the skill would be useful across multiple future tasks;
- the name remains meaningful outside the current session.

A new skill name MUST NOT be:

- a PR number;
- ticket or incident ID;
- exact error string;
- feature codename;
- one library's temporary issue;
- `fix-X`;
- `debug-Y`;
- `audit-Z`;
- any name that only makes sense for the current task.

Prefer durable names that describe the task class or decision domain.

# REQUIRED SKILL.md STRUCTURE

Every newly created SKILL.md and every substantially rewritten SKILL.md must follow the structure below.

Existing skills should be migrated toward this structure when they are already being modified for a qualifying learning. Do not rewrite an otherwise unrelated skill solely to normalize its formatting.

The canonical structure is:

```markdown
---
name: <skill-name>
description: Use when <clear proactive trigger describing when this skill should be invoked>
---

# <Skill Title>

## Inputs

<What information, artifacts, files, context, state, tools, or prerequisites are expected before executing this skill.>

## Workflow

1. <Step>
2. <Step>
3. <Step>
...
n. <Step>

## Outputs

<What the skill is expected to produce, change, validate, or report.>

## Constraints

<Optional. Durable constraints, invariants, safety boundaries, environment limitations, or conditions that affect execution.>

## Examples

<Optional. Small reusable examples that clarify non-obvious usage or decision points.>
"""

@dataclass(frozen=True, slots=True)
class DirectSkillGenerationConfig:
    """Settings loaded from ~/.msagent/prompts/generate-skill/config.yml."""

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
                skill_path = await self._write_skill(skill_md, cfg, thread_id)

            console.print_success(f"Skill draft saved to {skill_path}")
            console.print(f"[muted]Prompt source: {prompt_source}[/muted]")
            console.print("")
        except Exception as exc:
            console.print_error(f"Error generating skill: {exc}")
            console.print("")
            logger.exception("Skill generation failed")

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

    async def _load_prompt_template(
        self,
        root: Path,
        cfg: DirectSkillGenerationConfig,
    ) -> tuple[str, str]:
        """Resolve the prompt from the configured variant folder."""
        variant_dir = root / cfg.active
        if variant_dir.is_dir():
            if cfg.prompt_file:
                candidate = variant_dir / cfg.prompt_file
                files = [candidate] if candidate.is_file() else []
            else:
                files = sorted(variant_dir.glob("*.md"))
            if files:
                parts = [await asyncio.to_thread(p.read_text, encoding="utf-8") for p in files]
                source = str(files[0]) if len(files) == 1 else str(variant_dir)
                return "\n\n".join(parts), source

        console.print_warning(
            f"Prompt variant '{cfg.active}' not found under {root}; using built-in default"
        )
        return DEFAULT_GENERATE_PROMPT, "built-in default"

    async def _generate_skill_md(
        self,
        messages: list[AnyMessage],
        template: str,
        thread_id: str,
    ) -> str:
        """Call the session LLM directly, bypassing the graph and checkpointer."""
        ctx = self.session.context
        llm_config = await initializer.load_llm_config(ctx.model, ctx.working_dir)
        llm = initializer.llm_factory.create(llm_config)

        trimmed, omitted = trim_history(messages, llm, llm_config.context_window)
        history_text = get_buffer_string(trimmed)
        if omitted:
            history_text = (
                f"[Примечание: {omitted} более ранних сообщений опущено из-за лимита контекста.]\n\n"
                f"{history_text}"
            )

        # str.replace, not str.format: history and prompt may contain arbitrary braces.
        rendered = template
        for placeholder, value in (
            ("{history}", history_text),
            ("{agent}", ctx.agent),
            ("{thread_id}", thread_id),
            ("{working_dir}", str(ctx.working_dir)),
        ):
            rendered = rendered.replace(placeholder, value)
        if "{history}" not in template:
            rendered = f"{rendered}\n\nИстория сессии:\n{history_text}"

        response = await llm.ainvoke([HumanMessage(content=rendered)])
        text = getattr(response, "text", None)
        raw = text if isinstance(text, str) else str(response.content)
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