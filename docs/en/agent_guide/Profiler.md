# Profiler Performance Tuning

`Profiler` is the Agent for Ascend profiling analysis and performance tuning. It turns profiling data into structured conclusions, root cause analysis, and actionable optimization suggestions.

## Positioning

- Handles single-card, multi-card, and cluster Ascend performance analysis.
- Focuses on profiling data quality, bottleneck localization, and tuning suggestions.
- Fits slow rank, slow node, MFU, communication, operator hotspot, host scheduling, and dispatch issues.

## Start

```bash
msagent --agent Profiler
```

From source:

```bash
uv run msagent --agent Profiler
```

## Prerequisites and Recommended Input

- Prepare Ascend profiling directories, `ascend_pytorch_profiler_*.db`, `kernel_details.csv`, or trace files.
- If `msprof-mcp` is required, make sure the environment can access the profiling data and required tools.
- Provide the symptom, training stage, rank / node scope, and expected output format when possible.

Example:

```text
Analyze /path/to/cluster_profiling/ for slow-rank issues, locate abnormal ranks, and provide likely causes and optimization suggestions.
```

## Expected Output

Profiler usually returns data quality checks, key metrics, bottleneck locations, root cause analysis, and executable optimization suggestions. For CSV / DB export tasks, it should explain generated file paths and fields.

## Notes

- Replace placeholder paths with real local profiling data.
- Without real profiling data, Profiler can provide methodology but cannot prove a concrete bottleneck.
- Conclusions should be grounded in tool output, database queries, and raw logs.
