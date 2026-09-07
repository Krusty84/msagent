# Experience Graph — Architecture

Status: P0 + P0.5 + P1 (`src/msagent/exgraph/`), schema version 2.
Branch: `feature/experience-graph`.

## 1. Purpose

Turn recorded msAgent trajectories into an **experience graph**: a relational
store of what the agent tried, whether the attempt failed, which tools ran,
and (when present) which skill proposal was distilled from that thread.

This is not a knowledge graph of world entities. Atomic units are **cases**
(one user turn / attempt), not triples like `Service → dependsOn → Redis`.

Primary consumers:

- offline inspection (`python -m msagent.exgraph.export`);
- later recipe / insight mining (P1+);
- Skill Evolver evidence (P3), without writing `SKILL.md` itself.

## 2. Task grain (P0 decision)

Interactive msAgent work is a multi-turn investigation. Hashing every user
message into its own task would split “try this next” follow-ups and break
`FIXED_BY` in P1.

P0 therefore uses **one `TaskAnchor` per thread** (`task:thread:{thread_id}`).
Each `Case` still stores that turn’s user text as `x`, so a later pass can
split anchors without rebuilding identities of cases or steps.

A `Thread` node sits above the anchor for provenance (`working_dir`, model).

## 3. P0 schema

Nodes: `Thread`, `TaskAnchor`, `Case`, `Step` (`kind=tool|llm`),
`SubagentRun`, optional `SkillDoc`, P1 `Episode`, workspace `Recipe`.

Edges: `HAS_TASK`, `CONTAINS`, `NEXT_CASE`, `HAS_STEP`, `PARENT_OF`,
`DELEGATES`, `IN_SUBAGENT`, `DERIVED_SKILL`, P1 `HAS_EPISODE`,
`FIXED_BY`, `INSTANTIATES`.

Case payload (EXG tuple on recorder events):

- `x` — user message of the turn
- `y` — last root-agent assistant text
- `r` — `golden` | `warning` | `unknown`
- `sigma` — tool path, errors, retries, approvals, tokens, subagents, skills

Prelude turns (`run_id=__prelude__`) are skipped.

## 4. Outcome policy v1

`turn.end status=completed` is not success.

- `warning` if the turn ended in `error` or any tool span has `status=error`
- `golden` only when the caller passes `--outcome success` on a non-warning turn
- `unknown` otherwise
- `--outcome fail` forces non-warning turns to `warning`

The policy name is stored on every case (`outcome_policy`) so labels can be
recomputed later.

## 5. Package

```
src/msagent/exgraph/
    __init__.py     no imports (lightweight CLI)
    config.py       config.exgraph.yml loader
    schema.py       ids, Node, Edge, CaseRecord
    sources.py      the only import of trajectory_recorder
    cases.py        deterministic ingest from the typed Trajectory model
    skills.py       optional SkillDoc scan
    enrich.py       P1: features.extract_episodes + FIXED_BY
    workspace.py    P1 overlay: features.mine_cross_session
    consumer.py     read-only markdown for Skill Evolver
    store.py        upsert JSONL under <state>/exgraph/
    export.py       CLI build | show | export
```

Storage (derived, never overwrites source JSONL):

```
<project-state>/exgraph/<agent>_<thread_id>/     # thread shard
    manifest.json
    nodes.jsonl
    edges.jsonl
    cases.jsonl
<project-state>/exgraph/_workspace/              # recipes only
    manifest.json
    nodes.jsonl
    edges.jsonl
```

Rebuild is upsert-by-id.

## 6. Skills from this trajectory (P0.5)

Skill Evolver owns detectors, classify/render, and `SKILL.md` writing.
Exgraph only **points** at files that already exist. It does not import
`skill_evolver` in P0.5 (YAML + path scan only).

When `skills.enabled` is true, ingest looks at, fail-open:

- `<working_dir>/skills/.proposals/<thread>/**/SKILL.md` — current writer root
- `<output_dir>/.proposals/<thread>/**/SKILL.md` if `config.skill.evolver.yml`
  sets `output_dir` (read as YAML from `~/.msagent/config/`)
- `<working_dir>/.proposals/<thread>/**/SKILL.md` — legacy P0 location
- `<working_dir>/skills/**/SKILL.md` outside `.proposals` whose
  `provenance.json.thread_ids` or footer cites this thread (accepted)

Identities (path is an attribute, not the id):

- proposal: `skill:proposal:{thread}:{name}`
- accepted: `skill:{name}`

A `/skill-review accept` move therefore does not reuse the draft id.
Both may exist at once; each gets `DERIVED_SKILL` from the Thread and
TaskAnchor. Missing folders are normal and silent.

P1 will import `skill_evolver.features` / `writer.batch_dir_name` and
store Recipes in `<state>/exgraph/_workspace/` (overlay). That is not P0.5.

## 7. P1 and Skill Evolver

Detectors are not copied. `enrich.py` calls `features.extract_episodes`;
`workspace.py` calls `features.mine_cross_session` on a **caller-supplied**
pool. The CLI `--all` pool size is the evolver constant
`CROSS_SESSION_LIMIT` (20). `select_trajectories` / `_gather_evidence` are
not modified.

After the evolver builds its own episode bundle it calls
`skill_evolver.exgraph_context.attach_stored_graph`: persist this thread's
shard (no extra JSONL loads) and append a stored-graph section to the
classify prompt. Fail-open: any error leaves the bundle unchanged.
`valid_seq` is still only the evolver episodes, so classify cannot cite
invented seqs.

## 8. What is still later

`SIMILAR_TO`, Insight-from-candidates, slash command `/exgraph`,
live retrieval, embeddings, external graph databases.

Documented future hook for online growth: `trajectory_hooks.finish_turn` may
enqueue `exgraph` ingest behind `online: false`. It must not block the agent.

## 9. CLI

```
python -m msagent.exgraph.export build --thread <id> [--outcome success|fail]
python -m msagent.exgraph.export build --all
python -m msagent.exgraph.export build --path /path/to/thread.jsonl
python -m msagent.exgraph.export show --thread <id>
python -m msagent.exgraph.export export --thread <id> --format json
python -m msagent.exgraph.export viz -o artifacts/exgraph_growth_demo.html
python -m msagent.exgraph.visualize --fixtures tests/fixtures/trajectories -o artifacts/exgraph_growth_demo.html
```

Testing procedures (unit + intensive A/B + growth HTML):
`exgraph_intensive_testing_procedures.md`.
Intensive file: `tests/it/exgraph/test_evolver_value.py` (8 tests, no LLM).

Kill switch: `MSAGENT_EXGRAPH_DISABLED=1` (also `true`/`yes`/`on`).

Checked live on every entry — CLI `build`/`build --all`, `remember_thread`,
the evolver `attach_stored_graph` hook, and the classify appendix. When set,
no shards or overlay are written and Skill Evolver sees the original bundle.
YAML `enabled: false` has the same effect. Inspection `show`/`export` of an
already-saved shard still works.
