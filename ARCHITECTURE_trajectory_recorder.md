# Trajectory Recorder — Architecture

Status: implemented (`src/msagent/trajectory_recorder/`), schema version 1. Typed reader (`model.py`,
`reader.py`) and the `ignore_agent` fix landed on 2026-09-04.

## 1. Purpose

msAgent needed durable, machine-readable access to **agent trajectories** — the complete record of how the
agent worked in each user session: user turns, every assistant message, every tool call with inputs, outputs
and timings, subagent delegations *including their internal steps*, human approval decisions, retries,
errors and context compressions.

The primary consumers are downstream processing pipelines, not humans:

- behavioral analytics (timings, token usage, error/retry/approval patterns per step);
- mining successful task-solving recipes into `SKILL.md` files (turn → steps → outcome sequences);
- building Knowledge Graphs (typed events with stable identities and explicit parent/child links).

### Why the existing persistence layers are not sufficient

| Layer | Location | Limitation for processing |
|---|---|---|
| LangGraph checkpointer | `<state>/checkpoints.sqlite` | msgpack blobs (fragile across langchain upgrades, not directly queryable); no per-message timestamps; history is *rewritten* by compression; **subagent internals are never stored** (the deepagents `task` tool returns only the final text to the parent) |
| Audit log | `<state>/audit_log/*.jsonl` | Only `user.turn`, `user.response`, `subagent.delegation` (input/output of a delegation, not its steps); disabled for most agents |
| `--trace-jsonl` (`CliRunRecorder`) | user-supplied path | Opt-in flag; file overwritten on every start; content truncated at 4000 chars |
| Conversation offload | `<state>/conversation_history/*.md` | Flat text via `get_buffer_string` — structure (tool calls, ids, usage) is lost; written only when compression fires |

The decisive constraint: subagents run via `await subagent.ainvoke(...)` inside the `task` tool with **no
checkpointer**, so their trajectories exist only in process memory at runtime. Full-fidelity capture must
therefore happen **at runtime**, as an event-sourced, append-only log. The trajectory recorder is that log
("flight recorder"): once an event is written it is never rewritten, so later compression/summarization
cannot lose data.

## 2. Design goals

1. **Isolation.** All functionality lives in a dedicated package, `src/msagent/trajectory_recorder/`. The
   rest of the codebase interacts with it through exactly one module (`msagent.trajectory_recorder.hooks`);
   integration is a handful of one-call insertions.
2. **Fail-safety.** Recording must never break or slow down the agent perceptibly. Every public hook and
   every callback method is exception-safe (log-and-swallow); a failed recorder degrades to a no-op.
3. **Config-driven.** Behavior is controlled by `config.trajectory.recorder.yml`
   (shipped default in `resources/configs/default/`), with per-user override and env kill switch.
4. **Full fidelity by default.** No truncation unless configured; `ensure_ascii=False`; messages serialized
   with `langchain_core.messages.message_to_dict` (complete and reversible via `messages_from_dict`).
5. **Stable identities.** Every event line is self-contained and addressable — required for KG construction.
6. **No new dependencies.** Uses `pyyaml`, `pydantic`, `langchain_core`, `langgraph` — all already required.
7. **Checkpointer-independent.** Trajectories are recorded even for agents configured with
   `checkpointer: memory`.
8. **Analysis without langchain.** Reading and analysing recorded files (`model.py`, `reader.py`) depends
   on the standard library only, so downstream tooling and CI can run without the agent stack or an LLM.

## 3. Package layout

```
src/msagent/trajectory_recorder/
    __init__.py     Package docstring only. Deliberately imports nothing, so lightweight
                    consumers (export CLI, reader) do not pull langchain into the process.
    config.py       Pydantic schema of config.trajectory.recorder.yml + cached loader
                    (env path -> user config dir -> packaged default -> built-in defaults).
    serialize.py    JSON-safe conversion, full message serialization, redaction, truncation.
    recorder.py     TrajectoryRecorder: thread-safe append-only JSONL writer with the
                    event envelope, size cap and error-once logging.
    callback.py     TrajectoryCallbackHandler (langchain BaseCallbackHandler): observes
                    LLM and tool events of the whole run tree, including subagents.
    hooks.py        The ONLY integration surface: instrument_config / finish_turn /
                    record_approval / record_compression / record_event / reset.
                    Owns the process-wide recorder registry and active-turn state.
    model.py        Typed view of one file: Trajectory -> Turn -> AiMessage / ToolCall
                    (slots dataclasses, stdlib only).
    reader.py       Builds the model from JSONL: iter_events, extract_message_text,
                    load_trajectory, load_trajectories. stdlib only, no langchain — the
                    entry point for skill_evolver and any other analysis (section 9).
    export.py       Markdown/json renderers + standalone CLI
                    (python -m msagent.trajectory_recorder.export); reuses
                    reader.iter_events and reader.extract_message_text.

resources/configs/default/
    config.trajectory.recorder.yml   Shipped default configuration.

tests/ut/trajectory_recorder/
    test_reader.py     reader/model unit tests on the fixtures below, plus a subprocess
                       check that importing reader loads no langchain/langgraph module.
    test_callback.py   tool events delivered through langchain's CallbackManager
                       (the ignore_agent gate) and a recorder -> reader round-trip.
tests/fixtures/trajectories/*.jsonl
    Six hand-written schema-v1 files (31-32 lines each): normal session with a subagent,
    orphan tool.start, missing turn.end, malformed lines, recorder.limit, tool.result
    without tool.start. Deterministic by construction — never regenerate them by running
    an agent.
```

## 4. Runtime data flow

```
MessageDispatcher.dispatch()                       (src/msagent/cli/dispatchers/messages.py)
    |
    |  graph_config = trajectory_hooks.instrument_config(graph_config, context, run_id, user_message)
    |       - resolves/creates the per-thread TrajectoryRecorder (emits recorder.attach once)
    |       - emits turn.start
    |       - appends a TrajectoryCallbackHandler to graph_config["callbacks"]
    v
graph.astream(input, graph_config, ...)            (langgraph)
    |
    |  langchain propagates callbacks down the run tree via the ambient run context:
    |
    |    model node  --> on_chat_model_start / on_llm_end / on_llm_error / on_retry
    |    tool node   --> on_tool_start / on_tool_end / on_tool_error
    |    task tool   --> subagent.ainvoke(...)   <-- callbacks are inherited here too,
    |                                                so subagent-internal LLM/tool events
    |                                                are captured with parent_span_id set
    |                                                to the task tool's span and
    |                                                graph.checkpoint_ns = "tools:<id>|model:<id>"
    v
TrajectoryCallbackHandler ---> TrajectoryRecorder.emit() ---> JSONL append (under a lock)
    |
    v
trajectory_hooks.finish_turn(context, run_id, status | error)   -> emits turn.end
```

Two properties make subagent capture work without touching deepagents:

- callbacks passed in the top-level `RunnableConfig` are inherited by nested runnable invocations
  (verified against the project venv: deepagents 0.4.8 `task` tool calling `subagent.ainvoke()` with no
  explicit config);
- langgraph stamps run metadata (`langgraph_node`, `langgraph_step`, `checkpoint_ns`, `ls_model_name`, ...)
  onto each run; the handler stores a filtered subset in the `graph` field of events. `checkpoint_ns` is a
  `|`-separated chain of `<node>:<task-id>` segments: root-agent events carry a single segment
  (`model:<id>`, `tools:<id>`), events inside a subagent carry the parent tools node as prefix
  (`tools:<id>|model:<id>`), and that prefix identifies the `task` invocation an event belongs to. The
  root agent's namespace is therefore never empty — "non-empty `checkpoint_ns`" does not mean "subagent".

## 5. Storage layout and file format

Files live next to the other project-scoped state:

```
~/.msagent/state/projects/<project-slug>-<sha12>/trajectories/{agent}_{thread_id}.jsonl
```

(`MSAGENT_HOME` overrides `~/.msagent`; directory and filename template are configurable.)

Each line is one event. Envelope fields present on **every** event:

| Field | Meaning |
|---|---|
| `v` | schema version (currently `1`) |
| `event` | event type (catalog below) |
| `ts` | UTC ISO-8601 timestamp, millisecond precision |
| `seq` | monotonic counter within one writer instance |
| `rec` | writer instance uuid — `(rec, seq)` is unique across process restarts |
| `thread_id`, `agent` | conversation thread and agent name (lines are self-contained) |

### Event catalog

| Event | Emitted by | Key payload fields |
|---|---|---|
| `recorder.attach` | hooks (first use of a recorder instance) | `schema_version`, `capture_level`, `working_dir`, `model`, `model_display`, `approval_mode`, `app_version`, `platform`, `os_version` |
| `turn.start` | hooks (`instrument_config`) | `run_id`, `source` (`dispatch` \| `resume`), `user_message` (full text), `model`, `approval_mode` |
| `turn.end` | hooks (`finish_turn`) | `run_id`, `status` (`completed` \| `error`), `duration_ms`, `error_type`, `error` |
| `llm.request` | callback (level `llm_io` only) | span fields, `model`, `messages` (the **exact** serialized message window sent to the LLM) or `prompts`, `message_count`, `graph` |
| `message.ai` | callback (`on_llm_end`) | span fields, `message` (full serialized AIMessage: content blocks, reasoning, tool_calls, response_metadata), `message_id`, `usage` (input/output tokens), `model`, `tool_call_count`, `duration_ms`, `graph` |
| `llm.response` | callback (fallback when a generation carries no message) | span fields, `llm_output` |
| `llm.error` / `llm.retry` | callback | span fields, `error_type`, `error`, `duration_ms` / `attempt`, `wait_seconds` |
| `tool.start` | callback (optional, `capture.tool_starts`) | span fields, `name`, `input` (structured args when available), `graph` |
| `tool.result` | callback (`on_tool_end`) | span fields, `name`, `status`, `duration_ms`, `graph`, and one of: `message` (serialized ToolMessage), `command_update_keys` + `messages` (langgraph `Command`, e.g. the `task` tool), or `output` |
| `tool.error` | callback | span fields, `name`, `error_type`, `error`, `duration_ms` |
| `approval.decision` | hooks (`record_approval`) | `run_id`, `interrupt_id`, `request` (interrupt payload), `decision` (resume value, including edited args) |
| `context.compression` | hooks (`record_compression`) | `run_id`, `messages_offloaded`, `messages_kept`, `tokens_before/after`, `pct_decrease`, `file_path` |
| `recorder.limit` | recorder | written once when `limits.max_file_mb` is exceeded; recording then stops for that thread |

Span fields on every callback-produced event: `run_id` (the user turn), `span_id` (langchain run id),
`parent_span_id` — together with `graph.checkpoint_ns` they reconstruct the full execution tree.

Two details that matter to consumers: `model` in `recorder.attach` / `turn.start` is the configured alias
(e.g. `default`) — the resolved name is `model_display` (e.g. `deepseek-v4-pro (openai)`) and the API model
name is `graph.ls_model_name` on `message.ai`; and `tool.result.status` is overwritten by the ToolMessage's
own status when the tool returns one (`success` / `error`), so a `tool.result` line can legitimately report
an error without a `tool.error` line.

## 6. Configuration

Resolution order (first existing file wins):

1. `MSAGENT_TRAJECTORY_CONFIG=/path/to/file.yml` (explicit override)
2. `<MSAGENT_HOME>/config/config.trajectory.recorder.yml` (per-user override)
3. packaged default `resources/configs/default/config.trajectory.recorder.yml`
4. built-in defaults (recording **on**, level `messages`)

`MSAGENT_TRAJECTORY_DISABLED=1` is a kill switch that overrides any file. An invalid config never breaks
startup — defaults are used and a warning is logged. The config is cached per process
(`reset_config_cache()` for tests).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | master switch |
| `capture.level` | `messages` | `off` / `messages` (final messages, tools, turns) / `llm_io` (+ exact per-call LLM prompt windows; verbose) |
| `capture.tool_starts` | `true` | emit `tool.start` in addition to `tool.result` |
| `capture.retries` | `true` | emit `llm.retry` |
| `capture.graph_metadata` | `true` | attach `graph` (langgraph node/step/namespace) to events |
| `output.directory` | `trajectories` | relative → resolved against the project state dir; absolute used as-is |
| `output.filename` | `{agent}_{thread_id}.jsonl` | per-thread file name template |
| `limits.max_field_chars` | `0` (unlimited) | per-string truncation |
| `limits.max_file_mb` | `0` (unlimited) | hard per-file cap (emits `recorder.limit`, then stops) |
| `redaction.patterns` / `.replacement` | `[]` / `[REDACTED]` | regexes applied to every captured string |

## 7. Integration with the main codebase

The integration contract: **only `msagent.trajectory_recorder.hooks` is imported** from existing modules, every hook
call is a plain statement (no control flow depends on it), and every hook swallows its own exceptions. The
`context` argument is duck-typed (the CLI `Context` object is read via `getattr`), so the trajectory package
has no import dependency on `msagent.cli`.

### Required — `src/msagent/cli/dispatchers/messages.py`

| Location | Insertion |
|---|---|
| module imports | `from msagent.trajectory_recorder import hooks as trajectory_hooks` |
| `MessageDispatcher.dispatch()`, right after `run_id = str(uuid.uuid4())` | `graph_config = trajectory_hooks.instrument_config(graph_config, context=ctx, run_id=run_id, user_message=content)` |
| `dispatch()`, after the stream/invoke `if/else` block | `trajectory_hooks.finish_turn(context=ctx, run_id=run_id, status="completed")` |
| `dispatch()`, first line of the existing `except Exception as e:` | `trajectory_hooks.finish_turn(context=self.session.context, error=e)` |
| `resume_from_interrupt()`, after `graph_config = RunnableConfig(...)` | `graph_config = trajectory_hooks.instrument_config(graph_config, context=ctx, run_id=str(uuid.uuid4()), source="resume")` |
| `resume_from_interrupt()`, after `await self._stream_response(...)` | `trajectory_hooks.finish_turn(context=ctx, status="completed")` |

This single file covers both the streaming (`astream`) and non-streaming (`ainvoke`) paths, because both
receive the instrumented `graph_config` built in `dispatch()`.

### Optional, not wired yet — `src/msagent/cli/handlers/interrupts.py`

After each existing `self._record_user_response(interrupt, choice)` call in `InterruptHandler.handle()`:
`trajectory_hooks.record_approval(context=self.session.context, interrupt=interrupt, resume_value=choice)`.
Unlike the audit writer, this records approvals regardless of the per-agent `audit_log` setting.

### Optional, not wired yet — `src/msagent/cli/handlers/compress.py`

After the state update in `CompressionHandler.handle()` (once `offload_result` is applied):
`trajectory_hooks.record_compression(context=ctx, messages_offloaded=..., messages_kept=...,
tokens_before=..., tokens_after=..., pct_decrease=..., file_path=offload_result.new_event.get("file_path"))`.

As of 2026-09-04 neither optional hook (nor `record_event`) has a call site, so `approval.decision` and
`context.compression` do not occur in recorded files. The reader handles them regardless.

### What was deliberately NOT touched

- `Session` (`cli/core/session.py`): no lifecycle wiring needed — recorders are created lazily per thread
  by the manager in `hooks.py`, and thread switches (`/threads`, `/clear`) are handled automatically because
  every hook call carries the current `context.thread_id`.
- `AgentFactory` / deepagents internals: capture rides on standard langchain callback propagation.
- `configs/registry.py`: the trajectory config has its own tiny loader following the same lookup convention
  (user config dir, then `importlib.resources.files("resources")/configs/default`), keeping the package
  droppable into the tree without registry changes.

## 8. Relationship to existing persistence layers

The recorder does not replace anything; it adds the processing-grade layer:

| Layer | Role after this change |
|---|---|
| `checkpoints.sqlite` | unchanged — runtime state for resume/`/threads` |
| `audit_log/` | unchanged — compliance-oriented delegation/interaction log |
| `conversation_history/` | unchanged — compression offload target |
| `trajectories/` | **new** — canonical full-fidelity event log for downstream processing |

## 9. Consuming trajectories

CLI (stdlib-only import path, safe to run anywhere):

```
python -m msagent.trajectory_recorder.export list   [-w DIR | --state-dir DIR]
python -m msagent.trajectory_recorder.export show   --thread <id> [--max-chars N]
python -m msagent.trajectory_recorder.export export --thread <id> --format json|jsonl|md [-o FILE]
```

Low-level library: `iter_events(path, malformed=...)` and `extract_message_text(message)` in
`msagent.trajectory_recorder.reader` (re-exported by `export`); `list_trajectories(dir)`,
`find_trajectory_file(dir, thread_id)`, `render_markdown(events)` in `msagent.trajectory_recorder.export`.

### Typed reader (`msagent.trajectory_recorder.reader`)

The analysis entry point. Stdlib only — importing it must not load langchain (enforced by a unit test), so
`skill_evolver` and CI can consume trajectories without the agent stack or an LLM.

```python
from msagent.trajectory_recorder.reader import load_trajectories, load_trajectory

trajectory = load_trajectory(path)                        # one file -> Trajectory
recent = load_trajectories(directory, agent="Profiler",   # newest by mtime first; filters read only
                           thread_ids=[...], limit=20)    # the first event of each file; limit keeps N newest
```

Model (`msagent.trajectory_recorder.model`, slots dataclasses):

| Type | Fields |
|---|---|
| `Trajectory` | `path`, `thread_id`, `agent`, `model`, `working_dir`, `started_at`, `turns`, `truncated_by_limit`, `skills_consulted`, `malformed_lines` |
| `Turn` | `run_id`, `seq_start`, `user_message`, `source`, `ai_messages`, `tool_calls`, `approvals`, `retries`, `compressions`, `status` (`completed` \| `error` \| `truncated`), `error_type`, `duration_ms` |
| `AiMessage` (frozen) | `seq`, `span_id`, `text`, `tool_call_names`, `usage`, `duration_ms`, `subagent` |
| `ToolCall` (frozen) | `span_id`, `parent_span_id`, `name`, `args`, `status` (`ok` \| `error` \| `orphan`), `output_text`, `error_type`, `error`, `duration_ms`, `seq_start`, `seq_end`, `subagent` |

Assembly rules:

| Rule | Behaviour |
|---|---|
| Tool spans | `tool.start` and `tool.result` / `tool.error` are joined by `span_id`. A start with no result before EOF is `status="orphan"` (`seq_end=None`) — the signature of an interrupted session, deliberately kept. A result with no start (`capture.tool_starts=false`) yields `args={}` and `seq_start == seq_end`. A `tool.result` whose ToolMessage status is `error` maps to `status="error"` with the message text in `output_text`. |
| Turns | Opened by `turn.start`, closed by `turn.end` with the same `run_id`; without `turn.end` (Ctrl+C, `recorder.limit`) the turn stays `status="truncated"`. Callback events are routed by their `run_id`, so events written after a `turn.end` still join their turn; events with a null `run_id` go to the most recently started turn, or to a synthetic `__prelude__` turn when none exists; a `run_id` never announced by `turn.start` yields a turn with `source="unknown"`. |
| Subagents | `subagent` = `graph.checkpoint_ns` without its last segment (`tools:<id>` for everything inside one `task` invocation), `None` for the root agent. |
| Header | `thread_id` / `agent` from the first event's envelope, `started_at` = its `ts`; `model` = `model_display`, else `model`, else the first `turn.start.model`; a second `recorder.attach` (process restart) is ignored. |
| Skills | `skills_consulted` = `args["name"]` of every `get_skill` call, taken from `tool.start` and from `message.ai` tool calls, first-seen order, deduplicated. |
| Robustness | Broken JSON lines, non-object lines and objects that are not schema-v1 events are skipped silently and counted in `malformed_lines`. A line with `v != 1`, a `turn.end` with an unknown status, or a file without a single valid event raises `TrajectoryReadError` — the analyzer fails loudly instead of masking bad data. Files are streamed line by line; `llm.request` payloads are never retained. |

Mapping to the target use cases:

- **Analytics** — `Turn.duration_ms` / `status` / `retries` / `compressions`, `AiMessage.usage`,
  `ToolCall.duration_ms` / `status`; or the raw events (`duration_ms`, `usage`, `llm.retry`,
  `approval.decision`).
- **SKILL.md mining** — iterate `Trajectory.turns` with `status == "completed"`, replay `Turn.ai_messages`
  and `Turn.tool_calls` in `seq` order (subagent steps carry `subagent`), use `skills_consulted` to see
  which skills the agent already leaned on; successful chains carry exact tool names and `args`.
- **Knowledge Graph** — nodes from events (turn, LLM call, tool call, subagent invocation, error, approval),
  identities from `(rec, seq)`, `run_id`, `span_id`, `message_id`, `tool_call_id` (inside serialized
  messages); edges from `parent_span_id`, `run_id` and `graph.checkpoint_ns`. Serialized messages are
  reversible into langchain objects via `messages_from_dict` when needed.

## 10. Failure model and safety

- Every hook and callback method is wrapped: recording failures are logged (`warning` once per recorder for
  write errors, `debug` elsewhere) and never propagate to the agent loop.
- The callback handler declares `raise_error = False` and `run_inline = True` (ordered, inline execution;
  writes are small appends under a `threading.Lock`, safe for parallel tool calls).
- Chain-level callbacks are ignored (`ignore_chain = True`) — one event per langgraph node would be noise.
  `ignore_agent` must stay `False`: langchain_core dispatches `on_tool_start/end/error` under that gate
  (`langchain_core/callbacks/manager.py`). It was `True` until 2026-09-04, which silently dropped every
  tool event; `tests/ut/trajectory_recorder/test_callback.py` pins the fix by driving the handler through a
  real `CallbackManager`.
- Redaction runs on every captured string; note that with `redaction.patterns: []` (default) tool outputs
  may contain secrets — configure patterns in deployments where that matters.
- Kill switch: `MSAGENT_TRAJECTORY_DISABLED=1` (no file edits required).

## 11. Known limitations / future work

- No `turn.end` is written when a turn is cancelled mid-flight (Ctrl+C); the next `turn.start` makes the
  gap detectable in data, and the reader reports such turns as `status="truncated"` with their pending tool
  spans as `orphan`.
- Files recorded before the `ignore_agent` fix (2026-09-04) contain no `tool.*` events at all — only
  `message.ai`, `turn.*` and `llm.*`. For them the reader yields empty `Turn.tool_calls`; tool usage can
  still be inferred from `AiMessage.tool_call_names` and `skills_consulted` (mined from `message.ai`).
- `resume_from_interrupt()` currently calls `_stream_response()` twice, the second time after
  `finish_turn()`, so a resumed turn can be followed by callback events carrying its `run_id` after its
  `turn.end`. The reader routes them to that turn by `run_id`; the dispatcher bug itself is tracked
  separately.
- `capture.level: llm_io` records the full prompt window per LLM call — file size grows roughly
  quadratically with conversation length; `messages` is the default for a reason.
- The summarization LLM call made *during* compression (`perform_conversation_offload`) is not itself
  instrumented; the compression is recorded as a single `context.compression` event — once
  `record_compression` is wired in (section 7).
- Historical sessions predating the recorder exist only in `checkpoints.sqlite`; a backfill converter into
  the same event schema is a possible follow-up (with honest gaps: no subagent internals, no prompts).
- One writer per process per thread file; concurrent CLI processes on the same thread would interleave
  lines (appends are atomic-ish per line, and `rec` disambiguates writers), but this is not a supported
  scenario.

## 12. Verification

Unit tests (`tests/ut/trajectory_recorder/`, run with `pytest tests/ut/trajectory_recorder -q`):

- `test_reader.py` — 18 test functions (26 cases) over the six hand-written fixtures in
  `tests/fixtures/trajectories/` plus inline files: header fields, both turn outcomes, all three
  `tool.result` shapes, `tool.error`, string / list / null tool inputs, subagent attribution, retries /
  approvals / compressions, late events after `turn.end`, orphan spans, missing `turn.end` (closed by the
  next `turn.start` and by EOF), a second `recorder.attach`, malformed lines with exact line numbers,
  `recorder.limit`, results without starts, `extract_message_text` block handling, loud failures
  (`v != 1`, bad `turn.end` status, missing `span_id`, empty file), directory loading (mtime order,
  filters, `limit`), `export` reusing the reader helpers, and a subprocess check that importing `reader`
  loads no `langchain*` / `langgraph*` module. `reader.py` line coverage: 100%.
- `test_callback.py` — tool events delivered through a real `langchain_core` `CallbackManager` (the
  `ignore_agent` gate) and read back by `load_trajectory`.

Original bring-up (langchain-core / langgraph / deepagents 0.4.8): `py_compile` of all new and patched
modules; import of the patched CLI modules; a functional smoke test driving `instrument_config` →
simulated `on_chat_model_start` / `on_llm_end` / `on_tool_start` / `on_tool_end` (ToolMessage and
`Command` outputs) / `on_tool_error` / `on_retry` → `record_approval` / `record_compression` /
`finish_turn`, asserting event ordering, subagent `checkpoint_ns`, usage propagation, `llm_io` prompt
capture, redaction, truncation, export CLI round-trip and the env kill switch. That smoke called the
handler methods directly and thus bypassed the manager-level `ignore_agent` gate — which is why the
missing tool events went unnoticed until the reader was run against real files.