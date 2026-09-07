# ExGraph value for Skill Evolver

Deterministic A/B on recorder-shaped fixtures (no LLM).

## Corpus

- `skill_evolver_signals.jsonl` — Profiler thread-signals (failing msprof flags, RU correction, denied write)
- `exgraph_reuse.jsonl` — Profiler thread-reuse (correct flags on first try)
- `exgraph_accuracy.jsonl` — Accuracy control thread (NaN dump, should not own profiler recipes)

## Evolver-only classify bundle (graph off)

- episodes: 5 kinds=['approval_denied', 'error_recovery', 'retry_loop', 'user_correction']
- evidence_score: 3.80
- valid evidence seqs: [2, 4, 5, 7, 8, 10, 11, 17, 26, 29, 32]
- chars: 3502
- facts present: ['Evidence:', 'approval_denied', 'error_recovery', 'user_correction']

## Same bundle after attach_stored_graph

- chars: 4048 (delta +546)
- facts present: ['Evidence:', 'Experience graph', 'Recipes instantiated', 'approval_denied', 'error_recovery', 'fixed_by', 'outcome=', 'user_correction']
- FIXED_BY edges in shard: 2
- case outcomes: {'run-1': 'warning', 'run-2': 'unknown', 'run-3': 'unknown'}
- case tool paths: {'run-1': ['bash', 'bash', 'bash', 'read_file'], 'run-2': ['bash', 'grep'], 'run-3': ['bash', 'ls']}
- cross-session recipes: 2

### Appendix added to classify

```
## Experience graph (stored)

Thread `thread-signals` agent `Profiler`.
- Case `run-1` outcome=warning tools=bash → bash → bash → read_file
- Case `run-2` outcome=unknown tools=bash → grep
- Case `run-3` outcome=unknown tools=bash → ls
Episodes:
- error_recovery weight=0.6
- error_recovery weight=0.6
- user_correction weight=0.9
- retry_loop weight=0.7
- approval_denied weight=1.0
Corrections:
- step:s2 fixed_by step:s6 via error_recovery
- case:run-1 fixed_by case:run-2 via user_correction
Recipes instantiated:
- bash>read_file support=2
```

## Why this is value, not duplication

Skill Evolver already lists episodes as independent bullets.
The graph adds relations the detectors do not emit:

- which *case* was corrected by which later case (`FIXED_BY`);
- per-turn outcome and tool path (the recipe grain);
- recipes that only exist when two threads share an n-gram;
- a SkillDoc link when a proposal cites the thread.

The appendix must not list `Evidence:` seqs, so classify `valid_seq`
stays the evolver set. `CROSS_SESSION_LIMIT` stays 20.

## Live data

Point the same comparison at a real project:

```
python -m msagent.exgraph.export build --all --working-dir /path/to/project
MSAGENT_EXGRAPH_DISABLED=1  # control: evolver bundle unchanged
```
