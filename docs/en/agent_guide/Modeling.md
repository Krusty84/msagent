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
Recommend serving auto-tuning parameters for Qwen3-32B on Ascend A3 series products with vLLM, prioritizing throughput.
```

## Expected Output

Modeling usually returns missing parameter checks, command planning, configuration snippets, recommendation rationale, and validation suggestions. Real performance conclusions should still be verified by actual runs.

## Current Boundaries

- Automated workflows are limited to the Skills configured for `Modeling`; unsupported workflows must not be presented as automated.
- Installation, execution, or environment changes require confirmation before they are performed.
- Parameter recommendations may use historical experience or heuristic rules, and the source of each recommendation should be identified.
- Real performance conclusions require actual execution and validation output.
