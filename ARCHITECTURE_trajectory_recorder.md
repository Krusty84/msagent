# Trajectory Recorder — Architecture

Status: implemented (`src/msagent/trajectory/`), schema version 1.

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

1. **Isolation.** All functionality lives in a dedicated package, `src/msagent/trajectory/`. The rest of the
   codebase interacts with it through exactly one module (`msagent.trajectory.hooks`); integration is a
   handful of one-call insertions.
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

## 3. Package layout

```
src/msagent/trajectory/
    __init__.py     Package docstring only. Deliberately imports nothing, so lightweight
                    consumers (export CLI) do not pull langchain into the process.
    config.py       Pydantic schema of config.trajectory.recorder.yml + cached loader
                    (env path -> user config dir -> packaged default -> built-in defaults).
    serialize.py    JSON-safe conversion, full message serialization, redaction, truncation.
    recorder.py     TrajectoryRecorder: thread-safe append-only JSONL writer with the
                   