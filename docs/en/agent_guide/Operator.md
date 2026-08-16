# Operator Tuning

`Operator` is the Agent for Ascend operator performance tuning. It helps analyze operator performance data and produce optimization suggestions for end-to-end operator development workflows.

## Start

```bash
msagent --agent Operator
```

From source:

```bash
uv run msagent --agent Operator
```

## Recommended Input

- Operator source directory and kernel file path.
- Performance report, profiling result, or msprof op data.
- Target hardware, shape, precision, and expected optimization goal.

Example:

```text
Analyze this operator directory end to end and suggest performance optimizations. The kernel source is path/to/kernel.cpp.
```

## Expected Output

Operator usually returns bottleneck analysis, optimization suggestions, supporting evidence, and next validation steps. Replace placeholder paths with real local operator files before running commands.

## Current Boundaries

- Performance conclusions require real profiling data; missing evidence must be marked for validation.
- End-to-end optimization requires an executable operator directory and kernel source. With profiling data only, analyze the evidence before deciding whether to modify code.
- If a user-provided data path or required file is unavailable, stop and ask for confirmation instead of guessing.
