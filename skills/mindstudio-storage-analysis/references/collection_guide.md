# 存储分析采集指南

本文件定义 `mindstudio-storage-analysis` 的只读采集命令清单、IO Snapshot 数据契约和提问协议。所有命令默认**只读**，不修改系统状态。

## 目录

- [1. 只读采集命令清单](#1-只读采集命令清单)
- [2. IO Snapshot 数据契约](#2-io-snapshot-数据契约)
- [3. 提问协议](#3-提问协议)
- [4. 降级采集](#4-降级采集)

本文中的相对路径命令均以 Skill 根目录（包含 `SKILL.md` 的目录）为当前目录；先从已加载 Skill 的位置解析并进入该目录，不要假设当前位于 msagent 仓库根目录。

## 1. 只读采集命令清单

### 1.1 设备级 IO 统计（核心）

```bash
# 推荐格式：扩展字段 + kB/s 单位 + 每秒采样 + 重复 N 次。
# 涵盖 %util / await / r/s w/s / rkB/s wkB/s / aqu-sz。
iostat -xz -k 1 10

# 备选：/proc/diskstats 两次采样差值（sysstat 未安装时）
# 字段顺序见内核文档 Documentation/iostats.txt
cat /proc/diskstats
sleep 10
cat /proc/diskstats
```

关键字段解读（`iostat -xz -k`）：

| 字段 | 含义 | 关注点 |
|---|---|---|
| `%util` | 设备忙时间占比 | 长期接近 100% 提示饱和（NVMe 含义弱化，需结合队列） |
| `r/s w/s` | 每秒读 / 写完成数 | IOPS 维度，小文件场景关注 |
| `rkB/s wkB/s` | 每秒读 / 写吞吐（KiB/s） | 带宽维度，大文件场景关注 |
| `r_await w_await` | 平均读 / 写等待时间（ms） | SSD < 5ms，HDD < 20ms，网络存储更高 |
| `aqu-sz` | 平均队列长度 | 持续高说明请求积压 |
| `rrqm/s wrqm/s` | 合并的读 / 写请求 | 高说明 IO 可被合并（大块友好） |

### 1.2 自动发现目标进程和数据路径

当用户没有提供训练 PID 或数据路径时，Agent 先运行目标发现器，不要求用户自己登录服务器查找：

```bash
# 完全未知：在有界时间内扫描训练进程候选
python3 scripts/discover_io_target.py -o target_candidates.json

# 用户只记得启动方式或大致绝对路径时，把它作为线索
python3 scripts/discover_io_target.py --process-pattern torchrun --path-hint /data -o target_candidates.json

# 已知 PID，只补充寻找它访问的数据路径
python3 scripts/discover_io_target.py --pid 12345 -o target_candidates.json
```

发现器只读取以下信息：

- `/proc/<pid>/cmdline`：判断它是否像训练/推理进程，并提取 `--data-dir`、`--dataset`、`--checkpoint` 等显式路径参数；常见口令和令牌参数会被脱敏。
- `/proc/<pid>/cwd`：记录进程工作目录，只作为弱线索。
- `/proc/<pid>/fd` 的符号链接：查看进程当前打开的文件落在哪些目录，不读取文件内容。
- `/proc/<pid>/mountinfo`：判断候选路径位于本地盘、NFS 或其他网络文件系统。

它不会读取 `/proc/<pid>/environ`，不会打开数据集、配置或 checkpoint 内容，不递归遍历目录，也不会连接远端存储。默认最多展开 20 个候选进程、每个进程 256 个文件描述符，总时间预算 3 秒；达到限制时输出 `status=partial` 和原因。

输出 `target_candidates.json` 的重点字段：

| 字段 | 含义 | Agent 怎么用 |
|---|---|---|
| `process_candidates[]` | 按证据排序的训练进程候选 | 向用户展示 PID、启动命令摘要和入选原因 |
| `process_candidates[].path_candidates[]` | 每个进程可能使用的数据/checkpoint 路径 | 展示路径、来源、打开文件样例和挂载类型 |
| `recommendation.pid/path` | 证据足够区分时给出的推荐目标 | 仅在无需确认时传给 collector |
| `recommendation.requires_confirmation` | 是否仍有相近或证据较弱的候选 | 为 `true` 时必须让用户确认，不能替用户猜 |
| `recommendation.preview_command` | 已填好目标的只读采集命令 | 确认后执行，生成 `io_snapshot.json` |

无显式值时，进程候选需达到 50 分且领先第二名至少 15 分；路径候选需达到 70 分且领先至少 15 分。启动命令里的数据参数和当前打开的数据文件是强证据，单独的工作目录是弱证据。分数只用于选择采集目标，不是存储异常结论。

### 1.3 进程级 IO 统计

```bash
# 默认按进程统计读写速率和累计 block IO delay 指示量，用于定位哪些进程在压盘
# iodelay 不是单次 IO latency；需要线程维度时显式加 -t
pidstat -d 1 10
pidstat -d -t 1 10

# 配合找出训练主进程及其 DataLoader worker
ps -eo pid,ppid,comm,args | grep -E 'python|torch|dataloader'

# 单个进程的累计 IO（启动以来）
cat /proc/<pid>/io
```

### 1.4 挂载与文件系统

```bash
# 挂载点与挂载选项（识别 nfs/cifs/lustre/gpfs/fuse 与 noatime 等）
cat /proc/mounts

# 磁盘空间与 inode 使用（海量小文件时 inode 可能先满）
df -h
df -i

# 设备 → 挂载点映射
findmnt
```

### 1.5 网络存储专用（按需）

```bash
# NFS：每挂载点的 RPC 延迟
nfsiostat 1 5

# NFS：客户端累计统计与重传（注意路径是 /proc/net/rpc/nfs，不是 nfsstat）
# 累计值，需两次采样求差才能反映本次 workload 窗口
cat /proc/net/rpc/nfs

# NFS：客户端 mount 统计（per-mount 的 per-op RTT / execute / retrans，首选）
# 同样是累计值，collector 在采集窗起止各读一次求差
cat /proc/self/mountstats

# 设备/路径 → 挂载点映射：findmnt 必须用 -T/--target 查找包含路径的挂载
findmnt -n -T /data/train/shard.bin -o TARGET,SOURCE,FSTYPE
# 或直接读 /proc/self/mountinfo 做最长前缀匹配（collector 首选，支持含空格/Unicode 挂载点）

# Lustre（如有 lctl/lfs）
lctl get_param llite.*.stats
lctl get_param osc.*.stats
lfs df -h
```

使用 `--pid --path` 时，collector 会在 `/proc/<pid>/root` 对目标路径做符号链接解析；Snapshot 的 `target.path` 记录该进程视角下的规范路径，若发生解析则以 `target.requested_path` 保留原始命令行路径。

### 1.6 内存 / page cache

```bash
# Cached / Buffers 大小，判断热数据是否能在内存中缓存
free -h
cat /proc/meminfo | grep -E 'Cached|Buffers|Dirty|Writeback|MemAvailable'

# 当前 readahead 设置（块设备）
blockdev --getra /dev/<dev>

# 当前 IO 调度器
cat /sys/block/<dev>/queue/scheduler
```

### 1.7 NPU 侧交叉验证（来自 profiler，非本 skill 采集）

IO 传导链证据来自 Ascend profiler 数据，不在此采集：

- `ascend_pytorch_profiler_{rank_id}.db`：device `Free`、DataLoader wait、step throughput（**不使用 `mte2_ratio` 作为传导证据**，理由见 SKILL.md）
- `step_trace_time.csv` / `analysis.db` 的 `StepTraceTime`：step 空泡段
- `profiler_info.json`：profiler level（影响结论强度）

把已核验指标整理成 analyzer profile JSON：

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

- `device_free_percent` 必须来自 profiler timeline/DB 的真实设备空闲或 wait 视图，不能由 `op_summary` task 间隙推导；范围为 0~100。
- `mte2_ratio` 只作计算侧上下文，范围为 0~1；不同算子的 cycle ratio 没有兼容 total-cycle 分母时不得按 Task Duration 聚合。
- 动态 profile 必须带 `profile_window.start/end`，并且至少 50% 的 profiler 窗口与 Snapshot.window 重叠；缺失或陈旧时 analyzer 丢弃动态指标。
- high 正向、high 负向或存储优先级降级要求 scope 精确为 `matched_workload_device_timeline`，并为对应指标提供完整 `provenance`。允许的 source/extraction 组合见下方 live 验收说明；缺失、任意 scope 或 `op_summary` task-gap 来源只作非认证候选。
- 当前 JSON-only profile 契约下 R500 正向结论封顶 medium。未来接入可信 profiler artifact verifier 时，R500 high 还必须要求显式 `Snapshot.target.pid/path` 已由重复观测、进程身份和 sysfs 设备映射（或当前 NFS 挂载身份）认证；profile window 与至少一个目标作用域内已确认 R100~R400 finding 的 `evidence_interval` 至少重叠 1 秒，且公共交集不少于较短区间的 50%。空 target 和顶层 Snapshot.window 都不能替代该绑定。
- 负向跨链结论同样必须同窗：不同窗的 MTE2/device Free 不能用于高置信转交；
  低 device Free 只能说明未观察到明显设备空泡，不能推断设备忙的具体原因。
- `io_npu_overlap_observed=true` 必须同时提供 `overlap_provenance`。其中 artifact/device 必须匹配 `provenance.device_free_percent`，`host_rule_ids` 必须对应目标作用域内已确认 Host finding，Host/device interval 必须分别包含在 finding/profile 的证据窗内并有足量交集，非空 `target` 必须与 Snapshot 的 `{pid,path}` 精确一致。裸 boolean 或 `{pid:null,path:null}` 不认证。
- 对照实验必须提供 `experiment_id`、`device_id`、`metric=device_free_percent`、`action`、精确 `target`，以及各含 artifact、window、device-Free 值的 `baseline`/`treatment`。baseline 必须匹配当前 profile，两个 artifact 不同且窗口不重叠；`result=improved` 还要求 treatment 的 device Free 更低。`result` 只接受 `improved`、`no_change`、`worse`、`inconclusive`，裸 result 不认证。
- `npu-smi` 空闲采样只能验证设备可见性/健康，不能替代 profiler 时间线或对照实验。
- 无法核验的字段应省略，不得按现象猜测。

如果输入是 Ascend `msprof` 导出目录，可用摘要器查看 `op_summary` 诊断 proxy：

```bash
python3 scripts/summarize_msprof.py /path/to/msprof-output --device 0 -o op_summary_diagnostics.json
```

它只读取唯一的 `op_summary_*.csv`，输出明确标注的 task-gap 和逐列 MTE2 ratio 统计 proxy。`op_summary` 没有设备 timeline，Task Duration 也包含调度、执行和响应阶段；因此摘要器不会生成 `device_free_percent`、全局 `mte2_ratio`、`profile_window` 或 `conduction_evidence`，输出不能直接传给 analyzer 的 `--profile`。

用 live runner 检查实时环境：

```bash
python3 evals/run_live_eval.py --duration 30 --pid <workload_pid> --path /data --require-npu-runtime
```

R500 验收必须使用同一 workload 时间窗内配对的 Snapshot 和真实 profiler timeline/DB 指标。应在 profiler 覆盖目标 workload 时并行运行 collector，再复用该 Snapshot；不要为已完成的 profile 启动一个新的采集窗口：

```bash
# 终端 A：在 profiler 覆盖目标 workload 的同时采集
python3 scripts/collect_io_snapshot.py --duration 30 --pid 42 --path /data/train --out io_snapshot.json
# 可选：生成不参与 R500 认证的 op_summary 诊断 proxy
python3 scripts/summarize_msprof.py /path/to/msprof-output --device 0 -o op_summary_diagnostics.json
# 从 timeline/DB 取得真实 device Free、认证 scope、metric provenance 及其窗口，生成 npu_metrics.json；核验实际 Host evidence_interval 同窗传导或受控实验后执行
python3 evals/run_live_eval.py --snapshot io_snapshot.json --profile npu_metrics.json --require-npu-runtime
```

第一条 collector 命令必须用 `--pid`/`--path` 显式绑定目标，并与被 profile 的 workload 时间窗口重叠。`summarize_msprof.py` 只产生非认证 proxy。`npu_metrics.json` 必须由真实 timeline/DB 指标构建，带 `profile_window.scope="matched_workload_device_timeline"`，并为每个动态指标记录 `provenance.{metric}`：`source_type`、可审计的 `artifact_id`、非负整数 `device_id`、精确 `metric` 和允许的 `extraction_method`。当前 JSON-only contract 只能给出 medium R500 候选；可信 artifact verifier 尚未实现时不得要求 R500 high。`op_summary`、导出 task gap、缺失或任意 scope 均不参与认证。`run_live_eval.py --profile` 要求 profile 带 `profile_window.start/end`，analyzer 会拒绝与所给 Snapshot 不同窗的动态指标。`--require-npu-runtime` 会执行真实 ACL init、设备枚举和 finalize，但不会制造负载。`--require-nfs` 只认证包含 `snapshot.target.path` 的 NFS 挂载及其同窗 delta，不接受其他挂载的活动。

在明确空闲的隔离测试节点上，若需要验证 ACLNN 编译、HBM 拷贝、算子执行和结果校验，可由操作者显式运行有界 smoke；这不是 collector 的自动步骤：

```bash
source /path/to/cann/set_env.sh
python3 evals/run_npu_runtime_eval.py --elements 1048576 --iterations 100 --report /tmp/npu-runtime.json
```

## 2. IO Snapshot 数据契约

`scripts/collect_io_snapshot.py` 输出的 JSON 结构采用两层校验：collector 的 pydantic 模型校验顶层 envelope 和自身生成的结构；analyzer 的 `validate_analysis_request()` / `normalize_and_validate()` 校验 provider `parsed` 深层容器、计数关系、时间窗、证据绑定和 profile 语义。完整字段说明见 `references/io_snapshot_schema.md`。

**核心变更（相对旧版）**：每个数据源用统一的 `ProviderResult` 表达，**不再用布尔 `available`**：

```json
{
  "source": "iostat",
  "status": "ok",
  "started_at": "2026-06-30T16:53:25+08:00",
  "ended_at": "2026-06-30T16:53:56+08:00",
  "exit_code": 0,
  "stderr": "",
  "error": "",
  "raw": "<iostat -xk 原始文本>",
  "parsed": {
    "disks": {
      "sda": {"util_percent": 87.2, "sample_count": 3}
    }
  }
}
```

`status` 取值（失败绝不伪装成成功）：
- `ok`：命令成功且有输出
- `missing`：命令/文件不存在
- `permission_denied`：无权限
- `command_failed`：命令存在但非零退出
- `parse_failed`：解析失败
- `empty`：命令成功但无输出，或格式已识别但没有受支持的真实采集对象
- `unsupported`：平台/工具不支持（如无 NFS 挂载、无 Lustre 工具）

完整顶层模型和各 provider 的 `parsed` 字段只在 `references/io_snapshot_schema.md` 维护，避免两份契约漂移。采集时重点检查以下运行语义：

- `diskstats_sample`：两次采样，用于差值计算速率；`sectors_read` 单位为 512B sector。**只含主设备，不含分区**（已用 `/sys/class/block/<name>/partition` 过滤）。两个 counter 端点不等于 3 个持续指标样本；即使间隔超过 10 秒，R100 正向和负向结论也最高为 medium。
- `mounts_provider`：记录挂载列表的采集状态与时间来源。只有 `ok`、非空列表，且 `started_at`/`ended_at` 落在顶层窗口内，才能判断是否存在网络挂载。`empty`、缺失、无权限、命令失败或陈旧列表都必须降级为证据不足。
- `iostat.parsed.disks`：结构化的 per-device 指标（`r_per_s`、`rkB_per_s`、`avgqu_sz`、`await`、`util_percent` 等）；每个设备至少要有一项真实 IO 指标，只有 `sample_count` 或空对象属于 `parse_failed`。全窗口聚合时 rate 算术平均、await 按总 IOPS 加权、util 保留 mean/max/p95；`sample_count` 是设备的有效报告数且不得大于 `parsed.reports`，`*_sample_count` 是字段样本数，`*_with_util_sample_count` 是该字段与 util 的同报告共现数。R100 正向或负向 high 要求至少 10 秒实际证据窗口，且至少 3 个样本同时覆盖 util 与触发判定的 queue/await 字段；缺少共现计数时必须降级，不能用独立计数猜测。
- `iostat`/`pidstat` 外部命令的 stdout 上限为 8 MiB、stderr 上限为 256 KiB。超限时 collector 终止该命令、将 provider 标记为 `command_failed`，且只保留最多 64 KiB 的诊断，避免长时间采集耗尽内存或写出超大 Snapshot。
- `pidstat.parsed.processes`：结构化的 per-process 指标（`pid`、`kbr_per_s`、`kbw_per_s`、`command`、`sample_count`、`active_sample_count`）；后者统计读或写 IO 速率至少 100 KiB/s 的真实报告数。`reports`、`sample_count`、`active_sample_count` 必须是 JSON 整数，并满足 `0 <= active_sample_count <= sample_count <= reports`；`reports` 必须为正。文本输出的 `Average:` 汇总块不计入 `reports`。R400 high 要求每个候选 PID 至少 3 个活跃样本且覆盖超过半数 `reports`，避免把错时的短暂 IO 拼成同时争抢。
- `process_io_map.parsed.mappings`：PID → path → mount → device 映射（R400 所需，必须提供 `--pid`；`--path` 用于收窄数据范围，单独提供 path 会返回 `status=unsupported` 而不会扫描全机 `/proc`）。进程树最多 256 项；后代项记录直接 `parent_pid`，analyzer 只将能沿父链回到显式目标 PID 的强身份后代纳入目标作用域；无父链的旧快照后代不参与归因。每个 PID 最多保留 256 条 FD 映射。指定 `--path` 时，collector 在最多 1 秒的 FD 扫描预算内优先保留数据相关路径；达到数量或时间预算都会在 `partial` 标明覆盖不完整，不能据此作 high。`observation_samples` 必须是正 JSON 整数；每条 mapping 的 `observation_count` 必须是非负 JSON 整数且不得超过它。每条 mapping 的 `boot_id`/`pid_starttime_ticks` 用于排除数值 PID 复用，`first_seen`/`last_seen`/`observation_count` 和顶层 `observation_samples` 用于证明多个 PID 确实同时映射该设备；同一进程和映射未实际观测两次或映射区间错开时不得 high。mountinfo 和 sysfs backing topology 只在单次观测内复用，窗口末次观测会重新读取；FD 提供 `mnt_id` 时必须精确匹配，不得退化为路径前缀认证。
- `df.parsed.filesystems`：空间与 inode 合并（`df -hP` + `df -iP`）。
- `nfs.parsed.mount_metrics`：`/proc/self/mountstats` per-op 统计的**窗内两次采样差值**（`windowing`/`avg_rtt_ms`/`avg_execute_ms`/`retrans`/`ops`，R200 性能证据）；无 NFS 挂载时 `status=unsupported`。单次累计值不反映本次 workload，差值才可作性能证据。`major_timeouts` 只有在同一 delta 中与非负整数 `ops`/`transmissions`/`retrans` 一致且不大于 `ops` 时才可确认瓶颈。
- `readahead`：单位为 512B sector（`blockdev --getra` 原始输出）。readahead 与 scheduler 为窗口外的静态上下文采集，所有块设备共用最多 5 秒探测预算；超时或未处理设备写入 `availability.partial`，不延长动态证据窗口。
- `availability`：`missing`（数据源缺失）/`partial`（unsupported/empty）/`errors`（permission/command_failed/parse_failed）三类汇总。
- `schema_version`：major.minor。minor 只增可选字段；analyzer 遇到未知 major 拒绝确定性分析。

## 3. 提问协议

### 3.1 总原则

- 每轮只问一个问题；不要用 "我先确认 3 点 / 4 点" 的批量问法。
- 优先用选择题；选择项使用用户语言，并提供 "不确定，先帮我看" 或 "使用默认值"。
- 用户已经提供的信息不要重复问。
- 不询问 `iostat`、`mount`、拓扑等采集器能自取的信息。
- PID 或数据路径未知时，先运行 `discover_io_target.py`；不要先把服务器定位工作推给用户。
- `recommendation.requires_confirmation=false` 时可直接使用推荐目标进行只读采集；为 `true` 时只展示最相关的 2～3 个候选和原因，并让用户确认其中一个。
- `status=partial` 或 `no_candidates` 不是“没有训练任务”的证明。说明扫描限制后，再询问一个最能缩小范围的信息，例如启动命令特征或大致数据目录。
- 如果用户已有 Snapshot，跳过采集提问，直接进入 Snapshot 质量检查。

### 3.2 必问项（按顺序）

1. **问题场景**：数据加载慢 / checkpoint 慢 / 多实例互相拖慢 / 网络存储疑似慢。
2. **目标确认（仅发现结果不明确时）**：从候选 PID 或路径中确认本次要分析的目标。
3. **数据集形态（自动分析后仍需补充时）**：大文件 / 海量小文件 / 混合（用于解释带宽与元数据线索）。

### 3.3 补问项（仅在采集后或信息不足时）

- 多卡规模：world_size、DataLoader `num_workers`、`prefetch_factor`、`persistent_workers`。
- benchmark 指标：samples/s、step time、吞吐目标。
- 是否有过 profiler 数据用于交叉验证。

补问也应使用单个选择题或明确说明 "不提供也可以继续"。

## 4. 降级采集

当理想采集条件不满足时，按以下顺序降级：

| 缺失 | 降级方案 |
|---|---|
| sysstat 未安装（无 `iostat`/`pidstat`） | 用 `/proc/diskstats` 两次采样差值算速率；`/proc/<pid>/io` 取累计 IO |
| 无 root | 多数只读命令（`iostat`、`cat /proc/*`、`df`）普通用户即可；`blockdev`、部分 `/sys` 读取可能受限，记入 `availability.missing` |
| 容器内 | `/proc/diskstats` 反映的是宿主机统计，需注意；`/proc/mounts` 反映容器视图。报告时标注 "容器视图" |
| 无 profiler 数据 | 只做 Host IO 压力链分析，明确标注 NPU 传导链未验证，置信度降低 |

永远不要因单一来源缺失就阻塞整个分析。
