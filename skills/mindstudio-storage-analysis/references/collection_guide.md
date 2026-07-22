# 存储分析采集指南

本文件定义 `mindstudio-storage-analysis` 的只读采集命令清单、IO Snapshot 数据契约和提问协议。所有命令默认**只读**，不修改系统状态。

## 1. 只读采集命令清单

### 1.1 设备级 IO 统计（核心）

```bash
# 推荐格式：扩展字段 + 每秒采样 + 重复 N 次。涵盖 %util / await / r/s w/s / rMB/s / aqu-sz。
iostat -xz 1 10

# 备选：/proc/diskstats 两次采样差值（sysstat 未安装时）
# 字段顺序见内核文档 Documentation/iostats.txt
cat /proc/diskstats
sleep 10
cat /proc/diskstats
```

关键字段解读（`iostat -xz`）：

| 字段 | 含义 | 关注点 |
|---|---|---|
| `%util` | 设备忙时间占比 | 长期接近 100% 提示饱和（NVMe 含义弱化，需结合队列） |
| `r/s w/s` | 每秒读 / 写完成数 | IOPS 维度，小文件场景关注 |
| `rMB/s wMB/s` | 每秒读 / 写吞吐 | 带宽维度，大文件场景关注 |
| `r_await w_await` | 平均读 / 写等待时间（ms） | SSD < 5ms，HDD < 20ms，网络存储更高 |
| `aqu-sz` | 平均队列长度 | 持续高说明请求积压 |
| `rrqm/s wrqm/s` | 合并的读 / 写请求 | 高说明 IO 可被合并（大块友好） |

### 1.2 进程级 IO 统计

```bash
# 每个进程 / 线程的读写字节与延迟，用于定位是哪些进程在压盘
pidstat -d 1 10

# 配合找出训练主进程及其 DataLoader worker
ps -eo pid,ppid,comm,args | grep -E 'python|torch|dataloader'

# 单个进程的累计 IO（启动以来）
cat /proc/<pid>/io
```

### 1.3 挂载与文件系统

```bash
# 挂载点与挂载选项（识别 nfs/cifs/lustre/gpfs/fuse 与 noatime 等）
cat /proc/mounts

# 磁盘空间与 inode 使用（海量小文件时 inode 可能先满）
df -h
df -i

# 设备 → 挂载点映射
findmnt
```

### 1.4 网络存储专用（按需）

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

### 1.5 内存 / page cache

```bash
# Cached / Buffers 大小，判断热数据是否能在内存中缓存
free -h
cat /proc/meminfo | grep -E 'Cached|Buffers|Dirty|Writeback|MemAvailable'

# 当前 readahead 设置（块设备）
blockdev --getra /dev/<dev>

# 当前 IO 调度器
cat /sys/block/<dev>/queue/scheduler
```

### 1.6 NPU 侧交叉验证（来自 profiler，非本 skill 采集）

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
    "scope": "between_first_and_last_exported_device_task"
  },
  "conduction_evidence": {
    "io_npu_overlap_observed": true,
    "controlled_experiment": {"result": "improved"}
  }
}
```

- `device_free_percent` 和 `mte2_ratio` 必须来自目标 workload 的 profiler 窗口，范围分别为 0~100 和 0~1。
- 动态 profile 必须带 `profile_window.start/end`，并且至少 50% 的 profiler 窗口与 Snapshot.window 重叠；缺失或陈旧时 analyzer 丢弃动态指标。
- `io_npu_overlap_observed` 只接受 JSON boolean；仅在 Host IO 异常区间与 device Free/DataLoader wait/step idle 存在足量重叠时设为 `true`。
- `controlled_experiment.result` 只接受 `improved`、`no_change`、`worse`、`inconclusive`；仅 `improved` 可把 R500 升级为 high。
- `npu-smi` 空闲采样只能验证设备可见性/健康，不能替代 profiler 时间线或对照实验。
- 无法核验的字段应省略，不得按现象猜测。

如果输入是 Ascend `msprof` 导出目录，可用仓库内的保守摘要器：

```bash
python3 scripts/summarize_msprof.py /path/to/msprof-output --device 0 -o npu_metrics.json
```

它只读取唯一的 `op_summary_*.csv`，对重叠 task 做区间合并，并记录 profile window/provenance；它不会推断 `io_npu_overlap_observed`。

用 live runner 检查环境和 profile 接入：

```bash
python3 evals/run_live_eval.py --duration 30 --path /data --profile npu_metrics.json
```

`run_live_eval.py --profile` 要求 profile 带 `profile_window.start/end`，并会在实际分析时拒绝与新 Snapshot 不同窗的动态指标。`--require-npu-runtime` 会执行真实 ACL init、设备枚举和 finalize，但不会制造负载。

在明确空闲的隔离测试节点上，若需要验证 ACLNN 编译、HBM 拷贝、算子执行和结果校验，可由操作者显式运行有界 smoke；这不是 collector 的自动步骤：

```bash
source /path/to/cann/set_env.sh
python3 evals/run_npu_runtime_eval.py --elements 1048576 --iterations 100 --report /tmp/npu-runtime.json
```

## 2. IO Snapshot 数据契约

`scripts/collect_io_snapshot.py` 输出的 JSON 结构。机器可校验的契约定义在 `scripts/collect_io_snapshot.py` 的 pydantic 模型（`IoSnapshot` / `ProviderResult` / `DiskStat` 等），字段说明见 `references/io_snapshot_schema.md`。

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
  "parsed": { "...结构化字段..." }
}
```

`status` 取值（失败绝不伪装成成功）：
- `ok`：命令成功且有输出
- `missing`：命令/文件不存在
- `permission_denied`：无权限
- `command_failed`：命令存在但非零退出
- `parse_failed`：解析失败
- `empty`：命令成功但无输出
- `unsupported`：平台/工具不支持（如无 NFS 挂载、无 Lustre 工具）

顶层结构示例：

```json
{
  "schema_version": "1.4",
  "collected_at": "2026-06-30T16:53:25+08:00",
  "host": {"hostname": "node-01", "kernel": "Linux 5.10.0", "platform": "..."},
  "duration_seconds": 30,
  "window": {"start": "2026-06-30T16:53:25+08:00", "end": "2026-06-30T16:53:56+08:00"},
  "target": {"pid": 12345, "path": "/data"},
  "mounts": [
    {"device": "192.168.1.10:/data", "mount_point": "/data", "fstype": "nfs4", "options": "rw,relatime,vers=4.1,..."}
  ],
  "mounts_provider": {"source": "mounts", "status": "ok", "started_at": "...", "ended_at": "...",
                      "parsed": [{"device": "192.168.1.10:/data", "mount_point": "/data", "fstype": "nfs4", "options": "rw,..."}]},
  "diskstats_sample": [
    {"sample_index": 0, "timestamp": 1234567.89, "disks": {"nvme0n1": {"reads_completed": 123456, "sectors_read": 9876543, "...": "..."}}},
    {"sample_index": 1, "timestamp": 1234598.89, "disks": {"nvme0n1": {"reads_completed": 123556, "...": "..."}}}
  ],
  "block_devices": {"source": "block_devices", "status": "ok", "started_at": "...", "ended_at": "..."},
  "iostat": {"source": "iostat", "status": "ok", "exit_code": 0,
             "parsed": {"disks": {"nvme0n1": {"r_per_s": 185000, "rkB_per_s": 740000, "avgqu_sz": 2.5, "await": 1.2, "util_percent": 99.5}}, "reports": 2},
             "raw": "..."},
  "pidstat": {"source": "pidstat", "status": "ok",
              "parsed": {"processes": [{"pid": 12345, "uid": "1000", "kbr_per_s": 0, "kbw_per_s": 0, "command": "python"}]}},
  "process_io_map": {"source": "process_io_map", "status": "ok",
                     "parsed": {"mappings": [{"pid": 12345, "path": "/data/x", "mount_point": "/data", "source": "192.168.1.10:/data", "fstype": "nfs4"}], "pid_count": 1}},
  "memory": {"source": "memory", "status": "ok", "parsed": {"memtotal": 27000000, "memavailable": 20000000, "cached": 5000000}},
  "df": {"source": "df", "status": "ok",
         "parsed": {"filesystems": [{"filesystem": "/dev/nvme0n1", "size": "3.5T", "used": "1.2T", "avail": "2.1T", "use_percent": "38%", "mounted_on": "/workspace", "inodes": "234M", "iuse_percent": "5%"}]}},
  "nfs": {"source": "nfs", "status": "ok",
          "parsed": {"client_stats_raw": "...", "mount_metrics": [{"mount_point": "/data", "ops": 12345, "rtt": 15, "execute": 20, "retrans": 0}]}},
  "readahead": {"/dev/nvme0n1": 256},
  "scheduler": {"/dev/nvme0n1": "none"},
  "availability": {"missing": [], "partial": [], "errors": []}
}
```

字段说明：

- `diskstats_sample`：两次采样，用于差值计算速率；`sectors_read` 单位为 512B sector。**只含主设备，不含分区**（已用 `/sys/class/block/<name>/partition` 过滤）。
- `mounts_provider`：记录挂载列表的采集状态与时间来源。只有 `ok`/`empty` 且 `started_at`/`ended_at` 落在顶层窗口内，才能判断是否存在网络挂载；缺失、无权限、命令失败或陈旧列表必须降级为证据不足。
- `iostat.parsed.disks`：结构化的 per-device 指标（`r_per_s`、`rkB_per_s`、`avgqu_sz`、`await`、`util_percent` 等）；全窗口聚合（-y 后保留全部真实区间报告，rate 算术平均、await IO 加权、util 保留 mean/max/p95/sample_count）。
- `pidstat.parsed.processes`：结构化的 per-process 指标（`pid`、`kbr_per_s`、`kbw_per_s`、`command`）。
- `process_io_map.parsed.mappings`：PID → path → mount → device 映射（R400 所需，需 `--pid`/`--path` 触发；无则 `status=unsupported`）。
- `df.parsed.filesystems`：空间与 inode 合并（`df -hP` + `df -iP`）。
- `nfs.parsed.mount_metrics`：`/proc/self/mountstats` per-op 统计的**窗内两次采样差值**（`windowing`/`avg_rtt_ms`/`avg_execute_ms`/`retrans`/`ops`，R200 性能证据）；无 NFS 挂载时 `status=unsupported`。单次累计值不反映本次 workload，差值才可作性能证据。
- `readahead`：单位为 512B sector（`blockdev --getra` 原始输出）。
- `availability`：`missing`（数据源缺失）/`partial`（unsupported/empty）/`errors`（permission/command_failed/parse_failed）三类汇总。
- `schema_version`：major.minor。minor 只增可选字段；analyzer 遇到未知 major 拒绝确定性分析。

## 3. 提问协议

### 3.1 总原则

- 每轮只问一个问题；不要用 "我先确认 3 点 / 4 点" 的批量问法。
- 优先用选择题；选择项使用用户语言，并提供 "不确定，先帮我看" 或 "使用默认值"。
- 用户已经提供的信息不要重复问。
- 不询问 `iostat`、`mount`、拓扑等采集器能自取的信息。
- 如果用户已有 Snapshot，跳过采集提问，直接进入 Snapshot 质量检查。

### 3.2 必问项（按顺序）

1. **问题场景**：数据加载慢 / checkpoint 慢 / 多实例互相拖慢 / 网络存储疑似慢。
2. **数据集形态**：大文件 / 海量小文件 / 混合（决定是带宽瓶颈还是元数据瓶颈）。
3. **数据存放位置**：本地盘 / NFS/CIFS / Lustre/GPFS / 对象存储挂载。
4. **是否允许只读采集**：决定走 Snapshot 还是用户手动提供输出。

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
