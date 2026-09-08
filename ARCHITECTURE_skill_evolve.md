# Skill Evolver — Architecture

Component: session-to-skill distillation (`/direct-skill-generation`)
Status: working draft; reflects branch `extract-session-history-generate-skill-md-and-other` as of 2026-09-08.

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
| `src/msagent/skill_evolver/features.py` | Code-only candidate extraction over recorded trajectories: `Episode`, six detectors, `extract_episodes`, `mine_cross_session`, `classify_approval`, `group_incidents`, `evidence_score`, `gate_decision`, `DEFAULT_MIN_EVIDENCE_SCORE`, `FEATURES_VERSION` (section 14) |
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
| `min_evidence_score` | `1.0` | Threshold of `features.gate_decision()`: the incident score a session must reach for the LLM stages to run at all (section 14). The strong user correction rule applies while the threshold is at most the default 1.0; non-numeric or negative values fall back to the default with a warning |

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

The stage prompts were rewritten **in place** (still `prompt_v1.md`) when the evidence bundle
switched from seq numbers to `[evN]` fragment ids and the candidate contract gained conditions
(section 15). Seeding is copy-if-missing (section 4), so a home that already holds
`~/.msagent/skill-evolver/prompts/{classify,render}/prompt_v1.md` keeps the old text and must be
refreshed by hand (delete the two files or copy the packaged ones over them). The symptom of a
stale copy is unmistakable: the model cites seq numbers, `evidence_refs` fails the `StrictStr`
schema, and after the corrective retry every candidate is rejected as `evidence not shown`.

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
        │                       + the mine_cross_session patterns it supports (§14);
        │                       returns the supporting trajectories the episodes cite
        ▼
 gate_decision() not passing → print_info with the reason, stop (no LLM call)
        │
        ▼
 build_evidence_bundle(episodes, [current, *supporting]) → EvidenceBundle (§15)
        │                       excluded episodes (insufficient context) → warning,
        │                       gate_decision(bundle.kept) again; failing → stop
        ▼
 classify(bundle.text, bundle.shown, llm, prompt)
        │                       one direct LLM call (+ one corrective retry inside
        │                       classify); rejected candidates → warning with the
        │                       reason; verdict "nothing" → print_info, stop
        ▼
 plan_render()                  reference candidates noted; update targets resolved
        │                       against the catalogue (unknown → dropped, warning);
        │                       one existing skill when every update points at it
        ▼
 render_skill_md(evidence=bundle.shown)
        │                       one LLM call → validate_skill_md(); errors → one
        │                       corrective call with the error list → validate again;
        │                       still invalid → print_error(list), nothing is written
        ▼
 write_proposal()               <root>/.proposals/<thread>/<name>/{provenance.json,
                                SKILL.md} (provenance v2, §16); success message +
                                activation hint
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
- **One fragment per event.** When two episodes cite the same event, the bundle shows it
  once, with the cut chosen by the heavier episode; the lighter episode's block repeats that
  line. Its own structured facts (the argument diff, the correction window) are still in its
  block, so nothing is lost, but a second cut of the same event is not shown.

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
`group_incidents()` joins the episodes that describe the same events, `evidence_score()` adds
the incidents up and `gate_decision()` says whether — and why — a thread reaches the LLM stage.
The module is stdlib only — importing it must not load langchain, which
`tests/ut/skill_evolver/test_features.py` enforces in a subprocess — so it runs in tests and CI
without an LLM, and every detection is reproducible. The rules below are `FEATURES_VERSION` 3.

```python
@dataclass(frozen=True, slots=True)
class EvidenceItem:
    ref: EvidenceRef          # file name + physical line (+ seq for display), see the recorder doc
    role: str                 # what the event is to the episode: error, fixed_call, correction, ...
    required: bool            # part of the minimum without which the episode cannot be shown
    snippet: str | None = None   # content-bearing cut chosen by the detector ("…"-marked);
                              # None = the bundle shows the head of the recorded event

@dataclass(frozen=True, slots=True)
class Episode:
    kind: Literal["error_recovery", "user_correction", "retry_loop",
                  "approval_denied", "repeated_procedure", "skill_gap"]
    thread_id: str
    source: str               # file name of the episode's own thread
    evidence: list[EvidenceItem]   # never empty, refs unique, >= 1 required, >= 1 of `source`
    tool_sequence: list[str]
    facts: dict[str, Any]     # JSON-safe, kind-specific details; text cut to 200 chars, cuts marked
    weight: float             # 0.0..1.0
    anchors: list[str] = field(default_factory=list)   # "<run_id>#<seq>" of the events the
                              # episode is about, never of context events; [] for skill_gap
    evidence_seq -> list[int] # property: sorted seqs of the own-source items (tables, exgraph)

@dataclass(frozen=True, slots=True)
class ApprovalVerdict:
    status: Literal["denied", "approved", "unknown"]
    denied: list[dict[str, Any]]   # rejected actions only: {"name": str | None, "args": clipped}
    reason: str                    # never empty: how the decision was read, or why it could not be

@dataclass(frozen=True, slots=True)
class GateDecision:
    incidents: list[list[Episode]]
    score: float
    passes: bool
    reason: str                    # GATE_NO_EPISODES | GATE_SCORE_REACHED |
                                   # GATE_STRONG_CORRECTION | GATE_SCORE_BELOW

FEATURES_VERSION = 3              # recorded in provenance.json; bumped with any rule or weight
DEFAULT_MIN_EVIDENCE_SCORE = 1.0  # gate threshold when the config sets none

extract_episodes(traj, *, skill_index=None) -> list[Episode]    # five per-trajectory detectors
mine_cross_session(trajs, *, min_support=2) -> list[Episode]   # repeated_procedure only
classify_approval(request, decision) -> ApprovalVerdict        # one approval, read by structure
group_incidents(episodes) -> list[list[Episode]]               # connected components over anchors
evidence_score(episodes) -> float                              # sum over incidents of max(weight)
gate_decision(episodes, *, min_score) -> GateDecision          # passes + reason for the LLM gate
```

| Kind | Weight | Rule (v3, one private `_detect_<kind>` each) | Facts | Anchors · Evidence (**required** / context) |
|---|---|---|---|---|
| `error_recovery` | 0.6 | inside one stream (turn group × subagent, see *Context streams*): a `status == "error"` call followed within `RECOVERY_WINDOW` (5) calls of the same stream by an `ok` call of the same tool; the first such `ok` call decides — a non-empty raw argument diff is the recovery, identical arguments (transient failure, or calls recorded without `tool.start`) are nothing; same-tool `error` / `orphan` calls in between are skipped. A following `dispatch` turn, another subagent and an orphan never recover; two failures sharing one recovery give two episodes (two diffs) | `tool`, `error_type`, `error` (`error` or, for a `tool.result` with `status=error`, its `output_text`; head 100 + tail 180, the cut marked), `args_diff` (`added` / `removed` clipped; `changed{old,new}` windowed on the change: common prefix and suffix dropped, 60 chars of context each side), `calls_between`, `subagent` | anchors: the failed call and the recovery · **`error`** (end of the failed call, snippet = the error text), **`fixed_call`** (start of the ok call), **`result`** (its end) / `failed_call` (start of the failed call) |
| `user_correction` | 0.9 strong / 0.5 weak | adjacent turn groups: the head of the later group has a `user_message` containing a `STRONG_CORRECTION_MARKERS` or `WEAK_CORRECTION_MARKERS` phrase (ru + en, case-insensitive, at a word start; the negations `нет,` / `no,` only when they open the message) **and** the group's actions differ from the previous group's — a tool added or removed (name sets over all calls of each group, any subagent) or a tool used in both groups whose last call before and first call after differ in normalized arguments. A marker without an observed change is nothing; a head without a user message (`resume`) cannot correct, and the prelude turn is never the corrected group. `strength` is `strong` when a strong marker is present, else `weak` with `WEAK_CORRECTION_WEIGHT` | `correction_text` (the window of 120 chars each side of the first marker in the whitespace-collapsed message, cuts marked — a long message keeps the phrase, not its head), `strength`, `markers`, `tools_before`, `tools_after`, `changes{tools_added, tools_removed, args_changed{tool: diff}}` (diffs windowed on the change), `run_id_before`, `run_id_after` | anchor: the correcting turn (`turn.start` of the head) · **`correction`** (that turn, snippet = the marker window) / `corrected_turn`, and per tool in `args_changed` a `before_call` and an `after_call` |
| `retry_loop` | 0.7 | inside one stream: calls chained by `(tool name, work object)` — a call recorded without arguments has no object and never chains; ≥ `RETRY_MIN_ATTEMPTS` (3) attempts (orphans count) with ≥ 2 distinct normalized argument sets **and** either a failed attempt (`status == "error"`) or variants differing in a `SEARCH_KEYS` key (`pattern` / `query` / `regex`). Reading three files is a fan-out and a parameter sweep that never failed is not a loop | `tool_name`, `work_object`, `attempts`, `reason` (`failed attempt` / `search key varies`), `args_variants` (normalized), `statuses`, `run_id` (turn of the first attempt) | anchors: every attempt · **`attempt`** first and last / `attempt` in between |
| `approval_denied` | 1.0 | every `Approval` of a turn is read by `classify_approval(request, decision)`: `denied` is an episode naming the rejected actions only, `approved` is skipped, `unknown` is logged at debug level with its reason and skipped. Context: the calls of the approval's turn with `seq_start > approval.seq` plus the calls of the following turns of the **same group** (the `resume` continuation of an interrupted turn, never the next `dispatch` turn), capped at `DENIAL_CONTEXT_CALLS` (3), any subagent — `Approval` has no subagent field | `interrupt_id`, `run_id`, `tools` (names of the rejected actions), `denied_actions` (`verdict.denied`), `request`, `decision`, `next_tools` | anchor: the approval · **`approval`** / `next_call` ×≤3 |
| `skill_gap` | 0.4 | unchanged in v2: domain tools (anything but `get_skill` / `fetch_skills` / `get_tool` / `fetch_tools` / `run_tool`) were used, `skills_consulted` is empty, and the BM25 top hit of *user messages + tool names* against the library scores ≥ `SKILL_GAP_MIN_SCORE` (1.0). A description-fix candidate, not a new skill. Needs a `BM25Index` from `retrieval.py` (stdlib BM25 over `name + description`; the caller builds `SkillDoc(skill.display_name, skill.description)` — `features.py` never imports `msagent.skills`) | `candidate_skill`, `score`, `matched_terms`, `domain_tools` | no anchors — a trajectory-level observation · **`first_call`**, **`user_message`** (the first) / `user_message` (the rest) |
| `repeated_procedure` | 1.0 | `mine_cross_session` only: steps are *segments* of one stream — catalog calls are dropped and a call with `status != "ok"` closes the segment, so a repeated failure is never a procedure; tool-name n-grams (n = 2..5) inside segments present in ≥ `min_support` **distinct** `thread_id`s (`min_support < 2` raises). Only closed patterns are reported — a sub-n-gram with the same support as a longer one is dropped, so a shared five-step procedure is one episode, not ten. The episode belongs to the first supporting trajectory in input order and cites the steps of that trajectory **and** of the lexicographically first other supporting thread — proof from two sessions — while `support` counts every supporting thread. It says that several sessions issued these calls in this order and each returned `ok`, never that the task succeeded | `ngram`, `support`, `thread_ids` | anchors: the own-thread steps · **`step`** of both threads (the bundle indexes the second trajectory too) |

Design points:

- **Evidence is real and addressable.** Every `EvidenceItem.ref` is the file name and physical
  line of one source event (`EvidenceRef`, built by `Trajectory.event_ref`; the recorder doc,
  section 9) and every anchor a `<run_id>#<seq>` of one; a property test over all fixtures
  re-reads each cited line and checks it holds that very `seq`. `seq` is display only: it
  restarts under a new `rec` after a process restart, which is why refs carry the line, anchors
  the `run_id`, and ordering is model order (turns in file order, spans in order), never
  trajectory-wide `seq` sorting. `Turn.approvals` are typed `Approval` records that keep their
  `seq` and `line` for this reason.
- **Required minimum and content-bearing cuts.** Each item has a role and a `required` flag: the
  required items are the minimum without which the episode is not worth showing (the table
  above); the evidence bundle trims context items under budget and excludes an episode whose
  required items do not fit (section 15). Text copied into facts or snippets is cut around what
  matters and every cut is marked with `…`: `_window` keeps `MARKER_CONTEXT` (120) chars around
  the correction marker, `_diff_window` drops the common prefix and suffix of a changed argument
  and keeps `DIFF_CONTEXT` (60) chars around the change, `_clip_edges` keeps the head (100) and
  tail (180) of an error, `_clip` cuts fact values at `VALUE_LIMIT` (200). Nothing absent is
  reconstructed: an empty result stays `(no output)` in the bundle, an orphan stays `orphan`.
- **Context streams.** Calls are compared inside one execution context: a *turn group* — a turn
  plus the adjacent turns with `source == "resume"` that continue it (the `/threads` path: no
  user message, a fresh `run_id`, no link field; adjacency plus `source` is the only continuation
  the recorder expresses) — split by `call.subagent` (`None` = root). `error_recovery`,
  `retry_loop` and the procedure segments never cross a following `dispatch` turn or a subagent
  boundary; `user_correction` compares whole groups (any subagent); the denial context runs into
  the `resume` turn of the same group only.
- **Normalized arguments and the work object.** `_normalize_args` drops `VOLATILE_KEYS`
  (`offset`, `limit`, `timeout`, `timeout_ms`), collapses whitespace and runs `PATH_KEYS` values
  through `posixpath.normpath` (`./cfg/dev.yml` == `cfg/dev.yml`); non-string values pass through.
  The *work object* of a call is the program plus the first non-option token of the first
  `COMMAND_KEYS` string (`msprof --collect train.py` → `msprof train.py`; `python a.py` ≠
  `python b.py`; a command wins over the `cwd` it runs in, which is context, not the object),
  else the first `PATH_KEYS` string, else `""` — the tool itself; a call recorded without
  arguments has none. `user_correction` and `retry_loop` compare normalized forms;
  `error_recovery` keeps the raw diff, because the diff is the knowledge.
- **Structured approval verdicts; unknown is unknown.** `classify_approval` matches
  `{"decisions": [...]}` by index against `request["action_requests"]` (a length mismatch is
  `unknown`; one decision without such a list applies to the flat request), a flat `{"action" |
  "type": ...}` dict against the request, and a bare legacy string only when it equals one of
  `LEGACY_DENIAL_ANSWERS` / `LEGACY_APPROVAL_ANSWERS`. `reject` denies; `approve` / `edit` /
  `respond` do not; any other type, free text (`No, don't`, `Cancel`), `None`, numbers and lists
  are `unknown` with a non-empty `reason` — never an assumed denial, so `approve` with the
  comment `no issues` is no longer a denial and a `no` in free text counts for nothing.
- **Incidents.** `anchors` are the keys of the events an episode is *about* (its calls, its
  correcting turn, its approval — never context events). `group_incidents` joins episodes that
  share an anchor (union-find over the list; an anchorless `skill_gap` is keyed on
  `kind@thread:evidence_seq`, so only an exact duplicate joins it), ordered by first episode.
  `evidence_score` is the sum over incidents of the heaviest episode in each: the two
  `error_recovery` and the `retry_loop` of one msprof chain count 0.7 once, independent incidents
  add up, and a duplicate episode changes neither the score nor the gate. The signals fixture
  yields 6 episodes, 4 incidents and a score of 3.0 (v1 summed the weights).
- **Gate.** `gate_decision(episodes, *, min_score)` returns one of four reasons, checked in this
  order: `no episodes` (first, so an empty thread never reaches `build_evidence_bundle`, even
  with `min_evidence_score: 0`); `score >= min_evidence_score`; `strong user correction` — a
  `user_correction` with `strength == "strong"` passes while `min_score <=
  DEFAULT_MIN_EVIDENCE_SCORE` (1.0), because one explicit correction with an observed change of
  action (weight 0.9) is worth the analysis at the default settings and a stricter threshold opts
  out of the rule; otherwise `score < min_evidence_score`. A weak correction (0.5) never admits
  a thread on its own. Passing the gate admits the thread to the LLM stage; whether a rule is
  worth keeping is decided there.
- **Known imprecisions (v2)**, documented rather than fixed:
  - markers are literal: `Нет, спасибо, дальше сам` opens with a negation and, when the agent
    then does nothing, is a strong correction (the previous tools were "removed").
  - `error_recovery` links by tool name and context, not by work object: `bash pytest` error →
    `bash ls` ok is a recovery.
  - the work object of a command is the program plus its first positional token: `pip install X`
    and `pip install Y` are one object, `msprof --output ./prof train.py` → `msprof ./prof`.
  - `grep` without a `path` has the object `""`: three unrelated searches in one group are a
    `retry_loop`.
  - the denial context is not filtered by subagent (`Approval` has no such field).
  - the marker window is cut around the *first* marker in table order (strong first), not the
    earliest in the message; a message with several corrections shows one of them in full.
  - `skill_gap` fires on a single distinctive shared term in libraries of four or more skills
    (unchanged from v1).
  - the user's copy of `prompt_v1.md` under `~/.msagent` is not updated automatically
    (copy-if-missing seeding, section 4; the stage prompts changed in place, section 6).

Wiring (`handle()`, section 7): the thread's JSONL is located via
`export.resolve_trajectories_dir(state_dir=initializer.get_project_paths(ctx.working_dir).root)`
and `export.find_trajectory_file(dir, thread_id)`; **no file → `print_error` and return without
calling the LLM** (a thread without a recorded trajectory is refused, not passed through).
`_collect_episodes(current, others, skill_index=...)` runs `extract_episodes` on the current
trajectory and `mine_cross_session([current, *others])` over the agent's newest
`CROSS_SESSION_LIMIT` trajectories; with the current trajectory first in that list, every shared
pattern it supports belongs to the current thread, so the kept episodes are exactly those with
`thread_id == current.thread_id`. Each of them also cites one supporting session's steps;
`_supporting(others, episodes)` picks the trajectories those refs point at and `_gather_evidence`
returns them, so the bundle indexes `[current, *supporting]` (refs are unique per file and line,
so several trajectories in one bundle are unambiguous). `gate_decision(episodes,
min_score=cfg.min_evidence_score)` decides: a decision that does not pass ends the command with an
info message naming the reason — `no episodes detected in thread <id>`, or `evidence score X <
min_evidence_score Y (N episodes, K incidents)` — and no LLM call; otherwise the episodes go to
the bundle → classify stage (section 15) and the candidates to the render stage (section 16).

Known gaps: files recorded before the `ignore_agent` fix have no `tool.*` events, so the
detectors see empty `tool_calls` there (no fallback to `AiMessage.tool_call_names` by design);
`record_approval` has no call site yet, so `approval_denied` fires only on fixtures until it is
wired.

Verification: `pytest tests/ut/skill_evolver -q` — detector positives and the mandatory negatives
("спасибо" is not a correction and a weak marker never makes a strong one; a fan-out over
different files, a parameter sweep that never failed and paging with a changing `offset` are not
retry loops; an `ok` in another subagent or in a following `dispatch` turn is not a recovery;
`approve` with the comment `no issues` is not a denial and an unreadable decision yields no
episode; catalog calls and failed calls are not procedure steps; an n-gram inside one trajectory
is not a procedure), `classify_approval` over every recorded shape, the incidents-and-gate section
(one chain seen by several detectors counts once, a shared turn does not merge incidents, a
duplicate episode does not change the gate, the four reasons, the strong correction rule), the
`skill_evolver_signals.jsonl` fixture end to end, per-fixture kind counts, the evidence property
test (every ref re-reads to a line holding its `seq`, every anchor resolves to a source event),
the roles and required items of every detector, the windowed cuts, the no-langchain/
no-network subprocess probe. The 99% line coverage of `features.py` (`uv run --with pytest-cov
pytest tests/ut/skill_evolver --cov=msagent.skill_evolver.features`) was measured on v1.
## 15. Evidence bundle and JSON classification (LLM stage, library only)

The replay of the whole session (section 8) has been replaced by two stages that give the model
only evidence and get a structured answer back; both are wired into `handle()` (section 7).

**`bundle.py`** — `build_evidence_bundle(episodes, trajectories, *, max_chars=30000) ->
EvidenceBundle(text, shown, episodes)`. One markdown block per episode, heaviest first (stable
for equal weights):

```
### Episode E1 — error_recovery (weight 0.60, thread thread-s)
Tools: bash, bash, bash
Facts:
- error: "msprof: unknown option --collect"
- args_diff: {"changed": {"cmd": {"new": "msprof --application train.py --output ./prof", "old": "msprof --collect train.py"}}}
Excerpts:
- [ev10] tool.error bash (error): msprof: unknown option --collect
- [ev11] tool.result bash (ok): profiling done
- [ev6] tool.start bash: {"cmd": "msprof --application train.py --output ./prof"}
- [ev8] tool.start bash: {"cmd": "msprof --collect train.py"}
```

No transcript and no chronology: facts come from the episode, excerpts are whitespace-collapsed
cuts (300 chars) of the cited events — the detector's snippet when it chose one (the marker
window of a correction, the head and tail of an error), else the head of the recorded event —
resolved through an index keyed by `EvidenceRef` built from the typed model of every trajectory
passed in (turn starts, tool starts and results, AI messages, approvals). A `tool.result` with
`status=error` renders its `output_text` as the error. Facts are re-clipped to 800 chars because
`approval_denied.decision` and list-valued facts are unbounded. A `repeated_procedure` block adds
`Support: N threads (counted by code); excerpts from 2 of them`, and excerpts of another
trajectory carry `(thread <id>)`.

Every excerpt line carries a bundle-local id `[evN]`, assigned in order of first appearance when
its block is committed; one event has one id and one text across blocks (a later block citing an
event already shown repeats the line). `shown` is the registry of exactly those fragments — id,
`EvidenceRef`, role, `required`, text — and `episodes` the outcome of every input episode:

- `shown` — the whole block fitted;
- `trimmed` — only the required excerpts fitted; the context ones are replaced by
  `- … N more events not shown`, which is not citable;
- `excluded` — even the required excerpts did not fit (insufficient context): the block is
  absent, the episode's weight must not carry the thread to the LLM, and scanning continues with
  lighter episodes. `EvidenceBundle.kept` lists the shown and trimmed episodes; both handlers
  run `gate_decision(bundle.kept)` again when anything was excluded and stop before creating an
  LLM if it fails. A bare event number is never citable, the `Evidence: seq` line is gone, and
  nothing raises for size — an empty bundle is a legitimate outcome.

Loud failures: an episode whose source is not among the trajectories or whose ref does not
resolve (episodes and trajectories must be the same data), a non-positive budget. Data conditions
are tolerated and documented: a source seen twice keeps its first copy (as `mine_cross_session`
does), the reader's synthetic `unknown` and prelude turns never shadow the real record at the
same line, and a recorder restart that repeats a `seq` yields two refs and two fragments. The
stored experience graph appendix (`exgraph_context.attach_stored_graph`) is appended to
`bundle.text` after budgeting and carries no ids: context, never citable evidence, and not
recorded in provenance.

**`classify.py`** — `classify(bundle_text, valid_refs, llm, template) -> Classification`, with
pydantic reply models `ClassifyResult(verdict: "save" | "nothing", candidates)` and
`Candidate(title, rule, evidence_refs: list[StrictStr], applies_when: str | None, constraints:
list[str], expected_outcome: str | None, future_applicability, target{action: create | update |
reference, existing_skill}, candidate_id)`, and the frozen `Classification(verdict, candidates,
rejected: list[tuple[Candidate, str]])`. The prompt is `skill-evolver/prompts/classify/prompt_v1.md`:
input `{evidence_bundle}` (filled by `classify`) plus `{skill_library}` (filled by the caller),
strict JSON output, the five-condition eligibility test of the generator prompt without any
SKILL.md structure or storage rules; it tells the model that only `[evN]` lines are citable and
that the conditions of a rule (`applies_when`, `constraints`, `expected_outcome`) are stated only
when the evidence shows them — `null` / `[]` otherwise, never invented. The module is stdlib +
pydantic: the LLM is duck-typed (`ainvoke` over `(role, text)` pairs, the reply read through
`.text` / `.content`), so the import-isolation probe covers it. Post-processing is code, not
prompt:

- the reply is stripped of `<think>` blocks and of a whole-reply fence, parsed with `json.loads`
  and validated; on failure one corrective retry replays the bad reply as an assistant turn with
  the error text, and a second failure raises `ValueError`;
- `StrictStr` refs, so a seq number, a boolean or a float never passes as a citation;
- a candidate with empty `evidence_refs` is rejected with `empty evidence_refs`; one citing any id
  outside `valid_refs` (an excluded event, a seq, an invention) with `evidence not shown in the
  bundle: [...]` — rejected candidates are returned in `Classification.rejected`, printed by the
  handlers as `Rejected '<title>': <reason>` and recorded in provenance; never repaired;
- kept candidates get `candidate_id` `c1`, `c2`, … in reply order (the join key of render and
  provenance; titles can collide), their refs deduplicated in the model's order;
- no candidates left → verdict `nothing`; a model verdict of `nothing` with grounded candidates
  passes through unchanged (downstream keys on `verdict`).

Guards raise before any LLM call: blank bundle, empty `valid_refs`, template without the
placeholder. Refs are unique per file and line, so a bundle may mix trajectories (a shared
procedure's second session); the classify prompt is resolved by the stage-aware loader
(`prompts/classify/<prompt_file>`, section 6).

Verification: `tests/ut/skill_evolver/test_bundle.py` (ordering, trim-then-exclude budget,
scanning past an excluded episode, one id per event, excerpt resolution and clipping, the
correction window and the argument diff reaching the text, an error in `tool.result.output_text`,
distinct refs after a recorder restart, every shown fragment re-read from its physical line across
all fixtures including `malformed_lines.jsonl`, cross-session blocks) and `test_classify.py`
(scripted fake LLM: fences, retry, schema violations, rejection reasons, candidate ids, optional
conditions, guards, prompt contract, isolation).

## 16. Render, validation and proposals

The classify verdict is turned into a file by three small modules, all stdlib + pydantic (the
import-isolation probe in `test_validator.py` covers them):

**`render.py`** — `plan_render(candidates, skills) -> RenderPlan` decides what one render call
gets: `reference` candidates are only reported (the library already holds the rule); `update`
candidates must name a catalogue skill by display name or by a bare name that is unique across
categories, otherwise they are dropped with a WARNING (never silently turned into `create`); the
existing skill is passed to the model only when every kept update points at the same skill, several
targets produce a console note and a new skill. `render_skill_md(candidates, *, llm, template,
existing_skill, expected_name, taken_names, evidence) -> RenderResult(content, validation, calls)`
fills `{candidates}` and `{existing_skill}` (formatted text or "None. Create a new skill.") in one
regex pass, calls the duck-typed LLM,
strips `<think>` blocks and a whole-reply fence, normalises line endings and validates. On errors
the model gets exactly one corrective turn (`("ai", bad reply)` + the error list); the result of
the second attempt is returned as is — the handler prints the errors and writes nothing.

`format_candidates(candidates, evidence)` renders one numbered block per candidate: the title and
applicability, `Rule`, then — only when the classifier filled them — `When` (`applies_when`),
`Constraints` (bullets) and `Expected outcome`, the `Target`, and `Evidence:` bullets with the
**text** of the fragments `select_render_evidence` picks for it (its cited fragments, required ones
first, at most `RENDER_EVIDENCE_LIMIT` = 3). Fragment ids, seqs and thread ids never enter the
payload — they belong in provenance, and the reply is the user-facing SKILL.md. A candidate
without conditions is rendered without them: nothing is invented on the way to the file.

**Prompt** `prompts/render/prompt_v1.md`: role and task (revise the existing skill and keep its
name, or create a new one with a durable kebab-case name; every rule lands in a Workflow step, a
Constraint or Inputs/Outputs; `When` is the trigger, `Constraints` the limits, `Expected outcome`
the completion criterion; the `Evidence` lines make the rule precise but no one-time value from
them is copied; no session narrative, seq numbers, evidence ids or other one-time details), the
two placeholders, the
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
`REQUIRED_PROVENANCE_KEYS` with non-empty `thread_ids` and `candidates`.
`build_provenance(*, thread_ids, bundle, classification, rendered, sources, model,
prompt_variants, category, target)` produces the **provenance v2** contract
(`PROVENANCE_VERSION = 2`):

```json
{"provenance_version": 2,
 "thread_ids": ["<analysed thread>", "<threads a shared procedure relies on>"],
 "sources": {"<file name>": "<path of the trajectory>"},
 "episodes": [{"kind": "...", "weight": 0.6, "thread_id": "...", "source": "<file name>",
               "bundle_status": "shown | trimmed | excluded",
               "evidence": [{"id": "ev1" | null, "source": "<file name>", "line": 4, "seq": 4,
                             "role": "error", "required": true}]}],
 "evidence_shown": {"ev1": {"source": "<file name>", "line": 4, "seq": 4, "role": "error",
                            "required": true, "text": "<exactly the excerpt line the model saw>"}},
 "candidates": [{"candidate_id": "c1", "title": "...", "rule": "...", "evidence_refs": ["ev1", "ev3"],
                 "applies_when": "..." | null, "constraints": [], "expected_outcome": "..." | null,
                 "future_applicability": "high",
                 "target": {"action": "create", "existing_skill": null}}],
 "candidates_rejected": [{"title": "...", "reason": "evidence not shown in the bundle: ['ev9']",
                          "evidence_refs": ["ev9"]}],
 "render_evidence": {"c1": ["ev1", "ev3"]},
 "model": "<llm_config.model>",
 "prompt_variants": {"classify": "<resolved path>", "render": "<resolved path>"},
 "features_version": 3,
 "generated_at": "<ISO 8601, UTC>",
 "category": "<cfg.category>",
 "target": {"action": "create | update", "existing_skill": "...", "existing_path": "..."}}
```

Three things are told apart: what the detectors **extracted** (`episodes`, every input episode
with its bundle outcome and every cited event — `id: null` marks an event the model never saw),
what the classify model was **shown** (`evidence_shown`: id → file, physical line, seq, role and
the exact text), and what reached the **render** stage (`candidates` are the kept ones,
`render_evidence` the fragment ids quoted to the renderer per candidate, derived by the same
`select_render_evidence` call the renderer uses). For every written candidate,
`evidence_refs → evidence_shown[id] → (source, line)` names the events and the fragments the
model saw; `sources` resolves the file. Rejected candidates are listed with their reason.

Compatibility: proposals written before this contract carry no `provenance_version` (**v1**):
their `candidates[].evidence_refs` are seq numbers, their `episodes[]` rows have `evidence_seq`
and there is no registry. `/skill-review` reads only `category`, `thread_ids`, `generated_at` and
`target`, which both versions share, so v1 proposals still list, accept and reject. `features_version`
3 marks the evidence-item contract of section 14 (2: incidents gate; 1: summed weights). Property
tests: every `evidence_shown` entry of a written `provenance.json` re-reads to the physical line
holding that `seq`, including across corrupted lines, and every candidate's refs are a subset of
the registry (`test_writer.py::test_provenance_shown_fragments_resolve_to_source_lines`,
`test_direct_skill_generation.py::test_handle_writes_proposal_not_library`); the correcting
phrase of a long user message is in the registry text and in both LLM payloads
(`test_handle_long_correction_phrase_is_in_provenance`); a budget too small for any episode's
required evidence creates no LLM (`test_handle_bundle_exclusion_creates_no_llm`,
`test_skill_mining.py::test_real_run_bundle_exclusion_creates_no_llm`).

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
`tests/ut/cli/handlers/test_skill_mining.py` (55 tests, `/skill-mine` and `/skill-review`) and
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

`/skill-mine` loops over threads: each thread whose `gate_decision()` passes (section 14) gets
its own bundle and classify + render pair, so a run costs at most `2 x threads` LLM calls and
writes at most one proposal per thread. The pool is the agent's newest `CROSS_SESSION_LIMIT` (20)
trajectories and each target goes **first** into `_collect_episodes`, so the kept
`repeated_procedure` episodes belong to that thread; `ThreadStats.supporting` holds the pool
trajectories their evidence cites (the second session of each shared procedure) and the bundle
indexes `[target, *supporting]`. `_generate` prints the excluded episodes and the rejected
candidates, re-gates on `bundle.kept` before `LazyLlm.get()`, quotes `bundle.shown` to the
renderer and writes provenance v2 (section 16).

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

- **Threads** (both modes): thread, turns, tools, ai, episodes, incidents, score, gate, reason;
  the threshold is in the title. `incidents`, `score`, `gate` (`pass` / `skip`) and `reason` come
  from one `gate_decision()` per thread, so a thread admitted by the strong correction rule at
  score 0.90 reads `pass` with `strong user correction`, and a skipped one says whether it had
  `no episodes` or a `score < min_evidence_score`. A zero `tools` count is styled `warning` — it
  is the most diagnostic number in the table, because every detector that needs tool calls is
  then dead.
- **Episodes** (dry run only): thread, kind, incident, weight, tool sequence, evidence seq (the
  own-thread seqs via `Episode.evidence_seq`, for display — refs identify events by file and
  line), grouped per thread with a `subtotal` row (empty `incident` cell). `incident` labels the
  episodes of one thread that describe the same events — `I1`, `I2`, … in `group_incidents`
  order — so the rows sharing a label are the ones the score counts once. Rows keep **detector
  order**, not weight order: the bundle sorts by weight for the model, while a human wants to
  know which detector fired. Both list columns carry the true element count in brackets before
  any clipping (5 tool names, 8 seqs, then `… +k`).

The dry run ends on one line: `Dry run: N threads, E episodes, K incidents, total evidence score
S; P threads would reach the LLM (up to 2P LLM calls). Nothing was written and no LLM was
created.`

Colour comes from column-level styles, never inline markup, and every data-derived cell is passed
through `rich.markup.escape` or wrapped in `rich.text.Text`, so a tool named `[bold]` renders
literally (`test_episodes_table_shows_markup_literally`).

Files recorded before the recorder's `ignore_agent` fix carry no `tool.*` events at all
(`ARCHITECTURE_trajectory_recorder.md` section 11), so **every** detector yields nothing on them —
`user_correction` included, because it needs an observed change of the agent's actions between turn groups. That
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

Every run ends on one fixed-shape line: threads mined, proposals, skipped by the gate, nothing to save,
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
