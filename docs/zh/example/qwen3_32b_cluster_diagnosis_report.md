# Qwen3-32B 集群快慢卡诊断报告

> 数据路径：`/workspace/Qwen3-32B/df536040f370_{651,654,657,664}_*_ascend_pt`（Rank 0–3）
> 分析时间：2026-08-19 ｜ 工具：msprof-analyze 8.5.2 集群分析 + 技能对比脚本 + advisor

## 分析范围与数据形态

- **集群规模**：4 卡（Rank 0–3），数据为 `ascend_pt`（Text + DB 双形态），profiling 覆盖 17 个 step
- **证据来源**：`cluster_analysis_output` 的 `slow_rank` / `cluster_time_summary`(ClusterStepTraceTime) / `hccl_sum` / `cann_api_sum` / `free_analysis` + 技能对比脚本 + 对慢卡 Rank 1 补跑的 `msprof-analyze advisor all`
- **说明**：`slow_link` 独立模块在当前 msprof-analyze 8.5.2 无独立 `-m`（并入 advisor），通信链路证据用 `ClusterCommunicationMatrix` 带宽补齐；已如实标注，不影响结论

---

## 问题 1：有无快慢卡？关键证据是什么？

**结论：存在快慢卡现象。慢卡为 Rank 1（最严重）和 Rank 2（次之）。**

**证据 1 — slow_rank 算法直接判定**（`slow_rank/cluster_analysis.db` 的 `SlowRank` 表）：

| Rank | slowAffectCount（影响其他卡的次数） |
|---|---|
| **1** | **137** |
| **2** | **129** |
| 0 / 3 | 未上榜 |

**证据 2 — 整步时间拆解不对称**（`ClusterStepTraceTime`，单位 ms）：

| Rank | Computing | Communication | Free（空闲） | Free 占比 | Stage |
|---|---|---|---|---|---|
| 0 | 333.8 | **447.9** | 632.4 | 44.7% | 1414.0 |
| **1** | 332.6 | **203.8** | **877.5** | **62.1%** | 1413.9 |
| **2** | 335.4 | **247.8** | **831.4** | 58.8% | 1414.6 |
| 3 | 334.2 | **445.0** | 636.1 | 44.9% | 1415.3 |

计算量四卡基本一致（~333ms），但 Rank 1/2 空闲显著偏高（+245ms / +199ms vs Rank 0/3），通信显著偏低（Rank 1 仅 203.8ms，不到 Rank 0/3 的一半）。

**证据 3 — 通信侧不对称**（`hccl_sum` HcclPerRankStats，allReduce 258 次/卡）：

- Rank 0/3：allReduce 总耗时 425.9 / 424.6ms，均值 1.65ms
- Rank 1/2：allReduce 总耗时 200.2 / 231.1ms，均值 0.78 / 0.90ms

即 Rank 0/3 在集合通信里**多等了约 224–225ms**。

**证据 4 — Host 同步等待不对称**（`cann_api_sum` CannApiSumRank，`aclrtSynchronizeEvent` 17 次/卡）：

| Rank | aclrtSynchronizeEvent 总耗时 | max |
|---|---|---|
| 0 | **329.9ms** | 133.2ms |
| **1** | **19.6ms** | 11.6ms |
| 2 | 285.7ms | 112.0ms |
| 3 | 308.7ms | 133.2ms |

Rank 1 的同步等待几乎为 0——它是"别人都在等它"的那张卡（伪快卡）。

---

## 问题 2：造成快慢卡的原因是什么？

**结论：Rank 1/2 是 Host 下发（CPU 调度/下发）瓶颈，属于"伪快卡"——不是计算慢，而是 CPU 下发慢导致其 NPU 饿死（Free 巨大），到达集合通信时其他卡已等待多时，故其通信瞬间完成。**

**证据 1 — 计算侧无劣化（排除计算型慢卡）**：`compare_op_stats.py Rank1 vs Rank0` 显示所有计算算子 count 完全一致、耗时几乎相同（MatMulV2、FusedInferAttentionScore 等 ratio 均在 0.96–1.03，总耗时差异 <0.5%）。设备算力没有问题。

**证据 2 — Host 侧 API 明显偏慢（`compare_api_stats.py Rank1 vs Rank0`）**：

| API（count 相同） | Rank1 总耗时 | Rank0 总耗时 | 差异 |
|---|---|---|---|
| aclnnInnerFusedInferAttentionScore (1088) | 130.1ms | 83.7ms | **+46.4ms（1.55x）** |
| aclrtRecordEvent (1819) | 62.7ms | 39.7ms | **+23.0ms（1.58x）** |
| launch (4168) | 276.8ms | 259.5ms | +17.3ms（1.07x） |
| aclrtMemcpyAsync (145) | 4.2ms | 2.2ms | **1.91x** |

注意设备侧 `FusedInferAttentionScore` kernel 两卡相同（18.6 vs 18.1ms），差异全部来自 Host 侧 API 处理/等待。

**证据 3 — free_analysis 直接命中下发间隙**：

- Rank 1 存在 **19.7ms 的 `node@launch` 间隙**（"Abnormal CANN layer: long time between two node@launch"），其余卡同类间隙仅 ~9ms
- Rank 2 存在 "Idle Pytorch layer: no task dispatched in 7339us"（Host 无任务下发）

**证据 4 — Rank 1 advisor 确认下发瓶颈**：

- 空闲 886.4ms 占 **62.3%**
- 可融合算子序列分析：**host 瓶颈耗时占比 0.9**（94 个有融合价值的序列，E2E 9775ms，NPU 仅 981ms）——大量算子下发被 Host 阻塞
- 附带发现：17 个通信算子**字节未对齐**、SDMA 通信 100% 为 <16MB 小包

**排除项 — 通信链路**：`ClusterCommunicationMatrix` 显示 HCCS 平均带宽 16.56 GB/s、LOCAL 38.16 GB/s，无慢链路信号；问题不在链路而在集合通信的等待与串行化。

---

## 问题 3：影响评估——拖慢了多少时间？

**结论：快慢卡问题每 step 额外损失约 245ms，占 1414ms step 的 ~17%。修复后单 step 可从 ~1414ms 降至 ~1170ms（吞吐提升约 17%）。**

**量化依据**：

1. **慢卡自身代价**：Rank 1 空闲 877.5ms vs 健康卡（Rank 0/3）632–636ms → **多饿死 245ms/step**
2. **其他卡的等待代价**：Rank 0/3 通信 447.9/445.0ms vs Rank 1 203.8ms → 其他卡在集合通信中**多等待 ~244ms/step**（同一现象的两面）
3. **同步等待代价**：Rank 0/2/3 的 `aclrtSynchronizeEvent` 合计 286–330ms，Rank 1 仅 20ms → 约 300ms 的 Host 阻塞差异
4. 由于 4 卡在每个 step 末尾强制同步（Stage 均为 ~1414ms），Rank 1 的下发延迟落在关键路径上，直接决定集群步长

> 保守表述：慢卡问题造成的损失为 **240–250ms/step（≈17%）**，这是"慢卡与健康卡"的差距；剩余收益空间请见下方重要提示。

**⚠️ 重要提示（影响优先级）**：即使修复慢卡，全集群 4 卡仍都有 45%–62% 的空闲时间（Rank 0/3 也有 632ms），且计算与通信重叠为 0（`overlapped` ≈ 0，`communication_not_overlapped` = 全部通信）。**全集群的计算-通信串行化/调度空闲是更大的问题**，快慢卡只是叠加其上的一层。

---

## 建议（按优先级）

1. **[P0] 修复 Rank 1/2 Host 下发瓶颈**：检查下发线程 CPU 绑核/亲和性、CPU 争抢与负载（4 进程是否挤在同一 NUMA/核上）、`node@launch` 19.7ms 间隙对应的同步点（`aclrtSynchronizeEvent` 调用链）；可参考 `mindstudio-cpu-binding` 技能做 CPU 绑核排查
2. **[P0] 通信字节对齐**：advisor 检出 17 个通信算子数据大小未对齐，建议调整通信数据量对齐（涉及 HCCL 配置/数据 shape）
3. **[P1] 减小小包通信**：allGather 单次仅 0.45MB（SDMA 100% <16MB），若为 ZeRO 类切分过细，可增大 batch/梯度累积，或内存允许时 ZeRO3→ZeRO2/1
4. **[P1] 提升计算-通信重叠**：当前 overlapped≈0，通信完全未被计算掩盖（这是全集群最大空闲来源），评估通信算子的 stream 调度与算子融合
5. **[P1] 动态 shape**：检测到 1 个动态 shape 算子，可关闭在线编译（`set_compile_mode(jit_compile=False)`）降低额外编译开销

## 验证方法

修改后重新采集同规模 profiling，对比：

- `step_trace_time.csv` / `ClusterStepTraceTime` 中 Rank 1/2 的 Free 占比是否回落到 ~45% 水平、Rank 0/3 通信是否从 448ms 回落到 ~204ms
- 观察 step 时长是否从 ~1414ms 降至 ~1170ms（吞吐提升 ~17%）
- 若只做 CPU 绑核调整，可先以 10 step 小规模验证，确认 `aclrtSynchronizeEvent` 等待从 300ms 级回落

**待验证**：本数据未开启调用栈（`with_stack: false`）与 `record_shapes`，无法定位到具体 Python 代码行；上述 Host 下发瓶颈的代码级根因（如数据预处理、锁竞争、tensor 搬运）需补采开启调用栈的 profiling 确认。
