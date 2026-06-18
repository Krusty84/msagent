# HCCL Communication Parameter Reference

This reference provides detailed descriptions, version constraints, and extended configuration examples for HCCL communication parameters. Use it after the main SKILL.md workflow has identified a communication issue that warrants parameter tuning.

## Parameter Priority

When multiple parameters could address a problem, tune in this order:

1. Parameters with the lowest risk and narrowest scope first.
2. Parameters that affect only the target communication group or op type.
3. Parameters that change system-wide communication behavior (last resort).

## Core Parameters

### HCCL_OP_EXPANSION_MODE

Controls where collective communication algorithm expansion (orchestration) occurs.

| Value | Expansion Location | Typical Use Case |
|---|---|---|
| `HOST` | Host CPU | Default mode. Broadly supported but host dispatch overhead is visible for many small communication ops. |
| `AI_CPU` / `AICPU` | Device-side AI CPU | Reduces host dispatch cost. Useful when Host CPU is a bottleneck and AI CPU has spare capacity. |
| `AIV` | Device-side AI Vector Core | Lowest orchestration latency. Suitable for small or medium communication patterns where Vector Core cycles can be spared. |

**When to tune**:
- Host dispatch time dominates communication orchestration cost (visible in timeline as host-side gaps before communication ops).
- Many small collective ops (allReduce, allGather with small payload) show high launch overhead relative to transfer time.
- Host CPU is overloaded while AI CPU or Vector cores are underutilized.

**Risks**:
- `AI_CPU`: may contend with other AICPU operators (e.g. communication helper kernels, some fusion patterns). AI CPU resource contention can degrade both communication orchestration and compute-side AICPU work.
- `AIV`: consumes Vector compute cores, reducing peak compute throughput during communication windows. Has product/version constraints for supported op types.
- Both `AI_CPU` and `AIV` may have limited operator support compared to `HOST`.

**Verification**:
- Compare communication op dispatch-to-start latency before vs after.
- Measure step time, communication time, and overlap ratio.
- Check whether AICPU or Vector core utilization changed materially.

Reference: https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/81RC1beta1/maintenref/envvar/envref_07_0096.html

### HCCL_BUFFSIZE

Sets the HCCL internal communication buffer size in MB.

**Default**: varies by product version; typically 20–200 MB depending on CANN version and device memory capacity.

**When to tune**:
- Large-message collective ops (allReduce, allGather, reduceScatter with payload > 64 MB) show bandwidth below expectation without retry, relay, or lane degradation.
- Multiple concurrent communication streams compete for buffer space.
- The existing buffer size may be too small to fully utilize available link bandwidth.

**How to tune**:
- Increase in moderate steps (e.g. from default to 200, then 500, then 1024 MB).
- Monitor NPU memory usage after each increase.
- Stop increasing when bandwidth stops improving or memory headroom is below safety margin.

**Risks**:
- Directly increases NPU memory consumption proportional to the buffer size multiplied by the number of concurrent communication streams.
- Insufficient memory may cause out-of-memory errors or force the runtime to fall back to smaller buffers.

**Verification**:
- Compare communication bandwidth for large-message ops before vs after.
- Check NPU memory usage via `npu-smi info` before and after the change.

### HCCL_DETERMINISTIC

When set, enables deterministic communication ordering. This can help isolate whether communication timing instability is caused by non-deterministic algorithm selection or ordering.

**Values**:
- Unset (default): non-deterministic ordering allowed.
- Set to any non-empty value: deterministic ordering enforced.

**When to use**:
- Communication timing varies significantly across identical runs without workload changes.
- Debugging or reproducibility is required.
- Ruling out algorithm/ordering jitter as a confounding factor before investigating other causes.

**Risks**:
- May add communication overhead because the runtime cannot choose the fastest available path at each invocation.
- Usually not recommended for production performance.

## Interacting Parameters

These parameters interact with HCCL behavior indirectly and should be checked before tuning HCCL-specific parameters.

### ASCEND_LAUNCH_BLOCKING

When set to `1`, disables the task queue pipeline and forces synchronous operator launch.

**Interaction**: `TASK_QUEUE_ENABLE` is disabled. `HCCL_OP_EXPANSION_MODE=AIV` or `AI_CPU` may not function as expected because the host-side dispatch pipeline is effectively serialized.

### TASK_QUEUE_ENABLE

Controls the operator task queue dispatch pipeline level.

**Values**:
- `1`: Level 1 optimization. Splits operator dispatch into two pipeline stages; aclnn operator calls are placed on the secondary pipeline.
- `2`: Level 2 optimization. Includes Level 1 and further balances primary/secondary pipeline load by moving workspace-related work to the secondary pipeline.

**Interaction with HCCL**: When communication ops are orchestrated on the host (`HCCL_OP_EXPANSION_MODE=HOST`), `TASK_QUEUE_ENABLE` can pipeline the host-side communication orchestration with other operator dispatch work, reducing exposed orchestration cost.

### HCCL_WHITELIST_DISABLE

Disables certain HCCL internal optimization paths.

**When to use**: Only when a specific HCCL internal optimization is suspected of causing incorrect behavior or performance regression, and the issue has been confirmed with HCCL team guidance.

**Risk**: Can significantly degrade communication performance across all ops. Only use for debugging under explicit guidance.

## Configuration Examples

### Scenario 1: Many small allReduce ops with host dispatch bottleneck

```bash
# Move expansion to AI CPU to reduce host dispatch pressure
export HCCL_OP_EXPANSION_MODE=AI_CPU
# Ensure task queue is enabled for pipelining
export TASK_QUEUE_ENABLE=2
```

### Scenario 2: Large-message allGather bandwidth below expectation

```bash
# Increase communication buffer to improve large-message throughput
export HCCL_BUFFSIZE=500
```

### Scenario 3: Communication timing instability debugging

```bash
# Enable deterministic ordering to isolate algorithm jitter
export HCCL_DETERMINISTIC=1
```

## Product/Version Constraints

- `HCCL_OP_EXPANSION_MODE=AIV` support depends on the specific collective op type, data type, and communication domain concurrency. Check the CANN version documentation.
- `HCCL_BUFFSIZE` effective upper bound is limited by available NPU memory divided by the number of concurrent communication streams.
- Parameter names and default values may differ across CANN versions. Always verify the current value before recommending changes.
