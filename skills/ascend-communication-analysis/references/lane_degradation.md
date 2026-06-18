# Lane Degradation Diagnosis Handbook

Lane degradation (降Lane) occurs when an HCCS physical link between NPU chips operates at fewer lanes than its designed maximum. This reduces bandwidth proportionally and can cause communication bottlenecks that appear as slow ranks or slow links in profiling data.

## Background

HCCS (Huawei Cache Coherence System) links connect NPU chips within a server or across servers. Each link consists of multiple physical lanes. The total bandwidth of a link is approximately proportional to the number of active lanes.

- **Normal state**: all lanes are active, providing full designed bandwidth.
- **Degraded state**: one or more lanes are down, reducing bandwidth proportionally. For example, a 7-lane link degraded to 3 lanes delivers roughly 3/7 of the original bandwidth.

Lane degradation is typically a hardware-level issue caused by:
- Physical connection problems (loose cables, damaged fibers, dust in optical ports).
- Signal integrity degradation over time or under thermal stress.
- Board-level hardware faults.
- Driver or firmware errors that cause lanes to be disabled.

## Detection

### Primary Tool: hccn_tool

`hccn_tool` is the primary tool for checking HCCS link lane status on Ascend NPU devices.

Check lane status for a specific device:

```bash
hccn_tool -i <device_id> -link -g
```

Output interpretation:
- Each link shows a lane count (e.g. `lane_num: 7` for a full-width link on 910B).
- A value lower than the expected maximum indicates degradation.
- Compare lane counts across all devices in the cluster to identify asymmetric degradation.

Check all devices on a node:

```bash
for i in $(seq 0 7); do
  echo "=== NPU $i ==="
  hccn_tool -i $i -link -g
done
```

### Timing: Before vs After the Task

Lane degradation can be:
- **Pre-existing**: present before the task starts. Check baseline lane status before running.
- **In-task**: develops during the task due to thermal stress, signal issues, or hardware faults. Check after the task completes or after a problematic iteration is observed.

Best practice:
1. Run `hccn_tool -i <id> -link -g` on all devices before the job.
2. Run the profiling job.
3. Run `hccn_tool -i <id> -link -g` on all devices after the job.
4. Compare before vs after for any lane count changes.

### Profiler-Based Suspicion

When `hccn_tool` output is not available, profiler evidence can raise suspicion of lane degradation:

| Profiler Signal | Interpretation |
|---|---|
| Stable low bandwidth on a specific rank pair, at a fixed ratio to expected (e.g. ~50%, ~25%, ~14%) | Strong suspicion of lane degradation at 1/2, 1/4, or 1/7 of full lanes. |
| Bandwidth is consistently low across many ops on the same link or rank pair | Suggests a persistent link issue rather than transient congestion. |
| One direction of a bidirectional link is slow while the reverse direction is normal | Asymmetric lane degradation (different lane counts per direction). |
| Bandwidth is stable over time, not fluctuating | Consistent with a fixed lane count rather than congestion or contention. |

Distinguish lane degradation from other causes of low bandwidth:

| Cause | Bandwidth Pattern | Key Differentiator |
|---|---|---|
| Lane degradation | Stable low bandwidth at a fixed ratio | `hccn_tool` confirms reduced lane count. |
| Small-packet communication | Low bandwidth but small transfer size | Check `transit_size` — if payload is small, low bandwidth is expected. |
| Congestion / backpressure | Fluctuating low bandwidth across many ops | `hccn_tool -stat -g` shows congestion counters. Lane count is normal. |
| Retry / relay | Intermittent very high duration ops | `retry` or `relay` counters are non-zero. Lane count is normal. |

## Lane Count Interpretation

### Expected Lane Counts by Product

| Product | Typical Full Lane Count | Notes |
|---|---|---|
| Ascend 910B | 7 lanes per HCCS link | Standard intra-server link width. |
| Ascend 910B (inter-server) | Varies by interconnect topology | Check product specification for expected lane count. |

### Bandwidth Impact

Expected bandwidth scales approximately linearly with lane count:

| Lane Count Ratio | Expected Bandwidth Ratio | Example (7 → N lanes) |
|---|---|---|
| Full (7/7) | 100% | ~21 GB/s RDMA, ~19 GB/s SDMA |
| ~1/2 (3-4/7) | ~43-57% | ~9-12 GB/s |
| ~1/4 (1-2/7) | ~14-29% | ~3-6 GB/s |
| 1/7 (1/7) | ~14% | ~3 GB/s |

Treat these as rough estimates. Actual bandwidth also depends on transfer size, protocol overhead, and measurement method.

## Recovery and Response

### If Lane Degradation Is Confirmed

1. **Report the affected links**: device IDs, link indices, current lane count, expected lane count, and the bandwidth impact ratio.
2. **Determine scope**: is this a single link, multiple links on one device, or multiple devices?
3. **Recovery actions** (in order of increasing invasiveness):
   - **Software reset**: `hccn_tool -i <device_id> -link -reset <link_id>` may recover lanes that were disabled by a transient driver/firmware error. Only attempt on idle devices.
   - **Device reset**: reset the affected NPU device via `npu-smi` if the software link reset does not help.
   - **Node reboot**: if device reset is insufficient, reboot the affected node.
   - **Hardware inspection**: if lanes remain degraded after reboot, inspect physical connections (cables, optical modules, board seating).

### Software Mitigation (If Hardware Recovery Is Not Immediately Possible)

Software cannot restore lost lanes, but communication can be routed to avoid degraded links in some topologies:

- HCCL may automatically detect degraded links and adjust routing. Check whether the current HCCL version supports automatic lane degradation handling.
- If manual intervention is needed, consider excluding the affected device from the communication group or reducing the workload assigned to it.

### Reporting

When reporting lane degradation in the output:

- State which tool confirmed it (`hccn_tool`).
- List affected device IDs, link indices, current lane count, and expected lane count.
- Quantify bandwidth impact: observed bandwidth vs expected bandwidth at full lanes.
- State whether degradation was pre-existing or developed during the task.
- Recommend whether hardware inspection is needed.

## Example Diagnosis

```
Lane degradation confirmed on NPU 3, link 5:
  - hccn_tool reports lane_num=3 (expected 7)
  - Observed SDMA bandwidth on NPU3→NPU6: ~8 GB/s (expected ~19 GB/s for 7 lanes)
  - Bandwidth ratio 8/19 ≈ 42% matches lane ratio 3/7 ≈ 43%
  - Lane count was normal (7) before the task; degradation occurred mid-task.
  - Recommendation: hardware inspection of NPU3 link 5 connection.
```
