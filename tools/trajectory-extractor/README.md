# msagent trajectory extractor

Turns what an msagent session already recorded into a **normalized step graph**
that is fit to hand to SKILL.md synthesis.

This is deliberately the mechanical half of the job. It deduplicates, lifts
concrete values into placeholders, scrubs secrets and marks the spots that
usually become `scripts/` and troubleshooting entries — so the judgement half
(deciding the workflow, the boundaries, the input contract) is done against
clean data instead of a raw log.

It is a standalone module: nothing under `src/msagent/` is imported at import
time, nothing in the repository's build or test configuration refers to it, and
`msagent` itself is used only opportunistically (see *Dependencies*).

## Install / run

No dependencies for the audit, history and trace sources:

```bash
PYTHONPATH=tools/trajectory-extractor python -m trajectory_extractor --help
```

Or install it as its own package, which also provides the
`msagent-trajectory` command:

```bash
pip install -e tools/trajectory-extractor
```

Reading LangGraph checkpoints additionally needs
`langgraph-checkpoint-sqlite` (already present in any environment where msagent
runs): `pip install -e 'tools/trajectory-extractor[checkpoints]'`.

## Usage

```bash
# what has been recorded for this project?
msagent-trajectory --working-dir ~/work/qwen-quant --list-threads

# extract one thread
msagent-trajectory --working-dir ~/work/qwen-quant \
                   --thread-id 6f2c1a90 \
                   --out steps.json

# add a trace file captured with `msagent ... --trace-jsonl run.jsonl`
msagent-trajectory --working-dir ~/work/qwen-quant --trace-file run.jsonl
```

Without `--thread-id` the most recent thread is used. JSON goes to stdout (or
`--out`), a summary and every warning go to stderr. Exit code is `1` when no
steps could be extracted.

Useful flags: `--source` to restrict readers, `--no-redact` to keep raw values
(not for anything leaving the machine), `--no-subagents`, `--result-chars`,
`--min-script-occurrences`.

## Sources

| Source | Where it reads | What it contributes | Notes |
|---|---|---|---|
| `checkpoint` | `checkpoints.sqlite` | tool calls and results, untruncated | needs LangGraph; written for every `checkpointer: sqlite` agent |
| `audit` | `audit_log/<Agent>_<thread>.jsonl` | task text, HITL decisions, subagent delegations | needs `audit_log.enabled` — only Quantizer ships with it on |
| `history` | `conversation_history/<thread>.md` | messages that compaction offloaded | text only; tool arguments do not survive offloading |
| `trace` | `--trace-jsonl` file | same events as `checkpoint`, one flat file | text capped at 4000 chars; carries no user messages |

All of them live under `$MSAGENT_HOME/state/projects/<slug>-<hash>/`, resolved
the same way msagent resolves it.

When both `checkpoint` and `trace` yield events, checkpoints win (untruncated)
and a warning says so. Nothing is silently dropped: every gap, truncation and
skipped namespace is reported in `warnings`.

## What the pipeline does

1. **Redact** — secret assignments, vendor tokens, auth headers, URL
   credentials, e-mail addresses; home directories keep their shape but lose the
   account name. Runs first, so placeholders are derived from clean values.
2. **Pair and collapse** — a call plus its result becomes one step; consecutive
   identical calls collapse into one step carrying `repeat_count`. A run that
   eventually succeeded is reported as a success.
3. **Parameterize** — paths, URLs, addresses, versions and long opaque ids are
   replaced consistently across every step, so a value produced by one step and
   consumed by another is visibly the same placeholder. A whole argument value
   takes its name from the argument key (`model_path` → `<MODEL_PATH>`); a path
   below one already lifted reuses its parent (`<MODEL_PATH>/config.json`).
   Device ordinals are replaced in context (`--device <DEVICE_IDS>`), never by
   bare value.
4. **Mark script candidates** — commands that repeat, are multi-line, are long,
   or form a run of three or more consecutive shell steps.
5. **Detect recoveries** — a failed step plus the corrected call that fixed it,
   with the changed arguments isolated. This is the material for a skill's
   troubleshooting and "do not do this" sections, which successful runs alone
   never yield.

## Output

```jsonc
{
  "schema_version": 1,
  "thread_id": "...", "agent": "Quantizer", "sources": [...],
  "stats": { "steps": 5, "failed_steps": 2, "recoveries": 1, ... },
  "user_messages": ["..."],          // the task, as stated
  "offloaded_context": ["..."],      // what compaction removed
  "phases": [...],                   // subagent delegations, HITL decisions
  "steps": [                         // the workflow
    {"index": 3, "tool": "execute", "ok": true,
     "args": {"command": "msmodelslim quant --model <PATH_1> --cfg w8a8_dynamic.yaml"}}
  ],
  "parameters": [...],               // seeds the skill's input contract
  "script_candidates": [...],        // seeds scripts/
  "recoveries": [...],               // seeds troubleshooting
  "warnings": ["..."]                // read these before trusting the rest
}
```

`Step.args` is parameterized; `Step.args_raw` keeps the originals in memory but
is left out of the export on purpose, so the JSON is safe to pass around.

## Scope

One trajectory in, one document out. Merging several runs into an invariant
core plus a variability table — the step that actually makes a skill general —
is not done here and is a deliberate next stage.

## Tests

```bash
python -m pytest tools/trajectory-extractor
```

Everything except the LangGraph-backed checkpoint read is covered: the
message-to-event conversion is duck-typed and tested with stubs, and thread
listing is tested against a synthetic SQLite database. The deserialization call
itself needs a real msagent environment.
