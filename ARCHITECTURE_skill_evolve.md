# Skill Evolver — Architecture

Component: session-to-skill distillation (`/direct-skill-generation`)
Status: working draft; reflects branch `extract-session-history-generate-skill-md-and-other` as of 2026-09-04.

## 1. Purpose

Skill Evolver turns a completed interactive msAgent session into a draft of a reusable
skill (`SKILL.md`). It is invoked on demand by the user, analyzes the full session —
user messages, model answers and reasoning, tool calls, tool results and errors — and
writes the distilled knowledge into the standard skill library layout, **without
mutating the analyzed session** in any way.

The component follows the msAgent philosophy that domain expertise lives in prompts
and skills, not in code: the review methodology is a user-editable prompt, the
executable part is a thin, generic pipeline.

## 2. User-facing behavior

| Invocation | Effect |
|---|---|
| `/direct-skill-generation` | Analyze the **current** thread (deprecated; see section 17) |
| `/direct-skill-generation last` | Analyze the most recent **previous** thread (e.g. after a CLI restart) |
| `/direct-skill-generation <thread-id>` | Analyze an explicit thread |
| `/skill-mine [--threads N] [--since 7d] [--dry-run] [--thread <id>]` | Mine several recorded threads, one proposal per thread (section 17) |
| `/trajectories [list \| show <thread-id>]` | Browse the recorded trajectories (section 17) |
| `/skill-review [list \| accept <name> \| reject <name>]` | Review, promote or delete a proposal (section 17) |

Output: `<root>/.proposals/<thread-id>/<skill-name>/SKILL.md` plus `provenance.json`,
where `<root>` is `<working_dir>/skills` (or `output_dir` from the component config).
The skill name is the validated frontmatter `name`; collisions inside the thread folder
get `-2`, `-3`, … suffixes. **Nothing is written into a library category.** A proposal
becomes active only when a human reviews it and moves the folder to
`<root>/<category>/<skill-name>/` (a `create` proposal) or replaces the existing
`SKILL.md` with it (an `update` proposal). Until then it is invisible to `/skills`, to
the `get_skill` tool and to the agent's system prompt (section 9). A thread without a
recorded trajectory, a session below the evidence threshold, a `nothing` verdict and a
`SKILL.md` that fails validation twice all end without writing anything.

## 3. Component inventory

New files:

| File | Role |
|---|---|
| `src/msagent/skill_evolver/direct_skill_generation.py` | `DirectSkillGenerationHandler`: config/prompt resolution and the orchestration of the evidence pipeline (trajectory → episodes → gate → bundle → classify → render → validate → proposal); the legacy session-replay helpers are kept but unused |
| `src/msagent/cli/handlers/session_history.py` | Shared **read-only** access to persisted thread history: `load_history` (thread resolution, incl. `last`), `latest_other_thread`, `trim_history` |
| `src/msagent/skill_evolver/features.py` | Code-only candidate extraction over recorded trajectories: `Episode`, six detectors, `extract_episodes`, `mine_cross_session`, `evidence_score`, `FEATURES_VERSION` (section 14) |
| `src/msagent/skill_evolver/retrieval.py` | Stdlib BM25 over skill descriptions (`SkillDoc`, `BM25Index`) used by the `skill_gap` detector |
| `src/msagent/skill_evolver/bundle.py`, `classify.py` | Evidence bundle and JSON classification (section 15) |
| `src/msagent/skill_evolver/render.py` | Render stage: `plan_render`, `render_skill_md` with one corrective LLM call (section 16) |
| `src/msagent/skill_evolver/validator.py` | Code validation of a rendered `SKILL.md`: `validate_skill_md`, `ValidationResult`, `skill_name` (section 16) |
| `src/msagent/skill_evolver/writer.py` | Proposal writer: `build_provenance`, `write_proposal` into `.proposals/` (section 16) |
| `tests/fixtures/trajectories/skill_evolver_signals.jsonl` | Hand-written trajectory exercising every per-trajectory detector |
| `tests/ut/skill_evolver/test_*.py` | Detector, retrieval, bundle, classify, render, validator, writer and handler tests on scripted fake LLMs; no-langchain/no-network probes |
| `resources/configs/default/config.skill.evolver.yml` | Packaged default component config |
| `src/msagent/skill_evolver/mining.py` | `SkillMiningHandler`: the multi-thread `/skill-mine` command, its argument parser, thread selection and the dry-run tables (section 17) |
| `src/msagent/cli/handlers/trajectories.py` | `TrajectoriesHandler`: `/trajectories list` and `show` over `trajectory_recorder.export` (section 17) |
| `src/msagent/cli/handlers/skill_review.py` | `SkillReviewHandler`: `/skill-review list`, `accept` and `reject` over `.proposals/` (section 17) |
| `tests/ut/cli/handlers/test_skill_mining.py`, `test_trajectories_handler.py` | Command tests: parser, tables, the no-LLM dry run, the per-thread loop on a scripted LLM, proposal accept/reject |
| `resources/configs/default/skill-evolver/prompts/<stage>/prompt_v1.md` | Packaged stage prompts `classify/` and `render/`; `default/` is the legacy replay prompt |

Modified files (integration points):

| File | Change |
|---|---|
| `src/msagent/cli/dispatchers/commands.py` | Handler instantiation, `"/direct-skill-generation"` entry in `_register_commands()`, `cmd_direct_skill_generation` delegate; the three commands of section 17 and the `[deprecated]` marker on the generator's help text |
| `src/msagent/cli/handlers/__init__.py` | Export of `DirectSkillGenerationHandler`, `SkillMiningHandler`, `TrajectoriesHandler`, `SkillReviewHandler` |
| `src/msagent/core/constants.py` | `CONFIG_SKILL_EVOLVER_FILE_NAME`, `SKILL_EVOLVER_CONFIG_FOLDER_NAME` |
| `src/msagent/core/storage_layout.py` | `_seed_skill_evolver_defaults()` called from `validate_and_initialize_storage_layout()`; `"skill-evolver"` added to `_MANAGED_DIRECTORIES` |
| `src/msagent/skills/factory.py` | `SkillFactory.load_skills` skips dot-directories below a scanned root (`_in_hidden_dir`), so `.proposals/` never enters the catalogue |

## 4. Runtime layout and startup seeding

```text
~/.msagent/                              (MSAGENT_HOME)
├── config/
│   └── config.skill.evolver.yml         # component configuration
└── skill-evolver/                       # isolated component folder
    └── prompts/
        ├── classify/
        │   └── prompt_v1.md            # evidence → JSON candidates (section 15)
        ├── render/
        │   └── prompt_v1.md            # candidates → SKILL.md (section 16)
        └── default/
            └── prompt_v1.md            # legacy replay prompt (unused by handle())
```

Seeding is performed once per process start by `_seed_skill_evolver_defaults()`
(`core/storage_layout.py`), invoked from `validate_and_initialize_storage_layout()`
right after the managed directories are ensured and **before** the early-return
branches, so upgrades of an existing home also receive new files. Mapping:

- `resources/configs/default/config.skill.evolver.yml` → `~/.msagent/config/config.skill.evolver.yml`
- `resources/configs/default/skill-evolver/**` → `~/.msagent/skill-evolver/**` (whole component tree)

Semantics: **copy-if-missing** — user edits are never overwritten; new files added in
later builds do arrive, changed defaults for already-materialized files do not.
Failures are logged as warnings and never abort startup. The `skill-evolver` home
folder participates in the standard layout validation (regular directory, no symlink)
via `_MANAGED_DIRECTORIES`.

Design note: `~/.msagent/skill-evolver/` is deliberately **outside** every stock
loading mechanism (`ConfigRegistry` does not scan it). It is owned exclusively by this
component, which keeps it isolated from the agent/LLM/MCP configuration lifecycle.

## 5. Configuration

`config.skill.evolver.yml` (resolution chain: user file in `~/.msagent/config/` →
packaged default in the wheel → dataclass defaults):

| Field | Default | Meaning |
|---|---|---|
| `active` | `default` | Variant folder of the **legacy replay prompt** (`prompts/<active>/`); the evidence pipeline does not use it. Validated against `[A-Za-z0-9._-]+` |
| `prompt_file` | `prompt_v1.md` (packaged) | File name looked up inside each stage folder (`prompts/classify/`, `prompts/render/`) and inside the legacy variant; when unset, **all** `*.md` files of the folder are concatenated in alphabetical order. Validated against `[A-Za-z0-9._-]+`; an unsafe value is ignored with a warning |
| `category` | `default` | Library category a proposal is meant for; recorded in `provenance.json` and shown in the activation hint, never written to automatically |
| `output_dir` | unset | Root that receives `.proposals/`; default is `<working_dir>/skills` |
| `min_evidence_score` | `1.0` | Minimal `features.evidence_score()` of a session for the LLM stages to run at all (section 14); non-numeric or negative values fall back to the default with a warning |

The config is intentionally **not** part of the `VersionedConfig`/`ConfigRegistry`
framework: it is component-local, has no cross-references to resolve, and carries no
migration burden yet. The parser is defensive — any unreadable/invalid file degrades
to defaults with a logged warning.

## 6. Prompt resolution

`_load_prompt_from(root, cfg, folder)` searches two roots in order — the user root
`~/.msagent/skill-evolver/prompts/` and the packaged root inside the wheel
(`resources/configs/default/skill-evolver/prompts/`) — taking the first `<folder>` that
yields content. `_load_stage_prompt(root, cfg, stage)` calls it with a stage name
(`STAGES = ("classify", "render")`; anything else raises), the legacy
`_load_prompt_template(root, cfg)` with `cfg.active`. A configured `prompt_file` that
does not exist produces an explicit warning and falls back to the folder's `*.md` glob
(a typo must not silently switch the methodology). If neither root yields a prompt, the
command fails with a descriptive error naming the folder; there is **no prompt text
embedded in Python** — the packaged templates are the single source of truth. The
resolved source path of each stage prompt is recorded in `provenance.json`.

Placeholders are substituted by code, never with `str.format`, so braces inside the
data are inert: the classify prompt gets `{skill_library}` (programmatic inventory of
the loaded skills, filled by the handler) and `{evidence_bundle}` (filled by
`classify()`); the render prompt gets `{candidates}` and `{existing_skill}` (filled by
`render_skill_md()` in one regex pass). The legacy replay prompt keeps `{agent}`,
`{thread_id}`, `{working_dir}` and `{history}`.

## 7. Execution pipeline

```text
/direct-skill-generation [last|<id>]
        │
        ▼
 load_history()                 read-only thread resolution: graph.aget_state
        │                       (current) or checkpointer.aget_tuple ("last" via
        │                       the same SQL as /threads, or an explicit id);
        │                       no messages → warning, stop
        ▼
 _load_config() ── _load_stage_prompt("classify"), _load_stage_prompt("render")
        │
        ▼
 find_trajectory_file()         <state_dir>/trajectories/<agent>_<thread>.jsonl;
        │                       no file → print_error, the LLM is never called
        ▼
 _gather_evidence()             load_trajectory(current) + load_trajectories(agent,
        │                       newest CROSS_SESSION_LIMIT=20); extract_episodes over
        │                       the current trajectory (BM25 index of the catalogue)
        │                       + the mine_cross_session patterns it supports (§14)
        ▼
 evidence_score() < min_evidence_score → print_info, stop (no LLM call)
        │
        ▼
 build_evidence_bundle(episodes, [current]) → classify(bundle, valid_seq, llm, prompt)
        │                       one direct LLM call (+ one corrective retry inside
        │                       classify); verdict "nothing" → print_info, stop
        ▼
 plan_render()                  reference candidates noted; update targets resolved
        │                       against the catalogue (unknown → dropped, warning);
        │                       one existing skill when every update points at it
        ▼
 render_skill_md()              one LLM call → validate_skill_md(); errors → one
        │                       corrective call with the error list → validate again;
        │                       still invalid → print_error(list), nothing is written
        ▼
 write_proposal()               <root>/.proposals/<thread>/<name>/{provenance.json,
                                SKILL.md}; success message + activation hint
```

The `llm` is `LLMFactory.create(load_llm_config(ctx.model))`, one client for both
stages; every stage runs outside the graph (no tools, no checkpointer, no middleware).

## 8. Session replay design (legacy, unused by `handle()`)

`_generate_skill_md` and its helpers remain in the module but are no longer called
(user decision, 2026-09-04: keep until the evidence pipeline has proven itself, then
delete). The design is recorded here for that decision.

Instead of serializing history into prompt text, the component **replays the genuine
message sequence** to the analysis model, preceded by a guard `SystemMessage`
(`_REPLAY_SYSTEM_PROMPT`) stating that the session is completed, must not be continued,
and only the final instruction message is to be followed.

Normalization (`_prepare_replay_messages` and helpers):

- **Reasoning folding** — providers reject a `reasoning_content` field on inbound
  messages, so past private reasoning is moved into the visible text of each
  `AIMessage` as a `<past_reasoning>…</past_reasoning>` block; the raw field is
  stripped.
- **Orphan tool calls** — a trailing `AIMessage` with `tool_calls` that never received
  its `ToolMessage` (interrupted session) is dropped to satisfy the strict
  assistant/tool pairing rules of OpenAI-compatible APIs.
- **Budget trimming** — `trim_history()` (`cli/handlers/session_history.py`) trims the
  replay to `_HISTORY_BUDGET_RATIO` (0.6) of the model's `context_window` (token
  counting via `utils.compression.calculate_message_tokens`). The default `head_tail`
  strategy keeps the first 6 messages (the task statement — the most valuable part for
  distillation) and cuts from the **middle**, keeping the newest tail; the tail is
  re-aligned to a `HumanMessage` boundary, and the head is shortened to end on a
  complete tool exchange so no `tool_call` is left without its `ToolMessage`. Only if
  the head alone exceeds the budget does the function fall back to the `tail` strategy
  on the full history (oldest dropped, newest kept; `tail` is also selectable
  explicitly). The omission count is reported to the model inside the instruction
  (`[Note: N messages from the middle of the session were omitted due to context
  limits.]`; in the degenerate fallback case the omitted messages are in fact the
  oldest ones).

What the analyst model sees natively: user turns, assistant turns with structured
`tool_calls` (name + arguments), tool results including error payloads, folded past
reasoning. What it cannot see: the session's system prompt (not stored in `messages`
— it is injected per-call by middleware in normal operation) and reasoning the
provider never returned. Multimodal blocks (images) pass through unchanged and require
a vision-capable analysis model.

## 9. Non-mutation guarantees

The analyzed thread is untouched by construction, on three pillars:

1. history is obtained only through read-only APIs (`aget_state` / `aget_tuple`);
2. the LLM is called directly through `LLMFactory`, bypassing the graph, the
   checkpointer and the middleware stack — nothing is appended to any thread;
3. files are written by the handler with plain file I/O — no agent tools run, so no
   HITL interrupts can fire;
4. the output is a **proposal** outside every skill scanner: `SkillFactory.load_skills`
   skips dot-directories below a scanned root, and
   `<root>/.proposals/<thread>/<name>/SKILL.md` lies one level below the depth at which
   `AgentFactory._resolve_existing_paths` and the deepagents `SkillsMiddleware` look
   for skills (`<child>/<sub>/SKILL.md`). The flat layout `.proposals/<name>/` would
   not do: `_resolve_existing_paths` would list `.proposals` as a deepagents source,
   and an `update` proposal (same name as a library skill, so it passes the name
   allow-list) would win the last-source-wins merge whenever the real skill is flat
   under `<working_dir>/skills/<name>/` or `output_dir` points at a later root such as
   `~/.msagent/skills`. Guarded by `test_writer.py::test_proposals_not_scanned_by_skill_factory`,
   `test_proposals_invisible_to_agent_factory_sources` and
   `test_direct_skill_generation.py::test_proposal_invisible_to_skill_scanners`.

The in-repo precedent for this pattern is `CompressionHandler`
(`aget_state` + direct LLM call).

## 10. Relationship to the main codebase

Reused core services:

| Service | Usage |
|---|---|
| `initializer` (bootstrap singleton) | `app_paths` (home/config layout), `load_llm_config`, `llm_factory`, `get_checkpointer`, cached skill catalog |
| `ConfigRegistry` (indirect) | Model alias resolution for the session's LLM |
| `LLMFactory` | Analysis model = the session's configured model |
| `SkillFactory.parse_frontmatter` | Frontmatter parsing inside the validator, so "valid" means "the skill loader reads the same data" |
| `trajectory_recorder.export` / `reader` | `resolve_trajectories_dir`, `find_trajectory_file`, `load_trajectory`, `load_trajectories` — read-only access to the recorded JSONL |
| `utils.compression.calculate_message_tokens` | Token budgeting for the (legacy) replay |
| `/threads` thread-listing SQL | Reused verbatim in `latest_other_thread` |

Deliberately untouched subsystems: graph assembly (`AgentFactory`), middleware stack,
approval/HITL, MCP, checkpointer write paths. The command is a pure CLI-layer feature;
removing its registration and the seeding call detaches it completely.

Feedback loop: proposals do **not** re-enter the platform on their own. A human promotes
a proposal by moving it into `<root>/<category>/` (or by replacing the library file it
revises); from then on the standard discovery path applies (`SkillFactory` scan →
`_FilteredSkillsMiddleware` per-turn refresh → `/skills` and slash shortcuts).

## 11. Key design decisions

- **Direct LLM calls, no tools.** Two calls per run (classify, render), each with at
  most one corrective retry: deterministic cost and latency, no headless-interrupt
  handling; the library inventory is injected as a programmatic `{skill_library}`
  snapshot instead of a tool call.
- **Evidence, not transcript.** The model sees code-extracted episodes (section 15),
  never the session; every candidate must cite seqs the bundle contained.
- **Code decides what is a skill.** `validate_skill_md` rejects what the render prompt
  forbids; the model gets exactly one corrective turn, and a second failure ends the
  command with the error list instead of a repaired file.
- **Proposals, not library writes.** The user promotes a proposal by hand; the
  `provenance.json` next to it makes a bad draft debuggable and a quality metric
  possible.
- **Prompt-as-data.** The review methodology lives in versioned, user-overridable
  Markdown; Python holds only the pipeline.
- **Component isolation.** Own home folder, own config file, own seeding — zero
  coupling to the config-migration framework at this stage.
- **Copy-if-missing seeding.** First launch of a new build materializes everything;
  user customizations survive upgrades.

## 12. Limitations and future work

- **Updates are applied by hand.** An `update` proposal is the full revised text of the
  existing skill (its `name` is enforced by the validator); the user replaces the
  library file after review. The model cannot inspect or modify the library: no skill
  tools are exposed, the programmatic `{skill_library}` snapshot is its only view. The
  candidate mechanism for an agentic evolver (a **thread fork** via
  `graph.aupdate_state` plus filesystem tools) is unchanged and still not implemented.
- **Legacy replay path.** `_generate_skill_md` and its helpers, the `Nothing to save.`
  sentinel, `prompts/default/prompt_v1.md` and the `active` field are kept in the
  module but `handle()` no longer calls them (user decision, 2026-09-04: remove once
  the evidence pipeline has proven itself).
- **One render for all candidates.** Every accepted candidate goes into one `SKILL.md`;
  the existing skill text is passed only when all `update` candidates name the same
  skill, otherwise a new skill is rendered with a console note. One proposal per run.
- **Validator gaps (deliberate, spec-literal):** deepagents' extra name rules (no
  trailing `-`, no `--`), non-empty `## Inputs` / `## Outputs`, a mandatory H1 title
  and the phrase "doesn't work" are not checked. The `^\d+$` task-id rule is
  unreachable behind the `^[a-z]…` name pattern and is kept as documentation.
  "Never use `--force`" inside a constraint is rejected as folklore — that is the rule
  as written.
- `mine_cross_session` loads the newest `CROSS_SESSION_LIMIT` (20) trajectories of the
  agent on every run; a corrupt neighbouring file (`TrajectoryReadError`) aborts the
  command loudly instead of being skipped.
- `.proposals/` under the repository's own `skills/` (when the CLI runs with the repo
  as working dir) is not git-ignored; adding it to `.gitignore` is the user's call.
- The component config is outside `VersionedConfig`; schema changes will need ad-hoc
  handling until it is migrated into the framework.

## 13. Operational notes

- Development runs use the editable install (`uv pip install -e .`); wheel rebuilds
  require `pip install --force-reinstall --no-deps dist/<wheel>` because the version
  number does not change between local builds.
- After hand-merging changes, run `ruff check --select F821,F401 src/` (or
  `pre-commit run`) — undefined-name regressions in this component have historically
  been the dominant failure mode.
- Seeding can be exercised against a clean home with
  `MSAGENT_HOME=$(mktemp -d) msagent config --show`.

## 14. Deterministic feature extraction (no LLM)

Candidate discovery is code, not prompt. `src/msagent/skill_evolver/features.py` turns one
`Trajectory` (the typed reader model of `msagent.trajectory_recorder`) into `Episode` records,
and `evidence_score()` sums their weights. The module is stdlib only — importing it must not
load langchain, which `tests/ut/skill_evolver/test_features.py` enforces in a subprocess — so
it runs in tests and CI without an LLM, and every detection is reproducible.

```python
@dataclass(frozen=True, slots=True)
class Episode:
    kind: Literal["error_recovery", "user_correction", "retry_loop",
                  "approval_denied", "repeated_procedure", "skill_gap"]
    thread_id: str
    evidence_seq: list[int]   # seq of the source events; never empty, strictly increasing
    tool_sequence: list[str]
    facts: dict[str, Any]     # JSON-safe, kind-specific details; values clipped to 200 chars
    weight: float             # 0.0..1.0

extract_episodes(traj, *, skill_index=None) -> list[Episode]    # five per-trajectory detectors
mine_cross_session(trajs, *, min_support=2) -> list[Episode]   # repeated_procedure only
evidence_score(episodes) -> float                              # sum of weights
```

| Kind | Weight | Rule (implemented literally, one private `_detect_<kind>` each) | Facts |
|---|---|---|---|
| `error_recovery` | 0.6 | a `status == "error"` call followed within 5 tool calls (model order, across turns) by an `ok` call of the same tool. The knowledge is the argument diff, so a recovery with identical arguments (transient failure, or calls recorded without `tool.start`) is not an episode; two failures sharing one recovery give two episodes | `tool`, `error_type`, `error`, `args_diff` (`added` / `removed` / `changed{old,new}`), `calls_between`, `subagent` |
| `user_correction` | 0.9 | the next turn's `user_message` contains a `CORRECTION_MARKERS` phrase (ru + en, case-insensitive substring) **and** its tool-name sequence differs from the previous turn's; turns without a user message (`resume`) and the prelude turn are skipped | `correction_text` (≤ 500 chars), `tools_before`, `tools_after`, `run_id_before`, `run_id_after` |
| `retry_loop` | 0.7 | ≥ 3 calls of one tool in one turn, chained while consecutive key sets have Jaccard > 0.7, with ≥ 2 distinct argument sets; other tools in between and the outcome of each attempt do not matter | `tool_name`, `attempts`, `args_variants`, `statuses`, `run_id` |
| `approval_denied` | 1.0 | an `approval.decision` whose serialized decision contains a whole-word `reject*` / `deny` / `denied` / `no` (`_is_denial`, covering deepagents `{"decisions": [...]}`, flat `{"action": ...}` and plain strings) plus the next 3 tool calls as context — crossing into the `resume` turn, because the decision is recorded after the turn ended | `interrupt_id`, `run_id`, `tools` (`action_requests[*].name` or `request.tool`), `request`, `decision`, `next_tools` |
| `skill_gap` | 0.4 | domain tools (anything but `get_skill` / `fetch_skills` / `get_tool` / `fetch_tools` / `run_tool`) were used, `skills_consulted` is empty, and the BM25 top hit of *user messages + tool names* against the library scores ≥ `SKILL_GAP_MIN_SCORE` (1.0). A description-fix candidate, not a new skill. Needs a `BM25Index` from `retrieval.py` (stdlib BM25 over `name + description`; the caller builds `SkillDoc(skill.display_name, skill.description)` — `features.py` never imports `msagent.skills`) | `candidate_skill`, `score`, `matched_terms`, `domain_tools` |
| `repeated_procedure` | 1.0 | `mine_cross_session` only: tool-name n-grams (n = 2..5) present in ≥ `min_support` **distinct** `thread_id`s (`min_support < 2` raises). Only closed patterns are reported — a sub-n-gram with the same support as a longer one is dropped, so a shared five-step procedure is one episode, not ten. The episode cites the first supporting trajectory | `ngram`, `support`, `thread_ids` |

Design points:

- **Evidence is real.** Every `evidence_seq` entry is a `seq` of the source JSONL; a property test
  over all fixtures checks it. Ordering is model order (turns in file order, spans in order),
  never trajectory-wide `seq` sorting, because `seq` restarts under a new `rec` after a process
  restart. `Turn.approvals` are typed `Approval` records that keep their `seq` for this reason.
- **Literal rules, documented false positives** (user decision, 2026-09-04): an all-`ok` fan-out
  such as reading three files in one turn is a `retry_loop`; a marker such as "actually"
  anywhere in a message counts; an approval whose free text contains the word "no" counts as a
  denial; `skill_gap` fires on a single distinctive shared term in libraries of four or more
  skills. These are left to the score threshold and to the LLM stage.
- **Threshold.** `min_evidence_score` (config, default 1.0) is the score below which the LLM
  must not be called at all; it cuts routine sessions before any budget is spent.

Wiring (`handle()`, section 7): the thread's JSONL is located via
`export.resolve_trajectories_dir(state_dir=initializer.get_project_paths(ctx.working_dir).root)`
and `export.find_trajectory_file(dir, thread_id)`; **no file → `print_error` and return without
calling the LLM** (a thread without a recorded trajectory is refused, not passed through).
`_collect_episodes(current, others, skill_index=...)` runs `extract_episodes` on the current
trajectory and `mine_cross_session([current, *others])` over the agent's newest
`CROSS_SESSION_LIMIT` trajectories; with the current trajectory first in that list, every shared
pattern it supports cites the current thread's own events, so the kept episodes are exactly those
with `thread_id == current.thread_id` and the evidence bundle needs only the current trajectory
(single-thread `valid_seq`, no cross-thread seq ambiguity). `evidence_score(...) <
cfg.min_evidence_score` → info message and return; otherwise the episodes go to the bundle →
classify stage (section 15) and the candidates to the render stage (section 16).

Known gaps: files recorded before the `ignore_agent` fix have no `tool.*` events, so the
detectors see empty `tool_calls` there (no fallback to `AiMessage.tool_call_names` by design);
`record_approval` has no call site yet, so `approval_denied` fires only on fixtures until it is
wired.

Verification: `pytest tests/ut/skill_evolver -q` — detector positives and the mandatory negatives
("спасибо" is not a correction, two calls are not a retry loop, an n-gram inside one trajectory
is not a procedure), the `skill_evolver_signals.jsonl` fixture end to end, per-fixture kind
counts, the evidence property test, the no-langchain/no-network subprocess probe;
`features.py` line coverage 99% (`uv run --with pytest-cov pytest tests/ut/skill_evolver
--cov=msagent.skill_evolver.features`).
## 15. Evidence bundle and JSON classification (LLM stage, library only)

The replay of the whole session (section 8) has been replaced by two stages that give the model
only evidence and get a structured answer back; both are wired into `handle()` (section 7).

**`bundle.py`** — `build_evidence_bundle(episodes, trajectories, *, max_chars=30000) -> (text,
seqs)`. One markdown block per episode, heaviest first (stable for equal weights):

```
### Episode E1 — approval_denied (weight 1.00, thread thread-si)
Evidence: seq 12, 14, 16
Tools: bash, read_file
Facts:
- decision: {"decisions": [{"type": "reject"}]}
Excerpts:
- seq 12 approval.decision: request={...} decision={...}
- seq 14 tool.start bash: {"cmd": "..."}
```

No transcript and no chronology: facts come from the episode, excerpts are whitespace-collapsed
cuts (300 chars) of the cited events, resolved through a per-thread `seq` index built from the
typed model (turn starts, tool starts and results, AI messages, approvals). Facts are re-clipped to
800 chars because `approval_denied.decision` and list-valued facts are unbounded; an episode citing
many events shows the first six and the last two excerpts, while the `Evidence:` line always lists
every seq. Over budget, the lightest episodes are dropped whole. The returned set holds every seq
of a kept block. Loud failures: an episode whose thread is not among the trajectories or whose seq
does not resolve (episodes and trajectories must be the same data), a non-positive budget, a
heaviest block that does not fit. Data conditions are tolerated and documented: a thread recorded
twice keeps its first copy (as `mine_cross_session` does), the reader's synthetic `unknown` and
prelude turns never shadow the real record at the same seq, and a seq shared by several records
(recorder restart) renders as `(ambiguous: N records share this seq)` with a warning.

**`classify.py`** — `classify(bundle, valid_seq, llm, template) -> ClassifyResult`, with pydantic
models `ClassifyResult(verdict: "save" | "nothing", candidates)` and `Candidate(title, rule,
evidence_refs: list[StrictInt], future_applicability, target{action: create | update | reference,
existing_skill})`. The prompt is `skill-evolver/prompts/classify/prompt_v1.md`: input
`{evidence_bundle}` (filled by `classify`) plus `{skill_library}` (filled by the caller), strict
JSON output, the five-condition eligibility test of the generator prompt without any SKILL.md
structure or storage rules. The module is stdlib + pydantic: the LLM is duck-typed (`ainvoke`
over `(role, text)` pairs, the reply read through `.text` / `.content`), so the import-isolation
probe covers it. Post-processing is code, not prompt:

- the reply is stripped of `<think>` blocks and of a whole-reply fence, parsed with `json.loads`
  and validated; on failure one corrective retry replays the bad reply as an assistant turn with
  the error text, and a second failure raises `ValueError`;
- `StrictInt` refs, because lax validation turns `true` / `"7"` / `4.0` into ints that could pass;
- a candidate with empty `evidence_refs`, or citing a seq outside `valid_seq`, is dropped with a
  WARNING naming the title and the offending seqs — the model cannot reference what it has not
  seen, and this is the only working defence against invented evidence;
- no candidates left → verdict `nothing`; a model verdict of `nothing` with grounded candidates
  passes through unchanged (downstream keys on `verdict`).

Guards raise before any LLM call: blank bundle, empty `valid_seq`, template without the
placeholder. Limitations: refs are flat ints, unique per thread only, so a multi-thread bundle
cannot tell the same number apart across threads (`(thread, seq)` refs are a follow-up; the
handler avoids the problem by bundling the current trajectory only, section 14); the classify
prompt is resolved by the stage-aware loader (`prompts/classify/<prompt_file>`, section 6).

Verification: `tests/ut/skill_evolver/test_bundle.py` (ordering, whole-block budget, excerpt
resolution and clipping, collisions, fixtures end to end) and `test_classify.py` (scripted fake
LLM: fences, retry, schema violations, dropped candidates, guards, prompt contract, isolation).

## 16. Render, validation and proposals

The classify verdict is turned into a file by three small modules, all stdlib + pydantic (the
import-isolation probe in `test_validator.py` covers them):

**`render.py`** — `plan_render(candidates, skills) -> RenderPlan` decides what one render call
gets: `reference` candidates are only reported (the library already holds the rule); `update`
candidates must name a catalogue skill by display name or by a bare name that is unique across
categories, otherwise they are dropped with a WARNING (never silently turned into `create`); the
existing skill is passed to the model only when every kept update points at the same skill, several
targets produce a console note and a new skill. `render_skill_md(candidates, *, llm, template,
existing_skill, expected_name, taken_names) -> RenderResult(content, validation, calls)` fills
`{candidates}` (title, rule, applicability, target — never evidence seqs) and `{existing_skill}`
(formatted text or "None. Create a new skill.") in one regex pass, calls the duck-typed LLM,
strips `<think>` blocks and a whole-reply fence, normalises line endings and validates. On errors
the model gets exactly one corrective turn (`("ai", bad reply)` + the error list); the result of
the second attempt is returned as is — the handler prints the errors and writes nothing.

**Prompt** `prompts/render/prompt_v1.md`: role and task (revise the existing skill and keep its
name, or create a new one with a durable kebab-case name; every rule lands in a Workflow step, a
Constraint or Inputs/Outputs; no session narrative or one-time details), the two placeholders, the
`# REQUIRED SKILL.md STRUCTURE` section carried over **verbatim** from the original generator prompt
(commit `b6938fe`, lines 369-661: canonical structure, proactive description, mandatory
Inputs/Workflow/Outputs, optional Constraints/Examples), and the output contract (the bare
`SKILL.md`, no fences, nothing around it).

**`validator.py`** — `validate_skill_md(content, *, expected_name=None, taken_names=()) ->
ValidationResult(ok, errors)` collects every violation (the corrective call needs the whole list)
and repairs nothing:

| Rule | Check |
|---|---|
| frontmatter | parsed with `SkillFactory.parse_frontmatter` (so "valid" means "the loader reads the same data"); the text must start with `---`, the delimiters must be alone on their lines, the body must start on a new line; a reply still wrapped in a code fence is an error |
| `name` | a non-empty string (YAML ints/bools are rejected, never coerced) matching `^[a-z][a-z0-9-]{2,48}$`; not a task identifier (`^\d+$`, `^(pr\|issue\|bug\|ticket)-\d+`, `^(fix\|debug\|audit)-.*$`); not in `taken_names` (a `create` must not shadow a library skill). With `expected_name` (an update) the name must equal it and the pattern rules are skipped — the model did not choose that name |
| `description` | a non-empty string starting with `Use when ` (so `Instructions for debugging` is rejected) |
| sections | `## Inputs`, `## Workflow`, `## Outputs` present exactly once (H1/H2 delimit sections, H3+ stays inside, headings and steps inside code fences are ignored); `## Constraints` / `## Examples` non-empty when present |
| workflow | at least two top-level numbered items (`1.` or `1)`) outside fences |
| folklore | `is broken`, `does not work`, `never use `, `не работает`, `сломан` anywhere in the text (frontmatter included), case-insensitive, reported with the line number |

Additions beyond the task's rule list, each one line to remove: `expected_name`, `taken_names`,
the duplicate-heading error, the delimiter-line strictness and the fence-wrapped-reply error.
Deliberately not added: see section 12.

**`writer.py`** — `write_proposal(content, *, root, name, provenance, thread_id) -> Path` writes
`<root>/.proposals/<thread>/<name>/provenance.json` first and `SKILL.md` second (a skill never
exists without its provenance), refuses unsafe names and thread ids (it never writes outside
`.proposals/`), decides collisions by directory existence with an atomic `mkdir()` (`-2`, `-3`, …;
a half-written folder from a crash is skipped, not overwritten), and requires every key of
`REQUIRED_PROVENANCE_KEYS` with non-empty `thread_ids` and `candidates`. `build_provenance(...)`
produces:

```json
{"thread_ids": ["<analysed thread>", "<threads a shared procedure relies on>"],
 "episodes": [{"kind": "...", "weight": 0.6, "evidence_seq": [4, 5], "thread_id": "..."}],
 "candidates": [{"title": "...", "rule": "...", "evidence_refs": [4, 5],
                 "future_applicability": "high",
                 "target": {"action": "create", "existing_skill": null}}],
 "model": "<llm_config.model>",
 "prompt_variants": {"classify": "<resolved path>", "render": "<resolved path>"},
 "features_version": 1,
 "generated_at": "<ISO 8601, UTC>",
 "category": "<cfg.category>",
 "target": {"action": "create | update", "existing_skill": "...", "existing_path": "..."}}
```

`category`, `target` and the per-episode `thread_id` go beyond the task's schema: they tell a
reviewer where the proposal is meant to go and keep the evidence check meaningful for
cross-session episodes. Property test: every `evidence_seq` in every written `provenance.json` is
a subset of the `seq` values of the source JSONL
(`test_writer.py::test_provenance_evidence_seq_subset_of_source`,
`test_direct_skill_generation.py::test_handle_writes_proposal_not_library`).

Verification: `pytest tests/ut/skill_evolver -q` — validator positives and one negative per rule
(incl. `description: Instructions for debugging`), all-errors collection, writer
layout/collisions/rejections, the scanner guarantees, render happy path / one correction / double
failure / update name enforcement / guards, and the handler end to end on the
`skill_evolver_signals.jsonl` fixture with a scripted LLM (proposal written, library untouched,
refusals without an LLM call, threshold, `nothing` verdict, fabricated refs, double validation
failure, reference-only, unknown update target, update with existing text).

## 17. CLI surface: `/trajectories`, `/skill-mine`, `/skill-review`

The pipeline of sections 14-16 was reachable only through `/direct-skill-generation`, which
analyses one thread and always ends in LLM calls. Three commands open it up; the generator stays
registered and working, marked `[deprecated]` in `/help` and printing
`Use /skill-mine for trajectory-based generation.` at the start of every run.

| Invocation | Effect |
|---|---|
| `/trajectories [list]` | Table of the project's recorded threads: thread, agent, turns, events, size, mtime, first user message |
| `/trajectories show <thread-id>` | One thread as markdown (`export.render_markdown`); the id may be a unique prefix |
| `/skill-mine [--threads N] [--since 7d] [--dry-run] [--thread <id>]` | Mine several threads, one proposal per thread |
| `/skill-review [list]` | Table of the proposals on disk: name, category, action, threads, age, description |
| `/skill-review accept <name>` | Re-validate and move the folder into `<root>/<category>/<name>/` |
| `/skill-review reject <name>` | Delete the folder after an explicit confirmation |

New files: `src/msagent/cli/handlers/trajectories.py` (`TrajectoriesHandler`),
`src/msagent/skill_evolver/mining.py` (`SkillMiningHandler`),
`src/msagent/cli/handlers/skill_review.py` (`SkillReviewHandler`),
`tests/ut/cli/handlers/test_skill_mining.py` (53 tests, `/skill-mine` and `/skill-review`) and
`tests/ut/cli/handlers/test_trajectories_handler.py`. Registration follows the existing pattern
exactly: export from `cli/handlers/__init__.py`, instantiate in `CommandDispatcher.__init__`, one
dict entry in `_register_commands()` and one `cmd_*` delegate whose **docstring is the help text**.
Slash completion needs no change — `Session` derives it from `dispatcher.commands.keys()`, and
`completers/reference.py` is the `@`-file-path completer, not a command table. There is no
subcommand or flag completion for any command in this CLI.

### 17.1 `/skill-mine`: one proposal per thread

`handle()` parses, then `_run()` loads the config, resolves the trajectories directory, extracts
evidence for every selected thread in one `asyncio.to_thread` call, prints the **Threads** table
and either stops (dry run) or enters the per-thread loop.

Evidence refs are flat ints, unique per thread only (section 15), so a multi-thread bundle cannot
tell the same seq apart across threads. `/skill-mine` therefore keeps the single-trajectory bundle
and loops: each thread whose `evidence_score()` reaches `min_evidence_score` gets its own
classify + render pair, so a run costs at most `2 x threads` LLM calls and writes at most one
proposal per thread. Cross-session `repeated_procedure` support is unchanged — the pool is still
the agent's newest `CROSS_SESSION_LIMIT` (20) trajectories, and each target goes **first** into
`_collect_episodes`, so the kept episodes cite that thread's own events.

Selection (`select_trajectories`): one `load_trajectories(dir, agent=..., limit=max(N, 20))` pass
feeds both the pool (first 20) and the targets, so no file is parsed twice. `--since` compares
**file mtime**, not `Trajectory.started_at`: mtime always exists and means last activity, while
`started_at` is the first event's `ts` and can be empty, which would force a keep-or-drop fallback
on unparseable data. Since the listing is already mtime-descending, the window is a contiguous
prefix and `--threads N` means "the newest N inside the window". Mtime does not survive copying
files between machines — the documented cost of that choice. `--thread` resolves through
`find_trajectory_file` (unique prefixes included) and reuses the pool object when it is there, so
a thread older than the newest 20, or belonging to another agent, still works. Empty selections
get three distinct warnings: nothing recorded, nothing for this agent, nothing inside the window.

Argument parsing is hand-rolled, not argparse: argparse reports errors with `sys.exit`, and
`SystemExit` is a `BaseException` that neither `CommandDispatcher.dispatch` nor `Session._main_loop`
catches, so a mistyped flag would end the user's session. `--since` accepts `<count>` plus `h`,
`d` or `w`; `m` is **rejected** as ambiguous between minutes and months. `--thread` combined with
`--threads` or `--since` is an error, not a precedence rule, and a repeated flag is an error —
silently ignoring a flag the user typed is the same masking pattern the project rules forbid.
`--threads` defaults to 5.

### 17.2 The dry run: the detector debugger

`--dry-run` stops after feature extraction and never constructs an LLM. The single construction
site is `LazyLlm.get()`, instantiated per invocation inside the loop and reached only after a
thread passes the gate, so a real run in which every thread fails the gate also creates nothing.
`test_dry_run_never_creates_an_llm` pins this by making `initializer.llm_factory.create` and
`load_llm_config` raise.

Two tables, because one cannot carry both "why nothing fired" and "what fired":

- **Threads** (both modes): thread, turns, tools, ai, episodes, score, gate; the threshold is in
  the title. A zero `tools` count is styled `warning` — it is the most diagnostic number in the
  table, because every detector that needs tool calls is then dead.
- **Episodes** (dry run only): thread, kind, weight, tool sequence, evidence seq, grouped per
  thread with a `subtotal` row. Rows keep **detector order**, not weight order: the bundle sorts by
  weight for the model, while a human wants to know which detector fired. Both list columns carry
  the true element count in brackets before any clipping (5 tool names, 8 seqs, then `… +k`).

Colour comes from column-level styles, never inline markup, and every data-derived cell is passed
through `rich.markup.escape` or wrapped in `rich.text.Text`, so a tool named `[bold]` renders
literally (`test_episodes_table_shows_markup_literally`).

Files recorded before the recorder's `ignore_agent` fix carry no `tool.*` events at all
(`ARCHITECTURE_trajectory_recorder.md` section 11), so **every** detector yields nothing on them —
`user_correction` included, because it needs the tool-name sequence to differ between turns. That
is the realistic first run on existing data, so a note under the Threads table names the cause once
(not per row) whenever any selected thread shows zero tool calls.

### 17.3 Failure policy

Two tiers, which is how "the analyzer must fail loudly" and a usable multi-thread loop coexist:

- Parsing, selection, loading and detection have **no `try` at all**. They reach the single
  top-level guard in `handle()`, which prints and `logger.exception`s, and the run stops. One
  corrupt neighbouring file raises `TrajectoryReadError` naming itself.
- Per-thread generation is guarded: the message is printed with the thread id, the traceback is
  logged, the id is repeated in the summary and the summary switches to `print_error`. The loop
  continues, because aborting would discard the remaining threads after earlier ones already wrote
  files. `KeyboardInterrupt` is caught by neither tier.

Every run ends on one fixed-shape line: threads mined, proposals, below threshold, nothing to save,
failed.

### 17.4 `/skill-review`

Proposals are read from `<root>/.proposals/<thread>/<name>/` — `SkillFactory.load_skills` skips
dot-directories, so `/skills` never shows them and the review command must scan the tree itself.
The directory name is not always the skill name (collisions get `-2`), so `accept` takes the
destination name from the frontmatter via `skill_name()`, which is what the loader reads. A
proposal is addressed by bare name when that name exists in exactly one batch, and by
`<thread>/<name>` otherwise; an ambiguous bare name lists the qualified candidates instead of
guessing. A proposal whose `provenance.json` is missing or unparseable is **listed with its error**
rather than skipped — a half-written folder must stay visible to the person who has to decide.

`accept` re-runs `validate_skill_md` because the file may have been edited by hand, refuses when
`<root>/<category>/<name>/` already exists (the only guard against shadowing a library skill),
then moves the whole folder with `shutil.move` so `provenance.json` travels with it, and prunes the
emptied batch directory. `reject` deletes after an explicit confirmation, implemented as
`SkillReviewHandler._confirm` (a `PromptSession` with a yes/no completer, shaped like
`InterruptHandler._prompt_choice`) — there is no y/N helper anywhere else in the CLI, and being a
method is what makes it patchable in tests. Anything but an explicit yes, and `Ctrl+C`, mean no.

**Update proposals are refused, not moved** (an addition beyond the task, which describes only the
create case): an `update` carries the name of a library skill, so moving it into
`<root>/<category>/<name>/` would create a second skill with that name in another category. The
command prints `provenance.target.existing_path` and says the file must be replaced by hand, which
matches the activation hint the generator already prints and section 12.

Note that `skill_review.py` imports the generator handler **inside** `_root()`: `handlers/__init__`
loads this module before the generator, which re-enters the package for `session_history`. A
module-level import would widen the pre-existing cycle documented in
`test_direct_skill_generation.py`.

Verification: `pytest tests/ut/cli tests/ut/skill_evolver -q`. The mining tests cover the parser
(every error message, `30m` and `0d` rejected, `7D` accepted, the `--thread` conflicts, duplicate
flags, defaults), the formatters (the `[n]` prefix always equals the true length), the tables
(markup shown literally, subtotals), the dry run (no LLM, the zero-tool note, the three empty
selections, `--thread`, `--threads`), the real run on the `skill_evolver_signals.jsonl` fixture
with a scripted LLM (one proposal written with its provenance, `nothing` verdict, a failing thread
reported while the loop continues) and `/skill-review` (list, accept, category, hand-broken
SKILL.md, occupied destination, update refusal, ambiguity, qualified names, reject with and
without confirmation).
