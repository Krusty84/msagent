# 存储问题根因手册

本文件是 `mindstudio-storage-analysis` 的根因桶详表，按 "症状 → 证据 → 建议" 组织。一个真实案例可能同时命中多个桶，**不要强行归一为单一根因**。

每条规则包含：

- **触发信号**：什么证据会触发该桶。
- **判定要点**：如何确认这是该桶而非其他。
- **常见误判**：容易被误判为该桶的情况。
- **典型建议**：保守 / 进阶方向（具体执行需用户确认，见 SKILL.md 安全边界）。

---

## R000 信息不足

### 触发信号

- Snapshot 缺少 `diskstats` 或 `iostat`。
- 未提供挂载信息（无法判断是否网络存储）。
- 无 profiler 数据，且无法判断 IO 压力是否传导到 NPU。

### 判定要点

先把可分析的部分做完，不可判断项列入 "信息缺口"，不强行下结论。

### 处理

- 指导用户补采集（优先补 `iostat -xz 1 10` 与 `cat /proc/mounts`）。
- 有 profiler 时优先补上交叉验证。
- 在报告中明确哪些结论无法下定。

---

## R100 吞吐 / IOPS 饱和（设备忙）

### 触发信号

- `%util` 长期接近 100%（HDD）/ > 90%（SSD）。
- NVMe：`%util` 含义弱化，需结合 `aqu-sz`（队列深度）与 `r/s w/s`。
- `rMB/s` / `wMB/s` 接近设备规格带宽。
- `r/s + w/s` 接近设备 IOPS 上限。
- `r_await` / `w_await` 显著高于设备正常水平（SSD < 5ms，HDD < 20ms）。
- `aqu-sz` 持续高。

### 判定要点

分两种亚型：

- **带宽饱和型**（大文件）：`rMB/s` 接近规格带宽，`r/s` 不算特别高，单次 IO 大。
- **IOPS 饱和型**（小文件）：`r/s` 极高但 `rMB/s` 不高，单次 IO 小，`r_await` 抬升。

两种亚型的优化方向不同：带宽型看 `readahead` / 大块 / 本地盘；IOPS 型看合并小文件 / 减少 IO 次数。

### 常见误判

- NVMe 上 `%util` 高但吞吐远未到上限 → 这是 NVMe 多队列下 `%util` 的已知弱点，**不能单凭 `%util` 判饱和**，必须结合 `r/s`、`r_await`、`aqu-sz`。
- 单设备 `%util` 高但实际数据在另一块盘 → 确认数据集路径所在设备是否就是这块盘。

### 典型建议

保守（代码 / 单次运行）：

- DataLoader 增大 `prefetch_factor`、开启 `persistent_workers`。
- 大文件改用 `mmap` 读取。
- 热数据（权重 / 高频数据集）预拷到本地盘。
- 减少不必要的 checkpoint 全量保存（增量 / 按需）。

进阶（需 root / remount，**仅建议，需确认**）：

- 增大 `readahead`：`blockdev --setra <sectors> /dev/<dev>`（回滚：恢复原值，默认常为 256）。
- 调整 page cache 策略。
- 把数据集分片到多块物理盘（增加并行度）。

验证指标：`rMB/s` 提升 / `r_await` 下降 / NPU 空泡下降。

---

## R200 网络存储 / 挂载延迟

> **两层判定（重要）**：本规则区分 (a) 仅"识别为网络挂载"（只说明类型，不构成瓶颈）和 (b) "确认网络存储瓶颈"（必须有 RTT/execute/retrans/吞吐等性能证据）。只有 (a) 而无 (b) 的证据时，置信度为低，不得确定性命中 R200。

### 触发信号

- 识别层：`/proc/mounts` 显示 fstype 为 `nfs` / `nfs4` / `cifs` / `lustre` / `gpfs` / `beegfs` / `fuse.*`。
- 确认层（性能证据，缺一不可判瓶颈）：
  - NFS：`/proc/self/mountstats` 中 per-mount 的 RTT / execute 偏高，或 `/proc/net/rpc/nfs` 显示重传增多，`nfsiostat` 显示 ops/s 低、RTT 高。
  - Lustre：`lctl get_param osc.*.stats` 显示高 `read`/`write` 延迟，OST 利用率高。
  - 首次访问慢、缓存预热后明显变快（强网络存储特征，配合 RTT 证据使用）。
  - 挂载选项含 `hard` + 没有合理超时，导致偶发卡死。

### 判定要点

确认问题确实来自网络往返而非本地盘本身：

- 对比本地盘路径与网络挂载路径上的同样负载，看延迟差异（对照实验）。
- **优先使用 `/proc/self/mountstats` 的 per-mount RPC RTT/execute/retrans**，而不是把本地块设备的 `r_await` 直接归到网络挂载点（`r_await` 是块设备层指标，对 NFS 等远程文件系统语义不准确）。

### 常见误判

- FUSE 挂载（如对象存储挂载）慢，误以为是本地盘问题 → 必须看 fstype。
- 网络抖动导致的偶发慢，误判为常态 → 需要重传计数与多次采样判断持续性。
- 仅识别为 NFS 挂载就断言是网络存储瓶颈 → 必须有 RTT/execute/retrans 性能证据（见两层判定）。

### 典型建议

保守（代码 / 单次运行）：

- 热数据 / 权重预拷到本地盘（启动时拉一次，之后本地读）。
- 小文件合并为 shard 后整体下载（避免海量小文件走网络 RPC）。
- 增大本地 page cache 依赖（让第二次访问走缓存）。

进阶（需 root / remount，**仅建议，需确认**）：

- 挂载选项：`noatime,nodiratime`（减少 atime 更新的元数据 RPC）。
- NFS 客户端：调大 `actimeo` / `acregmin` / `acdirmin`（属性缓存，减少 getattr RPC）。
- NFS：`rsize` / `wsize` 调大（如 1048576）。
- 引入本地缓存层（如 `fscache` / 商业缓存）。

验证指标：`r_await` 下降、首次访问时间下降、NPU 空泡下降。

---

## R300 元数据 / 小文件 / 远程访问开销

### 触发信号

- `r/s` 极高但 `rMB/s` 不高（典型小文件：每次读很少字节但 IO 次数多）。
- `df -i` 的 inode 使用高，或数据集目录下文件数量巨大。
- open/stat/readdir 密集（strace 或 `pidstat` 系统调用比例高）。
- cache 命中低：第二次访问同一批数据不显著变快（`Cached` 不足或被 evict，或文件元数据每次重新走网络）。
- 网络存储上尤其明显：每个小文件对应多次元数据 RPC。

### 判定要点

区分三种亚型：

- **纯小文件读吞吐**：`r/s` 高、单 IO 小、`r_await` 抬升。
- **元数据密集**：`open`/`stat`/`getdents` 多，数据量小，瓶颈在 inode / 目录项遍历。
- **cache miss**：`Cached`/`MemAvailable` 不足，热数据放不下，反复从盘读。

### 常见误判

- 把 CPU 预处理慢当成 IO 慢 → 看 `iostat` 的 `%util`（设备级，**不是** `pidstat -d` 的字段）与 `pidstat -u` 的 CPU；磁盘不忙而 CPU 忙，是预处理问题（交给 `mindstudio-cpu-binding`）。注意 `pidstat -d` 只给进程级 kB_rd/s/kB_wr/s，不含 `%util`。
- 把 `ls` 慢当成数据加载慢 → `ls` 是元数据操作，反映的是元数据开销，与实际读吞吐是两回事。

### 典型建议

保守（代码 / 单次运行）：

- 海量小文件合并为 shard：TFRecord / WebDataset / parquet / LMDB。
- 用 `mmap` + 索引文件，减少 `open`/`read` 系统调用次数。
- 避免每 step 全量遍历目录，预先生成文件列表索引。
- 增大 page cache（让热元数据 / 热数据常驻内存）。

进阶（需 root / remount，**仅建议，需确认**）：

- 挂载 `noatime,nodiratime`（减少 atime 元数据写）。
- 增大目录项 / inode cache（`vm.vfs_cache_pressure` 调小）。

验证指标：`r/s` 下降、系统调用比例下降、第二批访问明显变快（cache 命中提升）。

---

## R400 多 rank / 多 worker / 多实例 IO 干扰

### 触发信号

- `pidstat -d` 显示多个进程（多个 rank / 多个 DataLoader worker）有 IO 活动，且 `iostat` 显示它们访问的同一设备 `%util` 接近满（`pidstat -d` 本身不含 `%util`，需结合 `iostat` 与 PID→设备映射）。
- 单卡跑没问题、加卡就慢，且慢在数据加载阶段而非计算阶段。
- 不同 rank 的 step time 方差大，慢的 rank 对应的 IO wait 高。
- checkpoint 加载阶段多实例同时读同一份权重文件，磁盘被打满。
- 同一物理盘被多个数据集 / 多个任务共享（`findmnt` + 数据集路径交叉确认）。

### 判定要点

关键判据：**并行度增加 → 设备饱和 → 单 worker 实际可用吞吐下降**。如果是这种情况，单纯加 `num_workers` 反而更慢。

### 常见误判

- 把 "加卡变慢" 一律归为通信问题 → 必须看是不是数据加载段变慢（`step_trace_time` 的 Gen / 数据段）。
- 把 `num_workers` 调到等于卡数 × 大数 → HDD 上 worker 过多反而互相抢盘，总吞吐下降。

### 典型建议

保守（代码 / 单次运行）：

- `persistent_workers=True`（避免每 epoch 重新 fork 与重新 warm up 文件表）。
- 控制并发 worker 总数与磁盘并行度匹配：HDD 通常每盘 2~4 worker，SSD/NVMe 可更高。
- 每 rank / 每 worker 读独立 shard，避免争抢同一文件（用 rank 做分片）。
- checkpoint 分散存储或预拷到各节点本地盘（避免启动时全集群读同一份）。
- 把不同数据集分到不同物理盘。

进阶（架构级）：

- 用共享内存 / RAM disk 缓存热数据。
- 引入专用数据加载服务 / 预取 pipeline。

验证指标：多 rank step time 方差下降、`%util` 下降或更均衡、加卡后吞吐接近线性扩展。

---

## R500 NPU 传导链未成立（优先级修正）

### 触发信号

- Host IO 压力大（R100~R400 命中），但 profiler 显示：
  - device `Free` 比例低（NPU 不空泡）。
  - `step_trace_time` 的空泡不在数据加载阶段。

### 判定要点

这个桶不是新问题，而是**优先级修正**：把 "看起来是 IO 问题但 NPU 不受影响" 的 case 降级。

### 处理

- 明确告知：IO 压力存在但被计算掩盖，当前不是关键瓶颈。
- 记录为未来优化点（当计算优化到一定程度后，IO 可能成为新瓶颈）。
- 建议把分析重心交给 `ascend-computation-analysis` / `ascend-schedule-analysis`。

### NPU 传导链的置信度阶梯（重要）

确定性 analyzer 不重建 profiler 时间线，只读取已采集指标。动态 profile 必须带合法 `profile_window.start/end`，且至少 50% 的 profiler 窗口与 Snapshot.window 重叠；否则丢弃动态指标并报告 validation error。传导链置信度：

- **low**：仅 Host IO 异常，无 profiler 数据。
- **medium（封顶）**：Host IO 异常 + device Free 高，但缺同窗相关性 / 对照实验证据。只能报告"可能传导"，**不得声称"已传导"**。
- **high**：agent 从同一 workload 的 profiler 时间线核验后提供严格类型的 `conduction_evidence`（JSON boolean `io_npu_overlap_observed=true`），或受控实验得到 `controlled_experiment.result="improved"`。

同窗重叠证据需 profiler 时间线（属 `ascend-computation-analysis` 领域）；本 skill collector 只保证 Host IO 带真实时间窗，NPU 侧由 agent 跨 skill 交叉验证后回填。`npu-smi` 的瞬时 AICore 利用率或设备空闲状态不构成 R500 传导证据。

验证指标：先优化计算 / 调度后，重新评估 IO 是否成为新瓶颈。

---

## 跨桶组合模式

真实案例常见的组合：

| 组合 | 典型场景 | 主攻方向 |
|---|---|---|
| R200 + R300 | 网络挂载上跑海量小文件 | 合并 shard + 属性缓存（双管齐下） |
| R100 + R400 | 多 worker 同时读本地盘打满 | 控制并发 + shard 分片 |
| R300 + R400 | 多 worker 各自遍历海量小文件 | 文件列表索引 + 独立 shard |
| R100~R400 + R500 | IO 压力大但 NPU 不空泡 | 先降级，优化计算侧 |

组合模式说明：**不要只命中一个桶就停止分析**，应逐桶扫描证据。
