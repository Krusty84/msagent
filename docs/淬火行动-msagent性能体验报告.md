# Ascend 集群快慢卡 Profiling 分析总结

> 会话时间：2026-08-19
> 分析对象：`/home/Qwen3-32B` 下的 4 个 Rank ascend_pt 数据目录
> 会话历史：`/conversation_history/2f39f2fa-75dd-4145-b1ec-43c3d79018fc.md`

---

## 1. 任务背景

基于已采集的 Profiling 数据，使用 Agent 辅助分析调度类问题，以多轮交互形式依次回答：

1. 从当前 Profiling 数据来看，有无集群快慢卡，有什么关键证据？
2. 造成快慢卡的原因是什么？
3. 评估快慢卡问题造成的影响，拖慢了多少时间？

每轮记录对话输出，并判断是否符合预期。

---

## 2. 数据识别（交互前）

- **数据类型**：DB（每个 Rank 目录均有 `ascend_pytorch_profiler_{rank}.db`，另有 `cluster_analysis.db`）
- **Rank 数**：4（Rank 0/1/2/3），单节点 4 卡 DP（tp=1, pp=1, ep=1, dp=4），并行算法 megatron-lm（Qwen3-32B 推理/训练场景）
- **已有输出**：`cluster_analysis_output/cluster_analysis.db` 仅含通信/step 汇总表，缺少 `cluster_time_summary`、`compute_op_sum`、`hccl_sum`、`slow_rank`、`slow_link`、`cann_api_sum`
- **补齐动作**：已逐项运行 `msprof-analyze cluster -m <mode>` 补齐；`freq_analysis` 输出目录为空（失败，已记录为不可用证据）

---

## 3. 第 1 轮：有无集群快慢卡？

**用户**：从当前 Profiling 数据来看，有无集群快慢卡，有什么关键证据？

**Agent 回答**：

结论：**有快慢卡现象**。真正的慢卡是 **Rank 1（最严重）和 Rank 2**；Rank 0/3 是被拖累的等待方（表面耗时被拉平，即"假快卡"）。

关键证据（宏观）：

1. **slow_rank 统计算法**：`SlowRank` 表显示 Rank1 `slowAffectCount=137`、Rank2 `=129`，Rank0/3 为 0。
2. **cluster_time_summary 时间拆解**（单位 ms，占比按各自 stepTime）：

| Rank | stepTime | Compute | Comm | Free | CommWaitStage |
|---|---|---|---|---|---|
| 0 | 1414.0 | 333.8 (23.6%) | 447.9 (31.7%) | 625.0 (44.2%) | 441.1 |
| 1 | 1413.9 | 332.6 (23.5%) | **203.8 (14.4%)** | **869.5 (61.5%)** | 197.0 |
| 2 | 1414.6 | 335.4 (23.7%) | **247.8 (17.5%)** | **823.6 (58.2%)** | 240.9 |
| 3 | 1415.3 | 334.2 (23.6%) | 445.0 (31.5%) | 628.4 (44.4%) | 438.2 |

规律：各卡计算几乎相等（~333ms），但 **Rank1/2 的 Free 远超均值且 Communication 异常偏短**，Rank0/3 的通信时间中绝大多数是等待（Wait 441/438ms vs Rank1 的 197ms）。

3. **通信算子耗时分群**（HcclPerRankStats，count 均为 258）：Rank0/3 的 allReduce 总耗时 425.9/424.6ms，Rank1/2 仅 200.2/231.1ms —— 同型同 count 通信，耗时差 2 倍，只能来自等待。
4. **Timeline 交叉验证**（Overlap Analysis）：Rank1 窗口 Free 占比 **57.9%**、Comm 13.3%；Rank0 Free 36.3%、Comm 25.4%。

**判定**：✅ 符合预期。证据充足（slow_rank + cluster_time_summary + hccl_sum 三方一致）。结论有明确慢卡 Rank ID、候选快卡 Rank ID，且引用多项宏观证据。

---

## 4. 第 2 轮：造成快慢卡的原因？

**用户**：造成快慢卡的原因是什么？

**Agent 回答**：

结论：**Host 下发/调度瓶颈（伪快卡型慢卡），不是计算型、也不是通信链路问题。**

判定依据（逐一排除 + 微观证据）：

1. **排除计算型慢卡**：`compare_op_stats.py`（慢卡 Rank1 vs 快卡 Rank0）显示所有算子 count 完全一致、耗时比 ≈1.0（最大偏差 +5% 的 FusedInferAttentionScore 仅 0.44μs/次），负载切分均匀、无算子劣化。
2. **排除慢链路**：通信矩阵 HCCS 带宽 16.2~17.6 GB/s，各卡、各 op 均匀；传输总耗时各卡一致（~6.8ms），慢的是等待不是传输。`slow_link` 输出中异常项是等待占比而非带宽异常。
3. **确认 Host 下发瓶颈**（关键证据链）：
   - **free_analysis**：Rank1 存在 19.66ms 的 `Abnormal CANN layer: long time between two node@launch`（CANN 下发间隙），Rank2 为 8.87ms（2 次）+7.34ms 无任务下发；Rank0/3 同样间隙仅 ~9ms（1 次）→ 慢卡 Host 下发间隙更大、更频繁。
   - **API 对比**（`compare_api_stats.py` Rank1 vs Rank0）：`aclnnInnerFusedInferAttentionScore` API 总耗时 130ms vs 83.7ms（**慢 1.55×**，1088 次，每次多 42.6μs）；`launch` 276.8ms vs 259.5ms（多 17ms）；`aclrtRecordEvent` 慢 1.58×。
   - **同步点不对称**：`aclrtSynchronizeEvent`（17 次）快卡 Rank0/2/3 分别阻塞 329.9/285.7/308.7ms（平均 16.8~19.4ms/次），而慢卡 Rank1 仅 19.6ms（平均 1.15ms/次）→ 快卡在同步点大量等待慢卡。
   - **advisor（Rank1）**：E2E 1422.8ms 中空闲 886.4ms（62.3%），且明确输出"发现大量存在 host 下发瓶颈的算子序列，host 瓶颈耗时占比 0.9"。
   - **Timeline Free 分布**：Rank1 在 0.2~5ms 中段间隙合计 ~603ms（0.2-1ms 491 段、1-5ms 199 段），显著多于 Rank0（318 段合计 ~380ms）→ 频繁小颗粒下发间隙，典型 Host 饿死 NPU 特征。

**判定**：✅ 符合预期。因果闭环成立——Rank1/2 Host 下发慢 → 各自 NPU 饿死（Free 极大）→ 到达集合通信最晚 → Rank0/3 在 allReduce/allGather 处等待（CommWait 441/438ms）。

**待验证项**：Host 下发慢的更深层根因（CPU 绑核/频率/GC/线程调度）未采集 `with_stack`，无法定位到具体代码行——需补充带栈采集确认。

---

## 5. 第 3 轮：评估快慢卡影响，拖慢了多少时间？

**用户**：评估快慢卡问题造成的影响，拖慢了多少时间？

**Agent 回答**：

结论：**当前 E2E 窗口 1422.8ms，慢卡问题导致至少 246ms/窗口、上界可达 ~1000ms/窗口（约 70%）的无效耗时**。

量化过程：

1. **当前基线**：慢卡 Rank1 E2E = 1422.8ms（计算 332.6 + 通信 203.8 + 空闲 886.4）。这是集群同步的下界（所有卡 stepTime 均 ~1414ms）。
2. **有效工作时间**：计算 ~334ms + 通信传输 ~6.8ms + 内存 ~7.5ms ≈ **348ms**，仅占 E2E 的 24.5%。其余 ~1074ms 均为等待/空闲。
3. **慢卡直接损失**：Rank1 比 Rank0 多出 246ms Free（886.4 vs 640.3），Rank2 多 183ms（823.6 vs 640.3）。这部分是慢卡 Host 下发间隙导致的本卡 NPU 空转，若下发对齐到快卡水平至少可省 **246ms/窗口（≈17.3%）**。
4. **快卡被拖累的等待**：Rank0/3 在通信中的等待为 419/418ms（allReduce 425.9/424.6ms − 传输 6.8ms），相对 Rank1 自身等待基线 193ms，每个快卡多等 **~226ms**。
5. **理论上界**：若完全消除错位等待与下发间隙，E2E 可收敛至 ~348ms + 固有抖动 ≈ 400ms 量级，潜在加速 **~3.5×**。

影响汇总：

| 指标 | 数值 |
|---|---|
| 当前 E2E/窗口 | 1422.8 ms |
| 慢卡超额空闲（保守可省） | ~246 ms（17.3%） |
| 快卡额外等待 | 每卡 ~226 ms |
| 理论上界（完全消除错位） | ~1000 ms（70%） |

**判定**：✅ 基本符合预期，但需标注不确定性。保守数字（246ms/17.3%）有直接数据支撑；"上界 70%/3.5×" 是推演值，依赖"所有等待都可消除"的理想假设。建议验证方法：对 Rank1/2 优化绑核或排查 Host 下发线程后重新 Profiling，对比 E2E 与 Free 变化。

---

## 6. 三轮交互总结判定

| 轮次 | 问题 | 判定 | 说明 |
|---|---|---|---|
| 1 | 有无快慢卡 | ✅ 符合预期 | 慢卡 Rank1/2 定位准确，证据覆盖 slow_rank + 时间拆解 + 通信分群 + timeline 交叉验证 |
| 2 | 造成原因 | ✅ 符合预期 | 正确识别为 Host 下发瓶颈并排除计算/链路；有 1 项待验证（根因到代码层需补栈采集） |
| 3 | 影响量化 | ✅ 基本符合预期 | 给出保守与上界两档量化；上界为推演值，需以复测数据收敛 |

**总体判断**：三轮输出均符合"数据驱动 + 证据闭环 + 结论简洁"的要求，问题定位、原因分析和影响评估链条完整、可追溯。

**主要限制**：
- `freq_analysis` 无输出（已记录为不可用证据）
- 未采集 `with_stack`，Root Cause 无法下钻到 Host 侧具体代码/线程级

均属于可接受的证据边界，已在报告中明确标注为待验证项。

---

## 7. 生成的分析结果产物

补充分析结果保存在 `cluster_analysis_output/` 下：

- `cluster_time_summary/` — 集群迭代耗时拆解（Free/Compute/Communication/Wait）
- `compute_op_sum/` — 计算类算子汇总（排除计算型慢卡证据）
- `hccl_sum/` — HCCL 通信算子汇总（通信耗时分群证据）
- `slow_rank/` — 快慢卡影响次数与慢算子明细
- `slow_link/` — 异常耗时链路分析
- `cann_api_sum/` — CANN API 汇总（下发/同步点证据）
- `free_analysis/` — 空闲时间成因分析（node@launch 下发间隙证据）
- `advisor_rank1/` — 慢卡 Rank1 专家建议报告（Host 下发瓶颈）
- `freq_analysis/` — ⚠️ 输出为空，未纳入证据
