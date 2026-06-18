---
name: ascend-communication-analysis
description: Analyze Ascend NPU collective communication profiling data with a DB-first workflow. Use when the user provides `cluster_analysis_output/cluster_analysis.db`, rank-level `analysis.db`, rank-level `ascend_pytorch_profiler_{rank_id}.db`, together with `profiler_info.json`, and asks about HCCL or hcom communication cost, collective communication TOP ops, wait time, slow rank/straggler, Notify Wait, bandwidth, retry, relay, SDMA/RDMA/HCCS links, communication matrix, Ascend communication fault patterns, communication-computation overlap/masking analysis, HCCL parameter tuning and configuration, lane degradation (降Lane), or slow link diagnosis.
---

# Ascend Communication Analysis

## Goal

Diagnose Ascend collective communication cost from profiling artifacts. Always separate:

1. **Wait-caused high duration**: the communication op looks slow because this rank waits for another rank or an earlier compute/copy path.
2. **Real communication bottleneck**: transfer, link, scheduling, retry, relay, bandwidth, alignment, or contention is suspicious after wait is ruled out.

This skill focuses on collective communication. P2P analysis is not complete yet; if P2P appears, report it as a limitation and only summarize available evidence.

## Data Scope

Current scope is intentionally limited to DB-based analysis. Use DB inputs in this priority order:

1. `cluster_analysis_output/cluster_analysis.db`
2. rank-level `analysis.db`
3. rank-level `ascend_pytorch_profiler_{rank_id}.db`
4. sibling `profiler_info.json`

Optional context:

- `profiler_metadata.json`: use for `world_size`, `tensor_model_parallel_size`, `pipeline_model_parallel_size`, `data_parallel_size`, and `parallel_group_info`

Do not treat JSON summary files, trace files, or non-DB artifacts as primary inputs in the current version of the skill. If none of the required DB files are present, state that the current skill scope does not cover the dataset yet.

If cluster-level analysis is requested but `cluster_analysis_output/cluster_analysis.db` is absent, first try to generate it by running:

```bash
msprof-analyze cluster -m all -d <cluster-data-path>
```

Here `<cluster-data-path>` is the cluster profiling dataset root provided by the user. After the command completes, re-check for `cluster_analysis_output/cluster_analysis.db` and continue with the DB-first workflow if generation succeeds.

Interpret the DB roles like this:

- `cluster_analysis_output/cluster_analysis.db`: first-priority source for cluster-wide communication diagnosis
- rank-level `analysis.db`: first-priority source for single-card communication diagnosis
- rank-level `ascend_pytorch_profiler_{rank_id}.db`: raw profiler DB used when communication task evidence is needed, especially for `level1+`

### `cluster_analysis.db` Tables

Common cluster-analysis tables:

| Table | Role | Use in this skill                                                                                                                         |
|---|---|-------------------------------------------------------------------------------------------------------------------------------------------|
| `CommunicationGroupMapping` | Communication group/domain mapping table. It maps group hash / `group_name` to group metadata such as `pg_name`, `group_id`, group `type`, and `rank_set`. | First choice for resolving communication domains. Use it before treating `ClusterCommunicationTime.group_name` as a final domain label.   |
| `ClusterCommunicationTime` | Cluster-wide communication large-op timing table. It records op name, group name, rank, step, elapsed time, wait time, transit time, and start timestamp. | Overview uses only op/domain/type/count and elapsed-time fields. Wait/transit fields are reserved for later deep-dive analysis.           |
| `ClusterCommunicationBandwidth` | Cluster-wide communication bandwidth table. It records op name, group name, rank, step, link/band type, transit size, transit time, bandwidth, and large-packet ratio. | Use later only for selected candidate ops when checking real communication bottlenecks.                                                   |
| `ClusterCommunicationMatrix` | Cluster-wide communication matrix table. It records source/destination rank pairs, communication group, transport/link type, transit size/time, and bandwidth. | Use after candidate selection to inspect link-level route, rank-pair imbalance, local/global rank interpretation, and SDMA/RDMA evidence. |


## Collection Level Rules

Determine collection level from `profiler_info.json`, using its `profiler_level`-related field(s) as the source of truth.

- **Level1 and above**: communication small-task information is expected to exist in the DB, so `COMMUNICATION_TASK_INFO`-based analysis is allowed. This includes Notify Wait, small communication task decomposition, transfer size, transport details, peer rank fields, and richer wait evidence.
- **Level0**: `COMMUNICATION_TASK_INFO` can also exist and may be used for small-task names and timing evidence, including Notify Wait timing when present. However, level0 does not provide reliable `transit_size`, `link_type` / `transport_type`, `src_rank`, or `dst_rank` detail for small tasks; these fields may be filled with DB default values. Treat such default values as unknown, not measured facts. In level0, do not claim detailed transport breakdown, communication matrix evidence, or peer-rank direction from small-task fields unless a non-default value is explicitly present and corroborated by another DB source.
- Always report the profiler level in the final diagnosis.
- Prefer delivered aggregate DB fields such as `transit_size`, `bandwidth`, `transit_time`, and `wait_time`. Do not recompute them from inferred small-task relationships unless the DB already exposes the needed fields.

## Concepts

- `COMMUNICATION_OP`: communication large op.
- `COMMUNICATION_TASK_INFO`: communication small task, including operations such as `Notify Wait`, `Notify Record`, `Memcpy`, `Reduce Inline`, RDMA send payload/notify, and related synchronization or transfer tasks. This table may exist even at profiler level0; at level0, size/link/peer columns can be DB defaults rather than collected values.
- `LINK_TYPE` / `transport_type`: transfer link type such as `LOCAL`, `SDMA`, `RDMA`, `HCCS`, or tool-specific labels.
- Communication op name pattern: `{type}__{communication-domain-hash-suffix}_{index-in-domain}_{0/1}`. Example: `hcom_allGather__318_0_1`.
- Rank IDs in rank-level matrix files can be communication-domain local ranks. Cluster analysis may map them to global ranks. Be explicit about local vs global rank evidence.

View communication in two independent layers:

| Layer | Meaning                                                                                               | Evidence                                                                                              |
|---|-------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------|
| Orchestration / expansion layer | where the communication algorithm is expanded and scheduled: `HOST`, `HOST_ts`, `AI_CPU`, or `AIV`| `HCCL_OP_EXPANSION_MODE` environment variable; Device type, communication operator types also matters |
| Data movement layer | which engine moves payload data: `SDMA`, `RDMA`, HCCS-related DMA path, or MTE data movement | link matrix, transit size/time, bandwidth, retry, relay                                               |

Do not collapse these layers into one "communication mode". For example, a communication op can be orchestrated by Host or AICPU while payload movement still uses SDMA or RDMA. Analyze orchestration overhead and data movement bottlenecks separately.

Orchestration / expansion modes:

| Mode | Typical scene                                                                          | Strength | Risk / constraint |
|---|----------------------------------------------------------------------------------------|---|---|
| HOST | default Host-side CPU expansion and dispatch                                           | broadly supported and easy to reason about from Host timelines | Host launch/dispatch overhead can be visible for many small communication ops |
| HOST_ts | Host-side expansion with TS-assisted dispatch path when it appears in trace/tool labels | similar goal to AICPU for reducing Host-side issue overhead | naming and availability are tool/version dependent; verify from trace labels or environment evidence |
| AI_CPU / AICPU | Device-side AI CPU expansion and scheduling                                            | saves Host dispatch time and is useful when Host launch overhead is material | AI CPU resource contention is possible; product/operator support and profiling constraints depend on CANN/product version |
| AIV | Device-side AI Vector Core expansion / execution path                                  | low latency for supported small or medium communication patterns | consumes vector compute cores; support is constrained by product, operator, data type, and communication-domain concurrency |

Data movement engines:

| Mode | Typical scene                                                                                                         | Strength | Risk |
|---|-----------------------------------------------------------------------------------------------------------------------|---|---|
| MTE | in-server small or medium communication, especially useful for MoE combine/dispatch; usually with AIV expansion mode. | low latency and high short-duration bandwidth | consumes vector compute cores |
| SDMA | in-server or cross-chip large data over HCCS                                                                          | dedicated transfer engine, does not consume compute cores | higher startup latency than AIV |
| RDMA | cross-server communication                                                                                            | remote direct memory access | network/topology/congestion sensitive |

Reference: `HCCL_OP_EXPANSION_MODE` configures communication algorithm expansion location, with documented values such as `HOST`, `AI_CPU`, and `AIV`: https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/81RC1beta1/maintenref/envvar/envref_07_0096.html

Reference bandwidth anchors from current expert notes:

| Link/mode | Data size condition | Reasonable bandwidth |
|---|---:|---:|
| cross-chip SDMA memcpy | `transit_size > 16 MB` | about `19 GB/s` |
| cross-chip SDMA inline-reduce | `transit_size > 16 MB` | about `17 GB/s` |
| cross-chip/cross-node RDMA | `transit_size > 1 MB` | about `21 GB/s` |

Treat these as heuristics, not hard pass/fail rules. Small packets, overlap, topology, and measurement method can lower apparent bandwidth.

## DB Access Policy

Use DB artifacts as the source of truth. For any SQL query or schema inspection, use `msprof_mcp-execute_sql` first.

Do not invoke the local `sqlite3` CLI, Python `sqlite3`, or shell commands that open DB files for ad hoc querying when `msprof_mcp-execute_sql` is available. Only fall back to local DB querying if `msprof_mcp-execute_sql` is unavailable, cannot access the target DB, or fails on the required query.


## Script Tools

Use bundled scripts for deterministic extraction when they cover the needed evidence. For fields not covered by scripts, use `msprof_mcp-execute_sql`; do not use local SQLite querying as the normal path.
When `cluster_analysis_output/cluster_analysis.db` is missing but cluster-level analysis is needed, prefer invoking the official CLI directly before falling back to rank-level-only evidence:

```bash
msprof-analyze cluster -m all -d <profiling-root>
```

If command options, environment setup, or troubleshooting for `msprof-analyze` are needed, use the dedicated `msprof-analyze-cli` skill.

```bash
python3 skills/ascend-communication-analysis/scripts/<script>.py ...
```

Scripts:

| Command | Purpose                                                                                                                                                                                                                                                                                                    |
|---|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `summarize.py --db <cluster_analysis.db> --output-stats-csv <communication_stats.csv> --output-links-csv <group_link_overview.csv>` | Summarize communication-group op time and group-level link statistics. The script owns its extraction logic and should be treated as the preferred overview path. It does not select deep-dive ops or diagnose faults.                                                                                                                                  |
| `collect_wait_evidence.py --db <cluster_analysis.db> --op-name <selected_op_name> [--op-name <selected_op_name> ...] --output-csv <timing_evidence.csv>` | Collect cross-rank timing evidence for the selected communication op(s), including longest, shortest, earliest-start, and latest-start ranks. Use this as the primary table for wait diagnosis.                                                                                                            |

The script emits JSON to stdout by default and Markdown with `--format md`. Save only the CSV artifacts unless the user explicitly asks for a JSON file.

## Workflow

During the workflow, surface key intermediate results to the user as soon as they are available. Print or summarize important findings from overview generation, candidate selection, wait gating, and transfer-evidence collection instead of keeping them only for the final report.

1. **Locate required DB inputs**: Search the user-provided path in this order: cluster-level `cluster_analysis_output/cluster_analysis.db`, rank-level `analysis.db`, rank-level `ascend_pytorch_profiler_{rank_id}.db`, and `profiler_info.json`.
   - If cluster-wide diagnosis is requested and `cluster_analysis_output/cluster_analysis.db` is missing, first run `msprof-analyze cluster -m all -d <cluster-data-path>` to generate it when the environment supports the CLI. Use the user-provided cluster dataset root as `<cluster-data-path>`, then re-scan for the generated DB before downgrading to rank-level evidence.
2. **Choose analysis entry DB**:
   - cluster communication: prefer `cluster_analysis_output/cluster_analysis.db`
   - single-card communication: prefer rank-level `analysis.db`
   - raw fallback / small-task evidence: use rank-level `ascend_pytorch_profiler_{rank_id}.db`
3. **Read profiler level**: Parse `profiler_info.json` and determine whether the dataset is `level0` or `level1+`.
4. **Set analysis mode**:
   - `level1+`: allow large-op and small-task communication analysis, including size/link/peer fields when present
   - `level0`: allow large-op analysis and small-task timing/name analysis, but treat small-task `transit_size`, `link_type` / `transport_type`, `src_rank`, and `dst_rank` as unknown when they carry DB defaults
5. **Build overview summaries**: Use `summarize.py` when its input DB schema matches. The goal is to get two overview tables before selecting deep-dive candidates:
   - `communication_stats`: communication group + op type elapsed-time distribution. Use `communication_stats` to find high-cost groups/op types.
   - `group_link_overview`: communication group + link type transit size/time/bandwidth distribution from `ClusterCommunicationMatrix`. Use `group_link_overview` to see whether link time is concentrated in specific groups or transport types.
   - Prefer `CommunicationGroupMapping` to resolve communication groups. Do not assume `ClusterCommunicationTime.group_name` or `op_suffix` alone is the final label; it may be a group hash that must be mapped to `pg_name`, `group_id`, group type, or rank set.
   - Do not treat low `min_bandwidth` alone as a fault signal; it can be caused by small packets or local aggregate records.
   - If summarize.py fails or the schema differs, do not stop. Use `msprof_mcp-execute_sql` to inspect table and column names, then adapt the script's extraction intent into narrowly scoped queries that produce the same style of overview.
6. **Select concrete communication instances for deep analysis**: 
   - Selects concrete high-cost collective communication instances from high-cost groups. In practice this usually means selecting a few concrete `op_name` values, optionally restricted to a target step. 
   - Exclude aggregate rows such as `Total Op Info`; they are overview rows, not analyzable communication ops. 
   - Report selection reasons directly in the final narrative.
7. **Collect wait/timing evidence first**: Use `collect_wait_evidence.py` only for the selected candidates. 
   - Pass selected concrete op names through `--op-name`; repeat `--op-name` in the same command when multiple selected ops should be analyzed together.
   - Use `--step-id` when needed to limit the analysis to a specific step.
   - The agent decides from the evidence whether high duration is wait-caused, straggler-related, or insufficient evidence.

Example:
```bash
python3 skills/ascend-communication-analysis/scripts/collect_wait_evidence.py \
  --db cluster_analysis_output/cluster_analysis.db \
  --op-name hcom_allReduce__123_0_1 \
  --op-name hcom_allGather__318_0_1 \
  --op-name hcom_reduceScatter__456_0_1 \
  --output-csv timing_evidence.csv
```

8. **Gate on wait diagnosis result**: After Step 7, classify each selected communication instance as `wait-caused`, `real-transfer-slow-suspected`, or `insufficient-evidence`. If the evidence clearly shows the long duration is caused by inter-rank waiting, stop the diagnosis for that instance and do not continue into retry, relay, bandwidth, or fault-mode checks.
9. **Collect transfer evidence only for non-wait cases**: For each `real-transfer-slow-suspected` instance, choose the concrete communication op on the rank with the longest duration. Use `msprof_mcp-execute_sql` on `ClusterCommunicationBandwidth`, rank-level `analysis.db`, and when needed `ClusterCommunicationMatrix` to collect transfer size, transit time, bandwidth, link/band type, and rank-pair imbalance. Read the corresponding row in rank-level `ascend_pytorch_profiler_{rank_id}.db` `COMMUNICATION_OP` for `relay` and `retry`.
10. **Compare peer and baseline behavior**: For each transfer-slow case, compare the longest rank with peer ranks in the same communication instance, and compare against same communication type / similar transfer size / same communication group or link type when available. Optional checks include whether memory-intensive compute overlaps the same time window on that rank, whether a no-overlap same-shape communication instance provides a baseline, and whether the corresponding communication small tasks show abnormal decomposition or timing.
11. **Produce evidence-backed gated diagnosis**: For `wait-caused` instances, report only the wait chain and supporting timing fields. For `real-transfer-slow-suspected` instances, map the collected transfer evidence to the handbook fault modes when possible. For `insufficient-evidence`, state the missing DB tables, fields, or comparison samples. Every conclusion must cite the DB table / DB field / script output field that supports it, and separate overview facts from candidate-selection reasoning and later deep-dive conclusions.


## Wait Diagnosis

Use two methods when possible:

| Method | Evidence | Interpretation |
|---|---|---|
| Small-task Notify Wait | Large `Notify Wait` on a plane; `src_rank` / `local_rank` recorded in task args or columns | Valid as timing evidence when `COMMUNICATION_TASK_INFO` exists. Peer-rank interpretation requires non-default peer fields, usually `level1+`; at level0, report the wait timing but do not infer `src_rank` / `dst_rank` from DB defaults. Map local rank to global rank only when group mapping is available. |
| Cross-rank same-op alignment | Same op name/domain across ranks should finish close together; ranks that start early and spend longer may be waiting for ranks that start late or finish late | This method assumes the selected records are the same collective communication instance and that end-time differences for the same communication in this communication group are relatively stable. If end times are unstable or not aligned, do not infer wait from start/duration skew alone. |

When reporting wait, name:

- waiting rank(s)
- suspected waited/late rank(s)
- wait duration or skew
- whether evidence uses global rank or local communication-domain rank
- confidence and missing evidence
- TP/PP/DP context from `profiler_metadata.json`
- the key ranks from `timing_evidence.csv`: `longest_rank`, `shortest_rank`, `earliest_start_rank`, and `latest_start_rank`

## Communication-Computation Overlap Analysis

When communication cost appears high in the step breakdown, first determine whether the communication is effectively overlapped (masked) by concurrent computation. Unoverlapped communication is exposed to the critical path; well-overlapped communication may look expensive in isolation but has low or zero net impact on step time.

### Overlap Detection

Use these evidence sources in priority order:

| Method | Evidence | Interpretation |
|---|---|---|
| Step-level overlap ratio | `StepTraceTime` or `step_trace_time.csv` shows `Communication Time` and `Communication Overlap Time` | If `Communication Overlap Time / Communication Time > 0.8`, most communication is already hidden. If `< 0.3`, most communication is exposed. |
| Timeline visual inspection | Profiler trace or dispatch timeline around communication ops | Look for compute kernels running concurrently with communication tasks on different streams. A communication op that starts and ends without any concurrent compute is fully exposed. |
| Communication group overlap breakdown | Per-communication-group overlap ratio when available | A specific communication group (e.g. TP allGather) may be well-overlapped while another (e.g. DP allReduce) is fully exposed. |

### Diagnosing Poor Overlap

When overlap is low, classify the root cause before recommending tuning actions:

1. **No compute to overlap with**: The step structure places communication in a region with no independent compute work. Common in gradient synchronization after backward completes, or in allReduce-heavy data-parallel steps where the communication phase is separated from compute.
   - Evidence: timeline shows communication in a dedicated sync phase with no concurrent kernels.
   - Action: consider communication-computation scheduling strategies (e.g. gradient bucketing to overlap allReduce with backward compute), or reorder the computation graph to interleave communication with independent compute.

2. **Communication stream blocks compute stream**: Communication and compute share the same device stream or a synchronization point serializes them.
   - Evidence: communication ops appear on the same stream as compute kernels, or explicit wait/sync events separate them.
   - Action: check whether communication is launched on a dedicated HCCL stream; verify no unnecessary `aclrtSynchronizeStream` or `torch.cuda.synchronize` equivalents between compute and communication.

3. **Compute window too short to hide communication**: The compute kernels available during the communication window finish before communication completes, leaving a tail of exposed communication time.
   - Evidence: timeline shows compute ending earlier than communication on the overlapping stream, leaving residual communication time exposed.
   - Action: increase compute granularity (larger micro-batches, fused operators), or reduce communication cost so it fits within the compute window.

4. **Compute-communication bandwidth contention**: Overlap is attempted but both suffer because they compete for shared resources such as memory bandwidth, MTE engines, or HCCS links.
   - Evidence: communication bandwidth drops relative to the no-overlap baseline, and overlapping compute kernels show elevated `mte2_ratio` or memory pressure.
   - Action: test whether disable-overlap actually improves end-to-end step time; if so, the current overlap strategy is counterproductive. Consider reducing compute memory intensity during communication windows, or isolating communication to a subset of links.

### Overlap Effectiveness Judgment

For each communication group with material cost:

1. Compute `exposed_time = total_communication_time - overlap_time`.
2. If `exposed_time / step_time > 5%`, this communication group is a candidate for overlap improvement.
3. Rank candidates by exposed time, not total communication time.
4. For each candidate, check which of the four root causes above applies.

Report overlap findings as:

- Communication group / op type.
- Overlap ratio.
- Exposed time and share of step time.
- Root cause classification.
- Recommended action with expected improvement bound.

## Fault-Mode Handbook

### Hardware-related modes

| Fault mode | Typical symptoms | Key evidence | How to distinguish it |
|---|---|---|---|
| Communication retry | Communication op duration is abnormally high, often with a single op or a few ops suddenly much slower than peer iterations; for large ops this often appears as communication-op duration above about `4 s`. | In rank-level `ascend_pytorch_profiler_{rank_id}.db`, the corresponding row in `COMMUNICATION_OP` has a non-zero `retry`-like metric. The same op may also show poor bandwidth or elongated transfer time. | Retry is stronger than generic low bandwidth because it has an explicit retry counter. If `hccn_tool -i <rank_id> -stat -g` shows retry-related counters increased before vs after the job, that is direct supporting evidence. If all counters stay `0`, do not claim historical retry on that rank. |
| Communication relay | Communication path is longer than expected and bandwidth may be lower than the normal direct path; some ranks may consistently look slower on the same route. | In rank-level `ascend_pytorch_profiler_{rank_id}.db`, the corresponding row in `COMMUNICATION_OP` has a non-zero `relay`-like metric. Matrix or link evidence may also indicate a non-direct route. | Prefer the explicit `relay` indicator over inferring from bandwidth alone. If `hccn_tool -i <rank_id> -stat -g` relay-related counters increased during the AI task, treat that as strong corroboration. |
| Communication congestion / backpressure | A large number of communication ops have non-small transfer size, but the measured bandwidth is persistently far below the heuristic anchors. The symptom may span multiple ops or ranks instead of a single isolated op. | Profiler-side evidence is: transfer size is not small, measured bandwidth is far below expectation, and stronger explanations such as retry, relay, or small-packet mode are absent. External evidence should come from `hccn_tool -i <rank_id> -stat -g`. | When many non-small communication ops all show abnormally low bandwidth, treat it as an important fault signal even before external confirmation. Still do not finalize congestion/backpressure from profiler alone; use `hccn_tool` counters before/after the job as stronger confirmation. If all returned counters are `0`, that rank has no direct historical fault evidence from this tool. |
| Lane degradation (降Lane) | Communication bandwidth is consistently below expectation across many ops on a specific link or rank pair, often at a fixed ratio to the expected bandwidth (e.g. roughly 1/2, 1/4, or 1/8 of normal). The degradation is stable rather than fluctuating. | Use `hccn_tool -i <rank_id> -link -g` to check the current lane count for each link. Normal HCCS links typically operate at full lane width (e.g. 7 lanes for 910B). A link with reduced lanes (e.g. degraded from 7 to 3 or 1 lane) directly explains the proportional bandwidth drop. In profiler data, stable low bandwidth on a specific rank pair or link direction that cannot be explained by small packets, retry, or relay is suspicious. | Lane degradation is a hardware-level issue, not a software tuning problem. Use `hccn_tool` lane status as the primary diagnostic tool. Before the task: check baseline lane status on all ranks. After the task: check whether any link degraded during the run. If lane degradation is confirmed, report the affected rank pair, link direction, current vs expected lane count, and the bandwidth impact. Recovery usually requires hardware intervention or a board reset; software tuning cannot compensate for lost lanes. |

### Non-hardware modes

| Fault mode | Typical symptoms | Key evidence | How to distinguish it |
|---|---|---|---|
| Small-packet communication | Communication time is dominated by setup/build-link overhead and apparent bandwidth is very low even though the absolute payload is small. | Transfer size is small: typically `SDMA < 16 MB` or `RDMA < 1 MB`. Low bandwidth here is not enough by itself to call it faulty, because these sizes naturally underutilize the link. | Before calling it a bottleneck, first confirm the payload is actually small. For small packets, low bandwidth is expected behavior and should usually be reported as a communication-pattern issue rather than a hardware fault. |
| Compute-communication bandwidth contention | Communication bandwidth drops relative to expectation, but not catastrophically; a rough pattern is that bandwidth may fall by about `1x` to `2x` compared with a no-overlap baseline. This often appears when communication overlaps with memory-bandwidth-heavy compute. | Low or reduced communication bandwidth coexists with overlapping compute such as `MatMul`, `FA`, or other memory-intensive operators that are likely `mte bound`. Trace/timeline overlap or step design shows compute and communication running concurrently. | This mode should be preferred when there is overlap with memory-intensive compute, bandwidth is degraded but not extremely abnormal, and there is no retry/relay/alignment evidence. Compare overlapped and non-overlapped performance if possible, then judge whether overlap still provides net benefit. |
| Suspected misaligned communication address | Communication bandwidth is extremely low even though transfer size is not small, especially for node-internal `SDMA`, often in ZeRO-like scenarios. | Treat it as a suspicion item when transfer size is not small, measured bandwidth is extremely low, and the transfer `size` cannot be evenly divided by `512`. | Because address fields are usually unavailable, do not describe this as confirmed address misalignment. Use it as a ranked suspicion only after excluding the small-packet case, and state clearly that the evidence is `size % 512 != 0` plus abnormal low bandwidth rather than a directly observed address. |

### Communication mode reasonableness

Besides fault classification, judge whether the current communication mode itself looks reasonable.

| Question | Reasonable pattern | Suspicious pattern |
|---|---|---|
| Is the communication mode consistent with payload scale? | Small packets using Host/AICPU/AIV orchestration can be acceptable when latency is the real goal; large packets rely on sustained transfer engines. | A mode optimized for latency is used on large payloads and produces obviously poor sustained bandwidth, or a large-transfer mode is repeatedly paying setup cost on tiny packets. |

When the communication mode is not ideal but not faulty, say so explicitly. For example: `current mode is functional but not optimal for this payload size` or `overlap is causing moderate bandwidth contention but may still be a net win`.

## Communication Parameter Configuration

When communication issues are detected, parameter tuning may be needed. This section provides evidence-driven guidance for HCCL communication parameters. Do not recommend parameter changes without first establishing evidence of the corresponding problem.

Parameter reference details are maintained in `references/hccl_params.md`.

### Core HCCL Parameters

| Parameter | Role | When to Consider |
|---|---|---|
| `HCCL_OP_EXPANSION_MODE` | Controls where collective communication algorithms are expanded: `HOST`, `AI_CPU`, or `AIV`. | Host dispatch is on the critical path; small or medium communication ops have visible orchestration overhead; Host CPU is overloaded while AI CPU or Vector cores are underutilized. |
| `HCCL_BUFFSIZE` | Sets the HCCL communication buffer size in MB. Default varies by product version. | Large-message allReduce or allGather shows lower bandwidth than expected; buffer size may limit in-flight data. Increasing can improve bandwidth for large transfers at the cost of NPU memory. |
| `HCCL_ALGO` | Selects the collective communication algorithm for a given op type and size range. | A specific collective op shows bandwidth or latency that is inconsistent with the expected algorithm behavior. Algorithm selection is usually automatic and tuning is rarely needed. |
| `HCCL_DETERMINISTIC` | Enables deterministic communication ordering when set. | Reproducibility is required, or communication timing instability across runs must be ruled out as a confounding factor. Note: may add overhead. |

### Environment Verification

Before tuning, always check what is currently configured. Key environment variables that interact with HCCL behavior:

| Variable | Effect | Interaction |
|---|---|---|
| `ASCEND_LAUNCH_BLOCKING=1` | Disables task queue and forces synchronous launch. | When set, `TASK_QUEUE_ENABLE` is disabled and `HCCL_OP_EXPANSION_MODE=AIV` may not function as expected. |
| `TASK_QUEUE_ENABLE` | Controls operator task queue pipeline level (1 or 2). | Improves host dispatch overlap, which can indirectly reduce communication orchestration overhead on the host. |
| `HCCL_WHITELIST_DISABLE` | Disables certain HCCL internal optimizations when set. | Used for debugging or when a specific optimization path is suspected of causing issues. |

### Tuning Workflow

1. **Identify the specific communication problem** using the evidence workflow (Steps 1–10).
2. **Classify the problem** as:
   - **Orchestration overhead**: host/AICPU expansion cost is high → consider `HCCL_OP_EXPANSION_MODE` change.
   - **Transfer bandwidth below expectation**: payload movement is slow without retry/relay/lane issues → consider `HCCL_BUFFSIZE` or check for small-packet communication.
   - **Unstable or irreproducible timing**: → consider `HCCL_DETERMINISTIC` to rule out ordering variation.
3. **Propose one parameter change at a time**, with:
   - Current value (or default assumption if unset).
   - Proposed new value.
   - Expected improvement mechanism.
   - Risk (e.g. memory increase, compatibility, other regressions).
   - Verification method and metric.
4. **After any parameter change**: re-profile with the same workload, same step range, and same profiler level. Compare the targeted communication metric before vs after.

### Parameter Change Risk Levels

| Risk Level | Parameters | Notes |
|---|---|---|
| Low | `HCCL_BUFFSIZE` (moderate increase) | Increases NPU memory usage. Test memory headroom first. |
| Medium | `HCCL_OP_EXPANSION_MODE` | Changes communication orchestration path. Verify on a single rank first. Some modes have product/version constraints. |
| High | `HCCL_ALGO`, `HCCL_WHITELIST_DISABLE` | Can degrade performance across all communication ops. Only change with explicit documentation support. |

See `references/hccl_params.md` for detailed parameter descriptions, version constraints, and extended configuration examples.

## Output Artifacts

The analysis should leave a small number of machine-readable artifacts in the chosen output directory:

- `communication_stats.csv`: overview statistics by communication group fields and `op_type`, including op count, total elapsed time, elapsed-time share, average elapsed time, and max elapsed time.
- `group_link_overview.csv`: group-level link statistics by communication group fields and `transport_type`, including row count, op count, rank-pair count, transit size/time, bandwidth, and low-bandwidth row count.
- `timing_evidence.csv`: main deep-dive table for selected candidates, including rank skew fields and key-rank roles.

Prefer a concise final report plus these few high-value artifacts over many intermediate CSV files.

## Output Contract

Return a concise report with these sections:

1. **Data Coverage**: artifacts used, inferred collection level, number of ranks, whether cluster analysis output exists, and parallel strategy from `profiler_metadata.json`.
2. **Communication Group Overview**: overview table by communication group fields and op type. Include op count, elapsed time share, total elapsed time, average elapsed time, and max elapsed time.
3. **Communication Link Overview**: overview table by communication group fields and `transport_type`. Include transit size/time, average bandwidth, rank-pair count, and low-bandwidth row count.
4. **Selected Ops For Deep Dive**: concise narrative or table of concrete high-cost ops selected by the agent. Include communication group hash / group id / pg name / group type, op type/name, total/max elapsed time, and `selection_reason`. Explicitly mention how many selected ops were analyzed and why that count was chosen.
5. **Wait Or Transfer Slow**: for each selected op, say `wait-caused`, `real-transfer-slow-suspected`, or `insufficient-evidence`. Always mention the key ranks: longest, shortest, earliest-start, and latest-start. If the case is `wait-caused`, stop there and do not add retry, relay, bandwidth, or fault-mode conclusions for that op.
6. **Communication-Computation Overlap**: for each communication group with material cost, report the overlap ratio, exposed time, root cause classification (no compute to overlap, stream serialization, compute window too short, bandwidth contention), and recommended action.
7. **Fault Mode Identification**: only for `real-transfer-slow-suspected` cases. Map the remaining evidence to one of the handbook fault modes when possible. Include the longest rank's transfer size, bandwidth, `relay`, `retry`, observed symptom, key evidence, contradictory or missing evidence, profiler level applicability, and whether the communication mode looks reasonable.
8. **Lane Status**: if lane degradation is suspected from bandwidth evidence, report whether `hccn_tool` lane status check has been performed, the current vs expected lane count per link, affected rank pairs, and bandwidth impact ratio. If lane check has not been performed, list it as a recommended next check.
9. **Parameter Recommendations**: if communication parameter tuning is indicated by the evidence, propose one parameter change at a time with current value, proposed value, mechanism, risk, and verification method.
10. **Output Artifacts**: list generated CSV paths, and keep the artifact list short and intentional.
11. **Next Checks**: only list checks that are directly motivated by selected-op evidence. Include lane status check via `hccn_tool` when bandwidth evidence suggests possible lane degradation.

Use certainty language carefully:

- Use factual wording for measured fields.
- Use `likely` only when at least two evidence sources agree.
- Use `possible` for one-source heuristics.
- Use `insufficient evidence` instead of guessing.

## Auxiliary Files

- `scripts/summarize.py`: communication op statistics and group-level link overview extraction.
- `scripts/collect_wait_evidence.py`: cross-rank timing evidence collection for selected communication ops.
- `scripts/utils.py`: shared utilities for DB access, CSV writing, and data formatting.
- `references/hccl_params.md`: detailed HCCL parameter descriptions, version constraints, and extended configuration examples.
- `references/lane_degradation.md`: lane degradation diagnosis handbook with `hccn_tool` usage, lane count interpretation, and recovery guidance.
