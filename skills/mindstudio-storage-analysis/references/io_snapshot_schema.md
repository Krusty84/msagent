# IO Snapshot Schema（机器可校验契约）

本文件是 `mindstudio-storage-analysis` 的 IO Snapshot 数据契约的**字段说明**。契约分为两层：`scripts/collect_io_snapshot.py` 的 pydantic 模型（`IoSnapshot`、`ProviderResult`、`DiskStat`、`DiskStatSample`、`Availability`）负责 collector envelope 和自身生成字段；`scripts/analyze_io_snapshot.py` 的 `validate_analysis_request()` / `normalize_and_validate()` 负责 provider `parsed` 深层结构、数值与计数关系、时间窗、证据绑定和 profile 语义。完整契约以这两层实现及本说明共同为准。

> pydantic 模型允许 provider `parsed` 和扩展字段，因此其 JSON Schema 不是完整的分析语义契约。只调用 `IoSnapshot.model_validate()` 不能替代 analyzer 校验。依赖版本声明在 `requirements.txt`。

## 目录

- [1. 版本策略](#1-版本策略)
- [2. 顶层模型 IoSnapshot](#2-顶层模型-iosnapshot)
- [3. ProviderResult](#3-providerresult核心失败语义细分类)
- [4. DiskStat / DiskStatSample](#4-diskstat--diskstatsample)
- [5. Availability](#5-availability)
- [6. Provider parsed 结构](#6-各-provider-的-parsed-结构)
- [7. 校验方式](#7-校验方式)

## 1. 版本策略

- `schema_version` 格式 `"<major>.<minor>"`，当前 `1.4`。
- **minor** 只允许新增可选字段（向后兼容），分析器不拒绝。
- **major** 变更（删字段/改语义）必须 bump；分析器遇到未知 major 时**拒绝确定性分析**并返回明确错误（见 `analyze_io_snapshot.analyze_all`）。
- pydantic 模型对 `schema_version` 做 `^\d+\.\d+$` 格式校验；`collected_at` 为必填字段——空文档 `{}` 会被拒绝。
- 当前 `SUPPORTED_MAJOR = 1`。

### 1.4 相对 1.3 的变化（向后兼容）

- 顶层新增 `mounts_provider`，保存 `/proc/<pid>/mounts` 的 `ProviderResult`。`mounts` 仍保留为兼容的结构化列表，但 R200/R300 必须先验证 provider 状态及当前窗口时间来源；读取失败或陈旧列表不能再被解释为“没有网络挂载”。
- analyzer 要求动态证据落在由 `collected_at` 锚定的顶层 `window` 内；缺失、陈旧或错窗证据不能产生 high 结论。
- 外部指标执行语义范围校验：百分比必须在 0~100，速率、延迟、计数和累计值必须非负，所有 `*_sample_count` 必须为整数且不得大于 `sample_count`。
- `iostat` 的每个设备必须至少包含一项可用 util、队列、await 或速率指标；空对象或只有 `sample_count` 为解析失败。仅有单类字段不能产生高置信健康结论；R100 正向或负向 high 至少需要 10 秒实际证据窗口，以及 3 个同时覆盖 util 与触发判定的 queue/await 字段的有效样本。每设备 `sample_count` 及字段计数不得大于正整数 `iostat.parsed.reports`；缺少 `*_with_util_sample_count` 时共现关系视为未知，不得从两个独立计数推断。短窗或稀疏字段只能到 medium；`diskstats_sample` 的两个 counter 端点不满足三样本条件，无论间隔多长都不得产生 R100 high。
- 命令格式回退时，provider 的 `started_at/ended_at` 只描述最终被采用的采集尝试，不覆盖此前已丢弃的 JSON/文本尝试。

### 1.3 相对 1.2 的变化（均向后兼容）

- `nfs.parsed.mount_metrics` 解析真实内核标记 `per-op statistics`（旧版只认 `per-op:`）；
  保留**全量 + 元数据子集（GETATTR/LOOKUP/READDIR/...）+ 数据子集（READ/WRITE）** 三类累计与窗内差值，
  新增 `retrans_ratio`/`metadata_ops`/`avg_metadata_rtt_ms`/`avg_metadata_execute_ms`/`data_ops`/`data_retrans`/`data_retrans_ratio`/`avg_data_rtt_ms` 等字段（R200 重传率判据 + R300 远程元数据证据）。
- `process_io_map.parsed.mappings` 每项新增设备拓扑归一字段：
  `canonical_device`（与 iostat 整盘名对齐）、`major_minor`、`backing_devices`、`device_resolution`（sysfs/heuristic）。
  `/dev/sda1`→`sda`、`/dev/dm-0`→`dm-0`、`/dev/mapper/*`→需 sysfs 解析（无 /sys 标 heuristic-unresolved-mapper）。
- `process_io_map.parsed` 记录 `observation_samples`；每条 mapping 记录 `boot_id`/`pid_starttime_ticks`/`first_seen`/`last_seen`/`observation_count`。R400 high 要求每个争抢 PID 的同一进程身份和映射实际观测至少两次，且映射区间至少有 1 秒公共交集；provider 宽窗口不能替代映射级时间证据。mountinfo 与 sysfs backing topology 缓存不得跨观测，带 `mnt_id` 的 FD 只能使用精确 mount-id 绑定。

### 1.2 相对 1.1 的变化（均向后兼容）

- `ProviderResult.status` 收紧为 `Literal`（拒绝非法状态字符串）。
- `nfs.parsed.mount_metrics` 改为**窗内两次采样差值**（`windowing`/`avg_rtt_ms`/`avg_execute_ms`/`retrans`/`ops`/`sum_rtt_ms`/`sum_execute_ms`/`bytes_*_delta`），反映本次 workload 窗口而非累计值。
- `nfs.parsed` 新增 `client_calls_delta` / `client_retrans_delta`（`/proc/net/rpc/nfs` 窗内差值）。
- `process_io_map.parsed` 新增 `pid_tree`（`--pid` 进程树，含 `role`）与 `partial`；`mappings` 每项新增 `path_relevant`。
- 块设备覆盖包含 `dm-`/`md`（device-mapper / soft RAID），并通过 sysfs 身份而非设备名前缀白名单识别 `mmcblk`、`rbd`、`nbd` 等合法设备。
- 块设备前缀解析改用 mountinfo 最长前缀匹配（支持含空格/Unicode 的挂载点，八进制 `\040` 自动还原）。

## 2. 顶层模型 `IoSnapshot`

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | str | 见上，`"1.4"` |
| `collected_at` | str | 采集开始时间（ISO 8601 带时区） |
| `host` | dict | `hostname` / `kernel` / `platform` |
| `duration_seconds` | float | 动态指标采样窗口长度 |
| `window` | dict | `{start, end}` 全局采集窗口 |
| `target` | dict | `{pid, path}` 用户指定的目标（用于 R400 映射）；当 `--pid --path` 解析了进程命名空间内的符号链接时，可附带原始 CLI 路径 `requested_path` |
| `mounts` | list[dict] | `/proc/mounts`（排除伪文件系统），每项 `device/mount_point/fstype/options` |
| `mounts_provider` | ProviderResult | 挂载列表采集状态与原始/结构化证据；R200/R300 的挂载来源契约 |
| `diskstats_sample` | list[DiskStatSample] | 两次 `/proc/diskstats` 采样（仅主设备，不含分区） |
| `block_devices` | ProviderResult | 块设备采样 provider（状态/可用性；实际两次样本在 `diskstats_sample`） |
| `iostat` | ProviderResult | iostat provider |
| `pidstat` | ProviderResult | pidstat provider |
| `process_io_map` | ProviderResult | PID→设备映射 provider（R400） |
| `memory` | ProviderResult | /proc/meminfo provider |
| `df` | ProviderResult | df（空间+inode）provider |
| `nfs` | ProviderResult | NFS provider |
| `readahead` | dict[str,int] | `/dev/<dev>` → readahead（512B sector） |
| `scheduler` | dict[str,str] | `/dev/<dev>` → IO 调度器 |
| `availability` | Availability | 数据可用性汇总 |
| `device_baselines` | dict[str,dict]（可选） | 设备名到正有限数值 `max_read_mbps` / `max_write_mbps` / `max_iops` 的用户规格；非法、布尔、零/负数和未知字段会被 analyzer 丢弃并记录 validation error，不得为 R100 high 背书 |

## 3. `ProviderResult`（核心：失败语义细分类）

每个数据源统一用此结构表达。**不再使用布尔 `available`**——这是相对旧版的关键修正。

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` | str | 数据源名（如 `iostat`） |
| `status` | str | 见下表 |
| `started_at` | str | 该 provider 采集开始时间 |
| `ended_at` | str | 该 provider 采集结束时间 |
| `exit_code` | int\|null | 外部命令退出码（文件读取为 null） |
| `stderr` | str | 外部命令 stderr |
| `error` | str | 错误描述 |
| `raw` | str | 原始输出（审计用） |
| `parsed` | Any | 结构化解析结果（规则计算用） |

### `status` 取值

| status | 含义 | 对规则的影响 |
|---|---|---|
| `ok` | 成功且有输出 | 正常参与判定 |
| `missing` | 命令/文件不存在 | 该 provider 证据缺失，对应规则降级 |
| `permission_denied` | 无权限 | 同上，但归入 `availability.errors` |
| `command_failed` | 命令存在但非零退出 | **绝不**当成有效采集；归入 `errors` |
| `parse_failed` | 输出存在但无法解析 | 同上 |
| `empty` | 命令成功但无输出，或格式已识别但无受支持的真实采集对象 | 归入 `availability.partial` |
| `unsupported` | 平台/工具不支持 | 归入 `availability.partial`（如无 NFS 挂载、无 Lustre 工具） |

## 4. `DiskStat` / `DiskStatSample`

`DiskStatSample`：`{sample_index, timestamp, disks: {name: DiskStat}}`

`DiskStat` 字段（对应 `/proc/diskstats` 内核字段）：

| 字段 | 含义 |
|---|---|
| `name` | 设备名 |
| `reads_completed` / `writes_completed` | 完成的读写数 |
| `reads_merged` / `writes_merged` | 合并的读写数 |
| `sectors_read` / `sectors_written` | 读写的扇区数（512B/sector） |
| `time_reading_ms` / `time_writing_ms` | 读写耗时（ms） |
| `io_in_progress` | 当前在途 IO |
| `time_io_ms` | 总 IO 忙时（ms），用于算 %util |
| `weighted_time_io_ms` | 加权 IO 时间（ms），用于算 avgqu-sz |

**分区过滤**：`diskstats_sample` 只含物理/逻辑主设备，不含分区。判定逻辑见 `collect_io_snapshot._is_real_block_device`：优先读 `/sys/class/block/<name>/partition`，无该文件时仅过滤无歧义的常见分区格式和 loop/ram/zram 伪设备；未知主设备名予以保留，避免漏掉 `mmcblk`、Ceph `rbd` 和 `nbd`。

## 5. `Availability`

| 字段 | 类型 | 说明 |
|---|---|---|
| `missing` | list[str] | status=missing 的 provider 名 |
| `partial` | list[str] | status=unsupported/empty 的 provider（带说明） |
| `errors` | list[str] | status=permission_denied/command_failed/parse_failed 的 provider（带说明） |

## 6. 各 provider 的 `parsed` 结构

| provider | parsed 结构 |
|---|---|
| `mounts_provider` | 与顶层 `mounts` 一致的挂载对象列表；失败状态时可以为 null |
| `iostat` | `{disks: {name: {r_per_s, w_per_s, rkB_per_s, wkB_per_s, avgqu_sz, await, r_await_ms（IO 加权）, w_await_ms, util_percent（均值）, util_max, util_p95, sample_count, util_sample_count, avgqu_sz_sample_count, await_sample_count, avgqu_sz_with_util_sample_count, await_with_util_sample_count, ...}}, reports, source_format}`（**全窗口聚合**：速率算术平均、await 按 IO 数加权、util 保留 mean/max/p95；`*_sample_count` 记录字段样本数，`*_with_util_sample_count` 记录该字段与 util 在同一报告中的共现数） |
| `pidstat` | `{processes: [{pid, uid, kbr_per_s, kbw_per_s, kbccwd_per_s, command, sample_count, active_sample_count}], reports, source_format}`；三个计数字段必须是 JSON 整数，`reports > 0` 且 `0 <= active_sample_count <= sample_count <= reports`。`active_sample_count` 统计读或写速率至少 100 KiB/s 的真实报告数，`Average:` 汇总块不计入 `reports`。R400 high 要求每个候选 PID 至少 3 个活跃样本且覆盖超过半数报告。 |
| `df` | `{filesystems: [{filesystem, size, used, avail, use_percent, mounted_on, inodes?, iuse_percent?}]}` |
| `memory` | `{memtotal, memavailable, cached, buffers, dirty, ...}`（/proc/meminfo 小写键） |
| `nfs` | `{mount_metrics: [{mount_point, source, fstype, windowing, ops, transmissions, retrans, retrans_ratio, major_timeouts, sum_rtt_ms, sum_execute_ms, avg_rtt_ms, avg_execute_ms, metadata_ops, avg_metadata_rtt_ms, avg_metadata_execute_ms, data_ops, data_transmissions, data_retrans, data_retrans_ratio, avg_data_rtt_ms, avg_data_execute_ms, bytes_read_delta, bytes_write_delta}], client_calls_delta?, client_retrans_delta?, nfsiostat_raw?}`（**窗内两次采样差值**；解析真实 `per-op statistics` 段，保留全量/元数据/数据三类 per-op 子集） |
| `process_io_map` | `{mappings: [{pid, boot_id, pid_starttime_ticks, role, path, mount_point, source, fstype, canonical_device, major_minor, backing_devices, device_resolution, path_relevant, first_seen, last_seen, observation_count}], observation_samples, pid_count, pid_tree: [{pid, boot_id, pid_starttime_ticks, role, parent_pid?}], partial}`；仅由 `--pid` 采集，`--path` 仅收窄范围；`descendant` 必须携带能沿链回到显式目标 PID 的直接 `parent_pid`，否则 analyzer 不把它纳入目标作用域。进程树最多 256 项，每 PID 最多保留 256 条 FD 映射。指定 path 时在最多 1 秒扫描预算内优先保留相关 FD；数量或时间截断均写入 `partial`。`observation_samples` 是正 JSON 整数，`observation_count` 是不大于它的非负 JSON 整数。 |

**NFS metric 身份绑定契约**：`nfs.parsed.mount_metrics` 的每条指标必须携带 `source` + `mount_point` + `fstype`（collector 从当前 mount 的 device/fstype 填充）。analyzer 按 `(source, mount_point, fstype)` 三元组与当前 `mounts` 强绑定后才允许 R200/R300 的 high 结论——`source` 缺失只做路径弱匹配，不得 high；`nfs`/`nfs4` 视为兼容身份。这避免同路径不同 source（挂载替换/namespace 混入）的旧 metric 被拼接为因果链。

`mounts_provider` 与 `nfs` 必须各自落在 `snapshot.window` 内，并且两个 provider 窗口相互重叠或间隔不超过 2 秒。该 2 秒容差用于 collector 在动态 NFS 采集结束后立即读取挂载表的顺序；窗口两端相互错开的身份和性能数据不得拼接为 high 证据。

## 7. 校验方式

```python
import json
from scripts.collect_io_snapshot import IoSnapshot
from scripts.analyze_io_snapshot import validate_analysis_request

data = json.load(open("io_snapshot.json"))
IoSnapshot.model_validate(data)  # collector envelope 非法时抛 ValidationError
snap, profile, errors, profile_errors, fatal = validate_analysis_request(data)
if fatal or errors or profile_errors:
    raise ValueError(f"fatal={fatal}; snapshot={errors}; profile={profile_errors}")
```

测试见 `evals/test_collect_io_snapshot.py` 与 `evals/test_analyze_io_snapshot.py`；完整行为矩阵见 `evals/cases.yaml`。
