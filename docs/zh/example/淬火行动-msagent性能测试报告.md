# Ascend 集群快慢卡根因分析报告

> 数据来源：`/workspace/user_data/df536040f370_{651,654,657,664}_*/ASCEND_PROFILER_OUTPUT/`（4 Rank 单机 4 卡，megatron-lm，dp=4, tp=pp=cp=ep=1，通信域 rank_set=(0,1,2,3)）
> 分析方法：`msprof-analyze`（cluster 模式 + advisor） + `communication.json`/`trace_view.json` 多 rank 时序对齐
> 分析时间：2026-08-20

## 1. 结论总览

| 项目 | 结论 |
|---|---|
| 慢卡 | **Rank 1**（Free 886.4ms / 62.3%，通信仅 203.8ms）；次之 **Rank 2**（Free 839.3ms / 59.0%） |
| 正常基线 | Rank 0 / Rank 3（Free ~640ms / ~45%，通信 ~445ms） |
| 根因 | **Host 下发瓶颈（CPU 下发慢 → NPU 饿死）**，非计算型慢卡、非链路问题 |
| 全局问题 | 通信与计算零重叠、通信小包 <16MB、17 个通信算子字节未对齐、AI Core 利用率仅 13-15% |

---

## 2. 宏观判定（谁慢）

`ClusterStepTraceTime`（cluster_analysis.db，含全部 4 rank）：

| Rank | Stage 时长 | 计算 | 通信 | Free | Free 占比 |
|---|---|---|---|---|---|
| Rank 0 | ~1422ms | ~335ms | ~445ms | ~640ms | ~45% |
| Rank 1 | ~1422ms | ~332ms | 203.8ms | **886.4ms** | **62.3%** |
| Rank 2 | ~1422ms | ~333ms | 247.8ms | 839.3ms | 59.0% |
| Rank 3 | ~1422ms | ~334ms | ~445ms | ~640ms | ~45% |

**判定**：4 卡计算时间几乎一致（332-335ms，算子对比差异 <2%），排除计算型慢卡与负载切分不均。差异全部集中在 Free 与通信等待的"互补迁移"：Rank1 比 Rank0 Free 多 ~246ms、通信少 ~241ms；Rank2 相应多 ~199ms、少 ~197ms——慢卡 NPU 空闲远超正常卡，指向**下发/调度瓶颈**。

---

## 3. 微观根因：Host 下发瓶颈（证据链）

### 3.1 自由时间里的"异常 CANN 下发间隙"

free_analysis（Rank 1）Top-10 空闲段：

- 存在 2 处 `Abnormal CANN layer: long time between two node@launch`：**19.7ms**、**7.3ms** launch 间隙
- 最长 46.6ms 空闲段内，Device 仅剩 `EVENT_RECORD`/`EVENT_WAIT` 任务（无计算、无通信可执行 → 纯等下发）

### 3.2 CANN API 对比（Rank1 vs Rank0）

| API | Rank 1 | Rank 0 | 差异 |
|---|---|---|---|
| `aclnnInnerFusedInferAttentionScore` | 平均 119.5us | 平均 76.9us | **+55%**（次数相同 1088 次） |
| `aclrtRecordEvent` | - | - | +58% |
| `launch`（总耗时） | 276.8ms | 259.5ms | +6.7%（单次最大 **45.4ms**） |
| `aclrtSynchronizeEvent` | 19.6ms（17 次，平均 1.15ms/次） | 285.7-329.9ms（平均 16.8-19.4ms/次） | 同步点几乎无等待 → NPU 队列被饿空 |

→ 慢卡 CPU 侧每次下发 attention 融合算子更耗时，且偶发秒级大停顿（45.4ms）；同步点几乎不等待，说明其 NPU 队列长期被饿空，只能等待 CPU 下发。

### 3.3 集合通信同步点：快卡在等慢卡（时序铁证）

对 258 个 `hcom_allReduce` 实例做 4 rank 启动/结束时序对齐：

- **Rank 1 或 Rank 2 总是最后到达集合点**（258 个 op 中 rank1=129 次、rank2=129 次 start-latest）
- **Rank 3 总是最后完成**（所有 op 中 rank3 end-latest = 258 次），说明 rank3 在等最慢到达者完成后才收尾
- 典型实例 `hcom_allReduce__503_1_1`：rank0/2/3 同时启动并耗时 **12.47ms**，rank1 迟到 12.4ms 后仅耗时 **0.024ms** 即完成 → rank1 根本没参与前 12ms 的数据传输，其他 3 卡原地等待
- 实例 `hcom_allReduce__503_130_1`：rank2 迟到（start 晚 5.7ms）
- 各 rank allReduce 累计（258 次）：rank0=425.9ms、rank3=424.6ms（以 idle 为主 420ms）、rank1=200.2ms（以 wait 为主 194.9ms）、rank2=231.1ms

**解读**：慢卡（rank1/2）因为 CPU 下发慢，**连集合通信算子的提交都迟到**；正常卡（rank0/3）在集合点长时间空转等待。Rank 0 的 `aclrtSynchronizeEvent` 总计 329.9ms（单次最大 133ms）进一步印证快卡在同步点苦等慢卡。

### 3.4 排除链路问题

- HCCS 带宽 ~16-17GB/s，正常；communication_matrix 无 slow link
- allReduce 走 LOCAL（52.8GB/s）、allgather 走 LOCAL+HCCS
- slow_link 检出的仅 2 个算子 offsetRatio 提示，非瓶颈来源

### 3.5 交叉验证与口径说明

- **slow_rank 算法**：`SlowRank` 表 slowAffectCount：Rank1=137、Rank2=129，直接命中慢卡判定
- **口径说明**：advisor 对 Rank1 的 wait/transmit 拆分（wait≈0.02ms / transmit≈203.8ms）与 `cluster_time_summary`（wait=197ms / transmit=6.8ms）存在统计口径差异，但不影响"Rank1 通信总时长显著短、空闲显著长"这一主结论

---

## 4. 全局性问题（非根因，但影响整体利用率）

1. **通信与计算零重叠**：Step 内通信未与计算 overlap，空闲占比高达 45-62%
2. **通信小包**：SDMA 通信 100% 为 <16MB 小包，带宽利用率受限
3. **17 个通信算子字节未对齐**（advisor 检出）
4. **AI Core 计算利用率仅 13-15%**（23.38% 计算 / 62.30% 空闲）
5. 检出 94 组可融合算子序列（host 瓶颈占比 0.9）；1 个动态 shape 算子（在线编译开销）

---

## 5. 优化建议

| 优先级 | 建议 | 操作 |
|---|---|---|
| **P0** | 修复 Host 下发瓶颈 | 排查 Rank1/2 进程 CPU 绑核/NUMA 亲和性；避免与其他 rank 争抢 CPU；检查 `torch.set_num_threads`/DataLoader/日志等 CPU 干扰；`msprof` 重采集 `with_stack=True` 定位 launch 间隙（19.7ms/45.4ms）对应代码位置 |
| **P1** | 增大单次通信量 | 增大 micro-batch / 梯度累积，使 allReduce 超过 16MB 小包阈值，提升带宽利用率 |
| **P1** | 通信对齐与融合 | 修复 17 个未对齐通信算子；启用梯度 bucketing（`--use-distributed-optimizer` + `DDP_BUCKET_CAP_MB`）；考虑算子融合减少下发次数 |
| **P1** | 关闭动态 shape | 消除动态 shape 在线编译开销；启用图模式/静态 shape |
| **P2** | 通信计算重叠 | 调整流水/异步通信，将集合通信与计算 overlap，降低全局 Free |

---

## 6. 验证方法

1. **复测 Host 下发**：`msprof` 重新采集并设置 `with_stack=True`，对比修复前后 Rank1/2 的 `aclnnInnerFusedInferAttentionScore` 单次耗时（目标从 119.5us 降到 ~77us）与 launch 间隙（目标消除 19.7ms/45.4ms 异常段）
2. **复测集合通信**：确认同一 allReduce 实例 4 rank start 时间差收敛到 <0.5ms（当前最大 12ms）
3. **复测整体**：Rank1/2 Free 占比从 62%/59% 降至接近 Rank0/3 的 ~45%，Stage 时长整体下降
4. 最终目标：AI Core 利用率从 13-15% 提升至 30%+（配合通信重叠优化后）

---

## 7. 附录：分析工具与数据源

- 工具：`msprof-analyze` 8.5.2（cluster_time_summary / free_analysis / cann_api_sum / hccl_sum / compute_op_sum / slow_rank / slow_link / advisor）
- 原始数据：各 rank `ASCEND_PROFILER_OUTPUT/` 下 `step_trace_time.csv`、`communication.json`、`communication_matrix.json`、`trace_view.json`、`kernel_details.csv`
- 注意：`cluster_time_summary`/`hccl_sum`/`cann_api_sum`/`compute_op_sum` 的按 rank 导出仅覆盖 Rank 0/1；4 卡宏观数据以原 all 模式 `cluster_analysis.db` 的 `ClusterStepTraceTime` 为准
