---
name: mindstudio-storage-analysis
description: "Diagnose storage and Host IO bottlenecks on Ascend NPU training or inference nodes. Use for slow DataLoader/dataset/checkpoint reads, suspected data-starvation idle gaps, iostat/await abnormalities, NFS RTT/execute/retrans issues, small-file metadata overhead, or rank/worker IO contention. Do not use when the established primary issue is CPU decode with idle disks, allreduce communication, operator-internal mte2, or Host launch/scheduling while data loading is normal; route those to the named specialist skill. Uses deterministic same-window evidence and safe, reversible optimization previews."
---

# MindStudio Storage Analysis

Diagnose whether storage or Host IO slows an Ascend workload, then determine separately whether the observed pressure propagates to device-side idle time.

Only conclude from observed evidence. Identify missing evidence explicitly, cap confidence when timestamps or provenance are incomplete, and never execute state-changing tuning automatically.

## Operating contract

- Build a Host IO pressure chain from R100-R400 evidence.
- Build an independent device-side conduction chain from profiler or controlled-experiment evidence.
- Bind dynamic evidence to the target PID/path and a valid collection window.
- Prefer the deterministic collector and analyzer over ad hoc threshold reasoning.
- Treat provider failures as partial evidence, never as a healthy result.

Cover local disk throughput, IOPS, queue and await pressure; NFS latency and retransmission; remote metadata and small-file overhead; multi-process IO interference; and Host-IO-to-device-idle triage.

### Non-negotiable safety gate

When a user asks to run `remount`, `drop_caches`, `blockdev --setra`, or another state-changing tuning command, stop before all tool use. The first reply must state **"not executing now"** and include a complete preview with target, proposed action, risks, blast radius, current-value capture, rollback, and validation. If a target or value is unknown, show a conditional command with `<placeholders>` and request the missing values; never postpone the preview. The user's initial "直接执行" request is not confirmation; require a separate affirmative reply after the preview. Never execute `drop_caches` through this Skill under any confirmation because its host-wide cache eviction is irreversible.

For remount and readahead, use this first-response structure without omitting a section: `Status: not executing now`; `Required inputs`; `Preview command`; `Risks`; `Rollback command restoring the captured original`; `Validation`; `Separate confirmation required`. For `drop_caches`, replace the command preview with `Declined: this Skill never executes it`, state that no rollback exists, and offer read-only observation.

中文请求必须使用下面的中文模板；保留每个标签，仅替换已知值，未知值保留占位符。发送前逐项自检，不得缩写、翻译或改成“稍后提供”。

```text
remount 首答
状态：现在不执行。
所需输入：<挂载点>、只读采集的当前挂载参数、维护窗口。
预览命令：mount -o remount,<当前参数>,noatime <挂载点>
风险：业务中断或挂起、参数不兼容、影响在途 IO；不修改 /etc/fstab。
回滚：mount -o remount,<原始参数> <挂载点>；回滚本身也可能中断 IO。
验证：核对挂载参数、业务读写、RTT/execute/retrans 和 workload 延迟。
单独确认：完成本预览并只读记录原值后，仍需用户再次明确确认。

drop_caches 首答
状态：现在不执行。
拒绝：本 Skill 在任何确认下都不执行 drop_caches。
风险：不可逆地驱逐全机 page cache/dentry/inode，造成后端 IO 和延迟尖峰；不会释放磁盘空间。
回滚：不存在；重新预热只是缓解，不是回滚。
替代：先只读采集内存、缓存和 IO 证据。

readahead 首答
状态：现在不执行。
所需输入：<块设备>、blockdev --getra 读取的当前值、访问模式、以 512-byte sector 表示的建议值。
预览命令：blockdev --setra <建议 sectors> <块设备>
风险：对随机 IO 可能浪费 page cache 和后端带宽。
回滚：blockdev --setra <原始 sectors> <块设备>
验证：同 workload 对比前后吞吐、await、队列和缓存占用。
单独确认：完成本预览并只读记录原值后，仍需用户再次明确确认。
```

Respect these boundaries:

- Automatic network-storage performance confirmation currently covers NFS only and requires current-window RTT, execute latency, retransmission, timeout, or throughput evidence; mount type alone never confirms a bottleneck.
- Lustre, CIFS, GPFS, BeeGFS, Ceph, and FUSE mounts are identified and routed to provider-specific manual evidence collection unless dedicated metrics are supplied.
- High `mte2_ratio` is not Host storage evidence. If Host IO is healthy and `mte2_ratio` is high, hand off to computation analysis.
- Treat `npu-smi` as hardware-health and coarse-utilization evidence only. Do not use an idle `npu-smi` sample as R500 conduction evidence.
- On non-Ascend GPU smoke tests, use Host IO rules normally and treat same-window GPU idle/profiler overlap as analogous conduction evidence. Never use Ascend-only `mte2_ratio` outside Ascend profiler contexts.

### Trigger exclusions and handoffs

Do not start storage collection when the user has already identified a different primary bottleneck:

- Disk/IO is explicitly idle while CPU is saturated by image decode or preprocessing: hand off to `mindstudio-cpu-binding`.
- The slowdown is in `allreduce` or other collective communication rather than data loading: hand off to `ascend-communication-analysis`.
- A profiler-only report concerns an operator's internal `mte2_ratio`, without a Host-IO symptom: hand off to `ascend-computation-analysis`.
- The symptom is dispatch or launch latency / Host Bound with data loading unaffected: hand off to `ascend-schedule-analysis`.

Multi-rank, DataLoader, or NPU-idle wording alone is not proof of a storage issue. If the primary cause is ambiguous, ask for the missing evidence and avoid claiming a storage root cause; run the collector only when a storage symptom remains plausible.

## Workflow

1. Classify the scenario: DataLoader/dataset reads, checkpoint loading, NFS/remote access, small files, rank/worker contention, or suspected device starvation.
2. Collect a read-only IO Snapshot for the affected workload window. Pass `--pid` and `--path` whenever available.
3. Run the deterministic analyzer. Do not replace analyzer output with invented thresholds.
4. Inspect R100-R400 as the Host IO chain.
5. Inspect R500 separately. Require profiler-side idle plus confirmed Host IO before attributing device idle to storage.
6. Report confidence, evidence fields, missing evidence, safe recommendations, rollback, and before/after validation.

## Prerequisites

Use Python 3.10 or newer. Verify dependencies before running the collector or evals:

```bash
python3 -c "import pydantic, yaml; print(pydantic.__version__)"
```

If dependencies are missing, obtain user approval before modifying the Python environment, then install the declared versions:

```bash
python3 -m pip install -r requirements.txt
```

Treat `iostat` and `pidstat` from `sysstat` as optional but strongly preferred. The collector falls back to `/proc/diskstats` when they are unavailable and records the confidence loss.

## Collect and analyze

Collect a snapshot:

```bash
python3 scripts/collect_io_snapshot.py --duration 30 --out io_snapshot.json
python3 scripts/collect_io_snapshot.py --duration 30 --out io_snapshot.json --pid 12345 --path /data
```

Analyze a snapshot:

```bash
python3 scripts/analyze_io_snapshot.py io_snapshot.json --mode all -o findings.json
python3 scripts/analyze_io_snapshot.py io_snapshot.json --profile npu_metrics.json -o findings.json
```

Use this profile-summary shape only after reading profiler output from the same workload window:

```json
{
  "device_free_percent": 25,
  "mte2_ratio": 0.1,
  "profile_window": {
    "start": "2026-07-20T10:00:05+00:00",
    "end": "2026-07-20T10:00:20+00:00",
    "scope": "between_first_and_last_exported_device_task"
  },
  "conduction_evidence": {
    "io_npu_overlap_observed": true,
    "controlled_experiment": {"result": "improved"}
  }
}
```

- `device_free_percent` 和 `mte2_ratio` 必须带合法的 `profile_window.start/end`；至少一半的 profiler 窗口必须落在 Snapshot.window 内，否则 analyzer 会丢弃动态指标并报告 validation error。
Set `io_npu_overlap_observed=true` only after verifying temporal overlap between Host IO pressure and device Free/DataLoader wait/step idle. Set `controlled_experiment.result="improved"` only when a controlled cache, storage, or IO-concurrency change improves the device idle symptom. Omit unverifiable fields instead of guessing.

从 Ascend `msprof` 导出目录生成保守 profile 摘要：

```bash
python3 scripts/summarize_msprof.py /path/to/msprof-output --device 0 -o npu_metrics.json
```

该摘要器只计算导出的 task 区间和指标，不会自动推断 `io_npu_overlap_observed`。

## Interpret rules

- R000: report missing, unsupported, failed, stale, or malformed evidence.
- R100: report local device throughput, IOPS, await, queue, or sustained pressure.
- R200: confirm NFS performance only from current-window mountstats RTT/execute/retransmission or equivalent timeout/throughput evidence; identify other network filesystems and hand off.
- R300: confirm remote metadata pressure from NFS metadata operation latency; treat small IO as a candidate signal only.
- R400: require same-device mapping, data-relevant paths, active PID IO, R100 device pressure, and overlapping windows for high confidence.
- R500: require confirmed Host IO plus device-side idle. Cap at medium without verified overlap or a controlled experiment.

## Safety

- The `Non-negotiable safety gate` and its response templates are authoritative; do not paraphrase away or postpone any required field.
- Never automatically run remount, umount, `blockdev --setra`, sysctl changes, fstab edits, NFS/Lustre/GPFS client or server changes, service restarts, or synthetic stress workloads. Remount/readahead may proceed only after their complete template preview, current-value capture, and a separate confirmation.
- Never run `drop_caches` through this Skill, even after confirmation. Never write fstab or other persistent configuration as part of a temporary performance experiment.
- Synthetic IO/NPU stress is allowed only when an operator explicitly requests a bounded test on an idle, isolated test node; it must never be launched automatically or against a production workload.
- Do not claim Host IO caused NPU idle when profiler evidence is absent.
- Do not emit high-confidence dynamic findings from missing, stale, malformed, or target-mismatched time windows.
- Do not treat provider `unsupported`, `empty`, or failure states as normal health.

## Validate the skill

Run deterministic evals and unit tests:

```bash
python3 evals/run_eval.py
python3 -m unittest discover -s evals -p 'test_*.py' -v
```

Run read-only validation on a Linux/Ascend host while the representative workload is active:

```bash
python3 evals/run_live_eval.py --duration 10 --path /data --require-npu
python3 evals/run_live_eval.py --duration 30 --path /data --profile npu_metrics.json --require-npu-runtime
```

Interpret `SKIP` as an unmet prerequisite, not a pass. Require `--require-npu-runtime`, `--require-nfs`, or `--require-r500-high` in an environment intended to certify those capabilities.

For an explicitly idle, isolated test node only, an operator may run the bounded real-device smoke below. It is synthetic NPU load and must never be launched automatically by this Skill or against a production workload:

```bash
source /path/to/cann/set_env.sh
python3 evals/run_npu_runtime_eval.py --elements 1048576 --iterations 100 --report /tmp/npu-runtime.json
```

## References

- `references/io_snapshot_schema.md`: snapshot schema and provider contract.
- `references/collection_guide.md`: collection protocol and command guidance.
- `references/failure_handbook.md`: root-cause evidence and remediation mapping.
- `scripts/collect_io_snapshot.py`: read-only collector.
- `scripts/analyze_io_snapshot.py`: deterministic analyzer.
- `scripts/summarize_msprof.py`: conservative `msprof` CSV-to-profile summarizer.
- `evals/cases.yaml` and `evals/run_eval.py`: deterministic behavior cases and runner.
- `evals/run_live_eval.py`: read-only Linux, provider, NPU, NFS, and R500 environment validation.
- `evals/run_npu_runtime_eval.py`: explicit, bounded ACLNN real-device smoke; operator-invoked only.
