# Modeling Simulation Modeling

`Modeling` is the Agent for LLM/VLM simulation modeling. It helps with environment initialization, performance modeling, single-point simulation, throughput planning, device profiling, and serving auto-tuning preparation.

## Start

```bash
msagent --agent Modeling
```

From source:

```bash
uv run msagent --agent Modeling
```

## Recommended Input

- For single-point simulation: model, device profile, device count, input/output length, and prefill/decode mode.
- For throughput planning: model, hardware, device count, SLO, deployment mode, and token length assumptions.
- For serving auto-tuning: inference framework, hardware information, model config, workload tokens, and optimization target.

Example:

```text
Recommend serving auto-tuning parameters for Qwen3-32B on Atlas A3 with vLLM, prioritizing throughput.
```

## Expected Output

Modeling usually returns missing parameter checks, command planning, configuration snippets, recommendation rationale, and validation suggestions. Real performance conclusions should still be verified by actual runs.
