# Quantizer Model Quantization

`Quantizer` is the Agent for msModelSlim model quantization and accuracy tuning. It orchestrates environment checks, model preparation, configuration generation, quantization execution, and evaluation.

## Positioning

- Handles LLM quantization accuracy tuning with msModelSlim.
- Lets users describe model path, quantization scheme, device, and accuracy target in natural language.
- Can trigger model adaptation when the target model is not yet supported.

## Start

```bash
msagent --agent Quantizer
```

From source:

```bash
uv run msagent --agent Quantizer
```

## Prerequisites and Recommended Input

- Prepare an inference environment, preferably inside a vllm-ascend container when applicable.
- Install msModelSlim and prepare AISBench evaluation service and datasets if end-to-end evaluation is required.
- Provide model path, output path, quantization scheme, device, accuracy target, and `trust_remote_code` decision when needed.

Example:

```text
Quantize path/to/Qwen3-32B with W8A8 on NPU 0, and keep gsm8k accuracy loss within 1% of the baseline.
```

## Expected Output

Quantizer usually returns parameter confirmation, environment check results, quantization configuration, evaluation configuration, iteration history, and final artifact locations. On failure, it should provide the failed command, log location, likely cause, and next action.

## Notes

- Replace placeholder model paths, output paths, and dataset names with real resources.
- Without NPU, msModelSlim, AISBench, or datasets, avoid running the full quantization workflow. Start with parameter review or environment checks.
- If no floating-point baseline is provided, the Agent may need to run baseline evaluation first.
