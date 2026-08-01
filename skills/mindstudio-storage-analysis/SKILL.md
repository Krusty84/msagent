---
name: mindstudio-storage-analysis
description: "Diagnose storage and Host IO bottlenecks on Ascend NPU training or inference nodes, discover unknown workload PID/data paths, and produce terminal and offline HTML reports. Use for slow DataLoader/dataset/checkpoint reads, data-starvation idle gaps, iostat/await abnormalities, NFS RTT/execute/retrans issues, small-file overhead, rank/worker IO contention, single-device workloads that become slow with multiple ranks specifically during data loading, or requests to remount, drop caches, or change block-device readahead that require the safety gate. Do not use when the established primary issue is CPU decode with idle disks (mindstudio-cpu-binding), allreduce communication (ascend-communication-analysis), operator-internal mte2 (ascend-computation-analysis), or Host launch/scheduling while data loading is normal (ascend-schedule-analysis). Uses bounded read-only discovery, deterministic same-window evidence, and reversible optimization previews."
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

Cover local disk throughput, IOPS, queue and await pressure; NFS latency and retransmission; remote metadata and small-file overhead; multi-process IO interference; and Host-IO-to-device-idle triage. Automated network-storage confirmation is currently NFS-only; other network filesystems are identified and handed off without an R200/R300 high conclusion.

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

- Automatic network-storage performance confirmation currently covers NFS only and requires current-window mountstats RTT, execute latency, retransmission, or major-timeout evidence; mount type or low throughput alone never confirms a bottleneck.
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
2. Resolve the target PID and data/checkpoint path. Reuse explicit values from the user. If either is unknown, run the bounded read-only target discoverer before asking the user to locate it manually.
3. Read `recommendation` and the ranked candidates. When `requires_confirmation=true`, present the top candidates with their reasons and ask one concise confirmation question; never silently choose between similar candidates. Target discovery never starts collection.
4. Collect a read-only IO Snapshot for the affected workload window. Pass `--pid` for bounded R400 PID-to-device mapping; add `--path` to bind the affected data scope. A path alone never triggers a host-wide `/proc` scan.
5. Run the deterministic analyzer. Do not replace analyzer output with invented thresholds.
6. Inspect R100-R400 as the Host IO chain, then inspect R500 separately. Require profiler-side idle plus confirmed Host IO before attributing device idle to storage.
7. Report confidence, evidence fields, missing evidence, safe recommendations, rollback, and before/after validation in the terminal.
8. When the filesystem is writable, create `agent_report.json`, run the deterministic HTML renderer, and return the path to `io_report.html` as an additional artifact. The HTML report does not replace the terminal answer.

## Prerequisites

All relative commands below assume the current directory is the Skill root: the directory
containing this `SKILL.md`. Resolve that directory from the loaded Skill location and `cd` to
it before running `scripts/...` or `evals/...`; do not assume the repository root is the cwd.

Use Python 3.10 or newer. Verify dependencies before running the collector or evals:

```bash
python3 -c "import pydantic, yaml; print(pydantic.__version__)"
```

If dependencies are missing, obtain user approval before modifying the Python environment, then install the declared versions:

```bash
python3 -m pip install -r requirements.txt
```

Treat `iostat` and `pidstat` from `sysstat` as optional but strongly preferred. The collector falls back to a two-endpoint `/proc/diskstats` delta when they are unavailable. That fallback can identify a candidate window but cannot certify either pressure or health above medium confidence.

## Discover, collect, and analyze

Discover the target when the PID or data path is unknown:

```bash
python3 scripts/discover_io_target.py -o target_candidates.json
python3 scripts/discover_io_target.py --process-pattern torchrun --path-hint /data -o target_candidates.json
python3 scripts/discover_io_target.py --pid 12345 -o target_candidates.json
```

`--process-pattern` is a case-insensitive plain-text hint, not a regular expression. `--path-hint` must be an absolute path from information the user supplied; do not invent a path. The discoverer is bounded by process, file-descriptor, and time limits. It reads process command lines, working-directory links, open-file links, and mount metadata from `/proc`; it does not read `/proc/<pid>/environ`, dataset contents, configuration contents, or checkpoint contents.

Use the output as follows:

- `process_candidates`: ranked candidate workload processes, with evidence for each score.
- `process_candidates[].path_candidates`: ranked data/checkpoint path candidates for each process, with command-line, open-file, working-directory, and mount evidence.
- `recommendation.preview_command`: the exact read-only collector command to run when a process is sufficiently clear. Execute it only when `requires_confirmation=false`; otherwise show candidates and confirm the target first.
- `status=partial` means the bounded scan hit a permission, count, or time limit. Report that limitation; do not reinterpret missing candidates as proof that no workload exists.

An explicit user PID or exact path hint takes priority. Without explicit values, a process must score at least 50 and lead the next candidate by at least 15 points; a path must score at least 70 and lead by at least 15. Otherwise the Agent must ask for confirmation. A working directory alone is weak evidence and never proves it is the dataset path.

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
    "scope": "matched_workload_device_timeline"
  },
  "provenance": {
    "device_free_percent": {
      "source_type": "profiler_timeline",
      "artifact_id": "PROF_20260720/device_0_timeline",
      "device_id": 0,
      "metric": "device_free_percent",
      "extraction_method": "device_idle_interval_ratio"
    },
    "mte2_ratio": {
      "source_type": "profiler_database",
      "artifact_id": "PROF_20260720/ascend_pytorch_profiler.db",
      "device_id": 0,
      "metric": "mte2_ratio",
      "extraction_method": "workload_total_cycle_ratio"
    }
  },
  "conduction_evidence": {
    "io_npu_overlap_observed": true,
    "overlap_provenance": {
      "artifact_id": "PROF_20260720/device_0_timeline",
      "device_id": 0,
      "metric": "device_free_percent",
      "extraction_method": "timeline_interval_overlap",
      "host_rule_ids": ["R100"],
      "host_evidence_interval": {
        "start": "2026-07-20T10:00:00+00:00",
        "end": "2026-07-20T10:00:20+00:00"
      },
      "device_evidence_interval": {
        "start": "2026-07-20T10:00:05+00:00",
        "end": "2026-07-20T10:00:20+00:00"
      },
      "target": {"pid": 42, "path": "/data/train"}
    }
  }
}
```

- `device_free_percent` must come from an actual profiler timeline or DB idle/wait view, not gaps reconstructed from `op_summary`. Dynamic metrics require a valid `profile_window.start/end`; at least half of the profiler window must overlap `Snapshot.window`.
- Any high positive conclusion, high-confidence handoff, or storage-priority downgrade requires `profile_window.scope="matched_workload_device_timeline"` and metric-specific `provenance`. `device_free_percent` accepts a profiler timeline `device_idle_interval_ratio` or profiler database `database_device_free_metric`; `mte2_ratio` accepts only a profiler database `workload_total_cycle_ratio`. Each provenance entry must include a non-empty artifact ID, non-negative device ID, exact metric name, and extraction method. Missing, arbitrary, `op_summary`, or exported-task-gap provenance is non-certifying and caps the cross-chain conclusion below high.
- A future trusted R500 artifact verifier must require an explicit `Snapshot.target.pid` or `target.path` with a repeated, identity-bound block-device mapping or current NFS mount identity. The profile window must overlap at least one target-scoped, confirmed R100-R400 `evidence_interval` for at least 1 second and 50% of the shorter interval. A null target or broad top-level Snapshot window is not a substitute.
- Apply the same-window requirement to negative cross-chain conclusions too: do not hand off high-confidence MTE2/device-idle findings or downgrade a confirmed storage issue using a disjoint profile window.
- `mte2_ratio` is computation-side context only. Do not aggregate per-operator cycle ratios into a workload ratio without their compatible total-cycle denominators.
`io_npu_overlap_observed` and `controlled_experiment` supplied only in profile JSON are non-certifying context: artifact identifiers and intervals are not proof that the underlying timeline or experiment artifacts were verified. They may support a medium-confidence candidate but cannot produce R500 high until this Skill has a trusted external artifact verifier. A bare boolean, null target, or bare result is invalid. Omit unverifiable fields instead of guessing.

从 Ascend `msprof` 的 `op_summary` 导出生成仅供排查的诊断摘要：

```bash
python3 scripts/summarize_msprof.py /path/to/msprof-output --device 0 -o op_summary_diagnostics.json
```

`op_summary` 不是设备 timeline。摘要器只输出 `op_summary_task_gap_proxy_percent` 和逐列 MTE2 ratio 统计等明确标注的 proxy；它不会输出可认证 R500 的 `device_free_percent`、聚合 `mte2_ratio`、`profile_window` 或 `conduction_evidence`，其结果不能直接作为 `--profile` 输入。

## Generate the HTML report

After explaining the deterministic findings, write the same plain-language summary and recommendations to `agent_report.json` using the contract in `references/html_report.md`. Then generate one self-contained report:

For this explanation step, extract only `rule_id`, `severity`, `confidence`, `summary`, `missing_evidence`, and the small device-topology fields needed to interpret a finding. Do not page through the full Snapshot or repeatedly reread large JSON artifacts. When a logical device and one of its `backing_devices` both appear in findings, describe them as layers of the same storage path; do not count them as independent disks or independent root causes.

```bash
python3 scripts/render_io_report.py \
  --snapshot io_snapshot.json \
  --findings findings.json \
  --targets target_candidates.json \
  --msprof op_summary_diagnostics.json \
  --agent-report agent_report.json \
  --output io_report.html
```

`--targets` and `--msprof` are optional; omit them when those earlier steps were not run. `--agent-report` is also optional for deterministic-only use, but include it during an Agent-led diagnosis so the HTML contains the user-facing summary, limitations, and recommendations.

Do not hand-write or edit report HTML. The renderer must receive the original artifacts, escapes Agent-controlled text, and only presents existing evidence; it does not rerun rules or execute recommendations. A text-only model can use this flow because it produces structured JSON and invokes a fixed renderer. Image understanding and screenshot inspection are not runtime requirements.

Return both the concise terminal diagnosis and the absolute HTML path. Warn before external sharing that the report may contain hostnames, PIDs, command summaries, and data paths.

## Interpret rules

- R000: report missing, unsupported, failed, stale, or malformed evidence.
- R100: report local device throughput, IOPS, await, queue, or sustained pressure. High positive or negative conclusions require an actual evidence window of at least 10 seconds and at least three samples covering util plus the supporting queue/await field; shorter or sparse evidence is capped at medium. A two-endpoint `diskstats` delta remains medium even when its window is longer than 10 seconds.
- R200: confirm NFS performance only from current-window mountstats RTT/execute/retransmission or major-timeout evidence; low throughput alone is context, not pressure proof. Identify other network filesystems and hand off.
- R300: confirm remote metadata pressure from NFS metadata operation latency; treat small IO as a candidate signal only.
- R400: require same-device mapping, data-relevant paths, each PID active in more than half of at least three pidstat reports, R100 device pressure, `observation_count>=2` for each PID's mapping, stable `boot_id` + PID starttime identity, and a real common interval across those mappings, R100, pidstat, and process-map evidence for high confidence. Mount and backing-device identity must be freshly observed at both process-map endpoints.
- R500: require target-scoped confirmed Host IO plus device-side idle. With the current JSON-only profile contract, cap at medium; do not infer a high-confidence conduction chain from self-declared overlap or experiment evidence.

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
python3 evals/run_live_eval.py --duration 10 --pid <workload_pid> --path /data --require-npu
```

For R500 investigation, capture the IO Snapshot during the profiled workload and obtain `device_free_percent` with timestamps from an actual profiler timeline/DB view. The current JSON-only profile contract reports this as medium-confidence context; it does not certify an R500 positive-high conclusion until a trusted artifact verifier exists. The `op_summary` diagnostic summarizer cannot create certifying metrics. Validate the paired artifacts without starting a new collection window:

```bash
python3 evals/run_live_eval.py --snapshot io_snapshot.json --profile npu_metrics.json --require-npu-runtime
```

Interpret `SKIP` as an unmet prerequisite, not a pass. `--require-nfs` certifies activity only for the NFS mount containing `snapshot.target.path`; unrelated NFS traffic cannot satisfy it. Require `--require-npu-runtime` or `--require-nfs` only in an environment intended to certify those capabilities.

For an explicitly idle, isolated test node only, an operator may run the bounded real-device smoke below. It is synthetic NPU load and must never be launched automatically by this Skill or against a production workload:

```bash
source /path/to/cann/set_env.sh
python3 evals/run_npu_runtime_eval.py --elements 1048576 --iterations 100 --report /tmp/npu-runtime.json
```

## References

- `references/io_snapshot_schema.md`: snapshot schema and provider contract.
- `references/collection_guide.md`: collection protocol and command guidance.
- `references/failure_handbook.md`: root-cause evidence and remediation mapping.
- `references/html_report.md`: HTML input contract, Agent report schema, output content, and text-only-model flow.
- `scripts/discover_io_target.py`: bounded read-only workload PID and data-path discovery; produces ranked candidates, not a diagnosis.
- `scripts/collect_io_snapshot.py`: read-only collector.
- `scripts/analyze_io_snapshot.py`: deterministic analyzer.
- `scripts/summarize_msprof.py`: non-certifying `msprof op_summary` diagnostic summarizer.
- `scripts/render_io_report.py`: deterministic, offline HTML report renderer.
- `assets/io_report_template.html`: self-contained report template used only by the renderer.
- `evals/cases.yaml` and `evals/run_eval.py`: deterministic behavior cases and runner.
- `evals/run_live_eval.py`: read-only Linux, provider, NPU, NFS, and R500 environment validation.
- `evals/run_npu_runtime_eval.py`: explicit, bounded ACLNN real-device smoke; operator-invoked only.
