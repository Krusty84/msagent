# Benchmark Builder

这是一个用于评估 Agent 的 benchmark harness。它会读取 case YAML，给被测 Agent 一个任务和数据目录，保存 Agent 运行 trace，然后用 LLM as a judge 进行评分。

核心流程：

1. 读取单个 case YAML，或读取目录下所有 `.yaml` / `.yml` case。
2. 将 case 的 `input_data_path` 复制到隔离工作区中的 `input_data/`。
3. 只把 `prompt` 和 `input_data/` 暴露给被测 Agent。
4. 要求 Agent 输出统一 JSON 答案。
5. 将 Agent trace、最终答案、`must_include` 和 `scoring_prompt` 交给 judge。
6. judge 判断答案是否覆盖 `must_include`，并按 `scoring_prompt` 给出 0 到 5 分。
7. 输出 trace、judge、metrics、scores 和 Markdown report。

## 目录结构

```text
benchmarks/
  *.yaml                     # 每个文件一个 benchmark case
data/
  */                         # case 输入数据
src/benchmark_builder/
  codex_cli.py               # Codex CLI Agent 和 judge adapter
  claude_cli.py              # Claude CLI Agent 和 judge adapter
  msagent_cli.py             # msAgent CLI Agent 和 judge adapter
  judge.py                   # 本地 heuristic judge，仅用于 smoke test
  metrics.py                 # token、耗时和 tool call 聚合
  schema.py                  # case YAML 加载和校验
  mock_agent.py              # 本地 heuristic agent，仅用于 smoke test
  trace.py                   # trace event builder
  run_benchmark.py           # CLI 入口
```

## Benchmark 接入标准

每个 case YAML 必须包含以下字段：

- `input_data_path`：case 数据目录或数据文件路径。相对路径按 YAML 文件所在目录解析。
- `prompt`：给被测 Agent 的任务说明。
- `must_include`：答案必须语义覆盖的内容列表。judge 会逐项判断是否覆盖。
- `scoring_prompt`：judge 的评分标准 prompt，用于给出 0 到 5 分。

`id` 是可选字段；缺省时使用 YAML 文件名作为 case id。

示例：

```yaml
input_data_path: ../data/cases/example
prompt: >
  请根据 input_data 中的数据回答问题。
must_include:
  - 必须覆盖的事实或结论 A
  - 必须覆盖的事实或结论 B
scoring_prompt: >
  按 0-5 分评价答案质量，重点看结论是否准确、证据是否充分、推理是否清晰。
```

旧格式 `ground_truth` 不再支持；`slow_cards`、precision、recall、F1 等旧的 slow-card 专用评分也已移除。

## Agent 输出格式

被测 Agent 必须返回一个 JSON object：

```json
{
  "answer": "最终答案文本",
  "evidence": ["支持答案的证据 1", "支持答案的证据 2"],
  "reasoning_summary": "简短、可审计的推理摘要",
  "confidence": 0.8
}
```

注意：

- `must_include` 和 `scoring_prompt` 只提供给 judge，不会提供给被测 Agent。
- Agent 只能访问隔离工作区中的 `input_data/`。
- Agent 不应读取 benchmark 源码、case YAML、历史 run 输出或父目录。

## LLM As A Judge

judge 有两个职责：

1. 对 `must_include` 中的每一项判断 `covered=true/false`，使用语义覆盖标准，允许同义表达和合理改写。
2. 根据 `scoring_prompt` 给出 `rubric_score`，范围为 0 到 5。

最终分数规则：

```text
if any must_include item is missing:
  score = 0
else:
  score = rubric_score / 5
```

judge 输出会保存为 `judge/<case_id>.judge.json`，其中包含：

```json
{
  "must_include_pass": true,
  "must_include_results": [
    {
      "item": "必须覆盖的事实或结论 A",
      "covered": true,
      "reason": "答案语义覆盖了该要求。"
    }
  ],
  "rubric_score": 4.5,
  "strengths": ["答案结论明确。"],
  "weaknesses": []
}
```

## 输出文件

每次运行会写入：

```text
runs/<run_id>/
  traces/<case_id>.trace.json
  judge/<case_id>.judge.json
  metrics/<case_id>.metrics.json
  scores.json
  report.md
```

`scores.json` / `report.md` 主要字段：

- `score`：最终归一化分数，范围为 0 到 1。
- `judge_score`：judge 给出的 `rubric_score`，范围为 0 到 5。
- `must_include_pass`：是否覆盖全部必含项。
- `must_include_results`：逐项覆盖判断。
- `token_usage`、`duration_ms`、`tool_calls`：运行指标。

## 运行

安装后运行：

```bash
python3 -m pip install -e .
benchmark-builder --config benchmarks --out runs/codex-run
```

不安装，直接用 `PYTHONPATH`：

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks \
  --out runs/codex-run
```

本地 smoke test，不调用真实模型：

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks \
  --out runs/smoke \
  --agent heuristic \
  --judge heuristic
```

使用 Codex CLI：

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks/example.yaml \
  --out runs/codex-cli \
  --agent codex-cli \
  --judge codex-cli
```

使用 Claude CLI：

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks/example.yaml \
  --out runs/claude-cli \
  --agent claude-cli \
  --judge claude-cli
```

使用 msAgent：

```bash
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks/example.yaml \
  --out runs/msagent-cli \
  --agent msagent-cli \
  --judge msagent-cli \
  --msagent-agent Hermes
```

从源码运行 msAgent 时可以指定 CLI：

```bash
MSAGENT_CLI="uv --project /path/to/msagent run msagent" \
PYTHONPATH=src python3 -m benchmark_builder.run_benchmark \
  --config benchmarks/example.yaml \
  --out runs/msagent-cli \
  --agent msagent-cli \
  --judge msagent-cli
```
