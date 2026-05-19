# Benchmark Builder

This repository is a small prototype for evaluating agents that identify slow
GPU cards in AI cluster telemetry.

The benchmark loop is:

1. Load one case YAML file, or every case YAML in a directory.
2. Run an agent against each case prompt and input data directory.
3. Persist the agent trace as JSON.
4. Compare the agent's predicted slow cards with the YAML ground truth.
5. Optionally run a structured judge over the trace and final answer.
6. Emit score, tool-call, token, and timing reports.

## Project Layout

```text
benchmarks/
  *.yaml                     # one benchmark case per file
data/mock/
  */                         # synthetic metrics, logs, events, topology
src/benchmark_builder/
  claude_cli.py              # real Claude CLI agent and judge adapters
  codex_cli.py               # real Codex CLI agent and judge adapters
  msagent_cli.py             # real msAgent CLI agent and judge adapters
  judge.py                   # deterministic local judge, useful for smoke tests
  metrics.py                 # token and timing aggregation
  schema.py                  # YAML loading and case model
  mock_agent.py              # deterministic simulated agent
  scoring.py                 # trace scoring rules
  trace.py                   # trace event builder
  run_benchmark.py           # CLI entry point
```

## Case Schema

Each case YAML has only four fields:

```yaml
id: slow_card_single_001
input_data_path: ../data/mock/slow_card_single_001
prompt: >
  Identify slow GPU cards in this cluster telemetry.
ground_truth:
  - node-a100-02/gpu3
```

Use `ground_truth: []` when there are no slow cards.

## Trace Shape

The runner writes one JSON trace per case. A trace contains ordered events:

- `thought`: compact planning note.
- `tool_call`: simulated or real tool invocation.
- `tool_result`: data returned by the tool.
- `observation`: evidence extracted from results.
- `final_answer`: structured result containing `slow_cards`.

The scorer reads `slow_cards` from the final answer and compares it with the
case's `ground_truth` list.

## Run Outputs

Each run writes:

```text
runs/<run_id>/
  traces/<case_id>.trace.json
  judge/<case_id>.judge.json       # omitted when --judge none
  metrics/<case_id>.metrics.json
  scores.json
  report.md
```

The trace includes agent metadata, elapsed time, and token usage:

```json
{
  "agent": {"name": "mock-slow-card-agent", "model": "heuristic-v0"},
  "duration_ms": 123,
  "token_usage": {
    "input_tokens": 1000,
    "output_tokens": 200,
    "total_tokens": 1200
  }
}
```

By default the runner uses `codex exec --json` for both the agent and judge. It
records real wall-clock time. Token usage is read from Codex JSONL events when
available; if the CLI does not emit usage events, the output marks token usage as
unavailable instead of estimating it.

Real Codex CLI and Claude CLI agent runs execute in a temporary isolated workspace. The runner
copies only the case input data into `input_data/` and gives the agent that
relative path, so the benchmark source, YAML ground truth, previous run outputs,
and heuristic implementation are not part of the agent workspace.

Codex CLI command executions and Claude CLI tool uses are normalized into
`tool_call` and `tool_result` trace events. Per-case metrics include an agent
tool-call summary with the count, tool names, commands, token usage, and elapsed
wall-clock time.

The msAgent adapter runs `msagent` in one-shot mode against the isolated
`input_data/` directory. It defaults to the built-in `Hermes` persona, which is
oriented toward Ascend profiling and performance diagnosis. It copies the local
`.msagent` configuration into the isolated workspace so project-local model and
agent settings are available without exposing benchmark YAML files. Override the
CLI path with `MSAGENT_CLI` when running from source, for example
`MSAGENT_CLI="uv --project /path/to/msagent run msagent"`, and override the
persona with either `--msagent-agent` or `MSAGENT_AGENT`.

## Scoring

The current scorer reports:

- `exact_match`: predicted slow-card set equals ground truth.
- `precision`: how many predicted cards are actually slow.
- `recall`: how many true slow cards were found.
- `f1`: harmonic mean of precision and recall; used as the case score.

The final score combines correctness and judge quality:

```text
final_score = 0.7 * correctness_f1 + 0.3 * (judge_overall_score / 5)
```

Expected final answer shape:

```json
{
  "slow_cards": ["node-a100-02/gpu3"]
}
```

## Run

Install the package or run directly with `PYTHONPATH`:

```bash
python3 -m pip install -e .
benchmark-builder --config benchmarks --out runs/mock-run
```

Without installation:

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks \
  --out runs/mock-run
```

For a local smoke test without model calls:

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks \
  --out runs/smoke \
  --agent heuristic \
  --judge heuristic
```

To run the real Codex CLI agent on a benchmark case without an LLM-as-a-judge
pass:

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks/kv_cache_type_page_seqlen_4096_bs_1_profile_count_0.yaml \
  --out runs/codex-cli-no-judge \
  --agent codex-cli \
  --judge none
```

To run the same case with Claude CLI:

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks/kv_cache_type_page_seqlen_4096_bs_1_profile_count_0.yaml \
  --out runs/claude-cli-no-judge \
  --agent claude-cli \
  --judge none
```

To run the same case with msAgent:

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks/kv_cache_type_page_seqlen_4096_bs_1_profile_count_0.yaml \
  --out runs/msagent-cli-no-judge \
  --agent msagent-cli \
  --msagent-agent Hermes \
  --judge none
```

The loader supports this simple four-field YAML shape even if PyYAML is not
installed.
