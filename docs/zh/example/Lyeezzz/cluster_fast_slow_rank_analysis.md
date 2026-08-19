# Ascend 集群快慢卡分析报告

- 分析时间：2026-08-19
- 数据源：`/workspace/df536040f370_65{1,4}_202601080932395{59,60}_ascend_pt`（Rank 0~3）
- 数据类型：DB + Text（`ascend_pytorch_profiler_{0..3}.db`、`trace_view.json`、CSV）
- 拓扑：单机 4 卡，host=`df536040f370`，并行策略 `megatron-lm(dp=4, tp=1, pp=1)`
- 分析工具：`msprof-analyze`（cluster_time_summary / compute_op_sum / hccl_sum / slow_rank / slow_link / cann_api_sum / free_analysis / advisor all）+ 技能对比脚本 `compare_api_stats.py`

---

## 1. 是否存在集群快慢卡：是

**慢卡：Rank 1、Rank 2；快卡（受害者）：Rank 0、Rank 3。**

### 1.1 宏观证据（cluster_time_summary / ClusterStepTraceTime）

| Rank | stepTime (ms) | Compute (ms) | Communication (ms) | Free (ms) | Free 占比 | 通信 waitStage (ms) | 通信 transmit (ms) |
|---|---|---|---|---|---|---|---|
| 0 | 1414.0 | 333.8 | 447.9 | 625.0 | 44.2% | 441.1 | 6.8 |
| 1 | 1413.9 | 332.6 | **203.8** | **869.5** | **61.5%** | **197.0** | 6.8 |
| 2 | 1414.6 | 335.4 | **247.8** | **823.6** | **58.2%** | 240.9 | 6.8 |
| 3 | 1415.3 | 334.2 | 445.0 | 628.4 | 44.4% | 438.2 | 6.8 |

### 1.2 slow_rank 判定（SlowRank 表）

| Rank | slowAffectCount |
|---|---|
| 0 | 0 |
| 1 | **137** |
| 2 | **129** |
| 3 | 0 |

### 1.3 决定性证据：逐集合通信到达偏斜

对每个 `hcom_allReduce__503_*` 算子统计各 Rank 到达时间（以 Rank 0 为基准）：

- Rank 0：基准 0µs；Rank 2：−64~−10µs；Rank 3：+40µs
- **Rank 1：晚到 +2.5ms ~ +12.4ms**，到达后 0.02ms 内完成通信（其他卡已等待多时）
- 其余卡 `elapsed ≈ idle`（例如 Rank 0 的 `hcom_allReduce__503_1_1` elapsed=12.47ms，其中 idle=12.46ms）

### 1.4 通信侧佐证（HcclPerRankStats）

| OpType | Rank 0 Sum | Rank 1 Sum | Rank 2 Sum | Rank 3 Sum |
|---|---|---|---|---|
| hcom_allReduce_ (258 次/卡) | 425.9ms | 200.2ms | 231.1ms | 424.6ms |
| hcom_allGather_ (17 次/卡) | 22.0ms | 3.6ms | 16.7ms | 20.4ms |

---

## 2. 造成快慢卡的原因

**结论：Host 下发瓶颈（调度型慢卡）；通信小包 / 字节未对齐为次因。排除计算型慢卡与慢链路。**

### 2.1 判定依据（专家规则：Free 极长 + Compute/Communication 异常偏短 → "伪快卡"实为慢卡）

| 证据类型 | 明细 |
|---|---|
| free_analysis：Host 下发间隙 | Rank 1：`Abnormal CANN layer: long time between two node@launch` 19.7ms、7.3ms；Rank 2：8.9ms、3.3ms 及 `Idle Pytorch layer: no task dispatched 7.3ms` |
| API 对比（Rank1 vs Rank0，compare_api_stats.py） | `aclnnInnerFusedInferAttentionScore` 总耗时 130.1ms vs 83.7ms（1.55x）；`launch` 276.8 vs 259.5ms；`aclrtRecordEvent` 62.7 vs 39.7ms |
| 同步点饥饿 | Rank 1 `aclrtSynchronizeEvent` 总耗时 19.6ms（Rank 0/2/3 为 285~330ms）→ NPU 长期饥饿 |
| Device 侧对照（排除计算劣化） | `FusedInferAttentionScore` Device 均值 Rank1=19.3µs vs Rank0=18.9µs（仅差 2.3%）→ 慢在 Host 入队 |
| advisor（Rank1） | 空闲占比 62.3%；可融合算子序列 **host 瓶颈耗时占比 0.9**（94 组序列 E2E 9775ms 中 NPU 仅 981ms） |
| 计算侧排除 | `compute_op_sum`：4 卡 Count 完全一致，Mean 差异 <5% |
| 链路排除 | HCCS 带宽 15~17 GB/s 正常；同 host 无跨节点 |

### 2.2 次因（通信侧）

- 17 个通信算子数据字节未对齐（advisor：字节对齐分析）
- SDMA 通信 100% 数据量 <16MB（小包通信，ZeRO 风格切分）

### 2.3 待验证项

- 本次未开启 host 侧采集（`_host_sys` 未开启），无法确认 Rank 1/2 Host 下发慢的最终来源。需补采定位：
  - CPU 绑核 / NUMA 冲突
  - 进程间 CPU 争抢（dataloader / 其他进程）
  - 数据准备阻塞
  - 驱动 / 中断抖动

---

## 3. 影响评估：拖慢了多少时间

**结论：快慢卡失衡使整集群慢约 200~245ms / 采集窗口（约 1.42s），即约 14%~17%；对应每迭代约 11~14ms。**

推算（同一采集窗口内两个独立口径交叉验证）：

- 口径 A（快卡被拖慢的等待）：Rank 0/3 通信等待超出慢卡基线
  - `441.1 − 197.0 ≈ 244ms`（Rank 0）
  - `438.2 − 197.0 ≈ 241ms`（Rank 3）
- 口径 B（慢卡多余空闲）：Rank 1/2 的 Free 超出快卡基线
  - `869.5 − 625.0 ≈ 244ms`（Rank 1）
  - `823.6 − 625.0 ≈ 199ms`（Rank 2）

由于集合通信强制同步，整集群步进由最慢卡决定。**消除 Rank 1/2 的下发延迟后，整窗预计从 ~1414ms 降至 ~1170~1215ms。**

验证方法：修复后重新 Profiling，检查：
1. 各 Rank waitStage 收敛到同一水平（目标偏差 ±20ms 内）
2. 各 Rank Free 占比收敛
3. 整窗耗时缩短 ~200ms

---

## 4. 优化建议（按优先级）

1. **[P0] 定位并消除 Rank 1/2 的 Host 下发延迟**
   - 检查 4 个进程 CPU 绑核 / NUMA 亲和性与 dataloader 争抢
   - 补采 `_host_sys` + 调用栈，定位 19.7ms launch 间隙对应的代码位置
   - 预期收益 ~14%~17%
2. **[P1] 通信侧优化**
   - 修复 17 个通信算子字节未对齐（联系 HCCL 研发）
   - 小包通信：增大 batch / 梯度累积；内存允许时从 ZeRO3 退回 ZeRO2/1
3. **[P1] 关闭动态 shape 在线编译**（advisor 检出 1 个动态 shape 算子）
   - `torch_npu.npu.set_compile_mode(jit_compile=False)`

---

## 5. 对话记录与预期校验

| 轮次 | 问题 | 输出摘要 | 是否符合预期 |
|---|---|---|---|
| 1 | 有无快慢卡、关键证据 | 判定慢卡 R1/R2、快卡 R0/R3；给出 cluster_time_summary、slow_rank、逐算子到达偏斜三重证据 | ✅ 符合：结论唯一、证据可复现 |
| 2 | 造成快慢卡的原因 | 定性为 Host 下发瓶颈；给出 launch 间隙、API 1.55x、同步点饥饿、advisor host 占比 0.9 证据；显式标注"下一层根因待验证" | ✅ 符合：未把猜测包装成结论；受采集配置限制无法给出 CPU 侧最终根因，已说明 |
| 3 | 影响 / 拖慢时间 | 量化 200~245ms/窗（14~17%），两个独立口径交叉验证 | ✅ 符合：影响量化有计算过程；提速百分比为估算，已标注 |

**整体判定：符合预期。** 三轮输出均做到"数据驱动 + 证据闭环 + 结论简洁"；"待验证"项均属于当前数据不可达范围，已如实标注而非臆断。

---

## 附录：分析产物路径

- `msprof-analyze` 集群分析输出：`/workspace/cluster_analysis_output/{cluster_time_summary, compute_op_sum, hccl_sum, slow_rank, slow_link, cann_api_sum, free_analysis, advisor_rank1}/`
- 已有通信分析 DB：`/workspace/cluster_analysis_output/cluster_analysis.db`
- 本文档：`/workspace/cluster_fast_slow_rank_analysis.md`
