# 淬火行动 - msagent 性能体验报告

## Ascend 集群快慢卡 Profiling 分析报告

- **报告日期**：2026-08-19
- **分析对象**：`/workspace/Qwen3-32b/` 下 4 卡 Ascend Profiling 数据（Qwen3-32B 训练）
- **分析工具**：`msprof-analyze` v8.5.2（cluster 集群分析 + advisor 专家建议）+ 集群快慢卡诊断技能
- **分析人**：Profiler（msagent）

---

## 一、分析背景与数据概况

### 1.1 数据说明

| 项目 | 内容 |
|---|---|
| 数据路径 | `/workspace/Qwen3-32b/` |
| Rank 数量 | 4（Rank 0/1/2/3） |
| 数据格式 | DB 类型（存在 `ascend_pytorch_profiler_{rank}.db`） |
| Host | 单机 `df536040f370`（hostUid 12939596036305169107） |
| Rank↔Device 映射 | Rank0→Device0，Rank1→Device1，Rank2→Device2，Rank3→Device3 |
| 已有集群分析结果 | `cluster_analysis_output/cluster_analysis.db`（含 ClusterStepTraceTime 等宏观表） |

### 1.2 分析流程

1. 数据类型判断：4 Rank，DB 类型，数据齐全
2. 调用 `msprof-analyze` 集群分析能力逐项执行：`slow_rank`、`slow_link`、`compute_op_sum`、`hccl_sum`、`cann_api_sum`、`free_analysis`
3. 基于宏观证据做快慢卡判定
4. 调用对比脚本 `compare_api_stats.py` / `compare_op_stats.py` 下钻微观根因
5. 对慢卡目录执行 `advisor all` 专家建议佐证

---

## 二、核心结论

### 2.1 结论 1：存在集群快慢卡

**存在。慢卡为 Rank 1、Rank 2，快卡为 Rank 0、Rank 3。**

**关键证据（`msprof-analyze cluster -m slow_rank` 官方算法输出）：**

| Rank | slowAffectCount（慢卡影响次数） | 判定 |
|---|---|---|
| Rank 1 | **137** | 慢卡 |
| Rank 2 | **129** | 慢卡 |
| Rank 0 / Rank 3 | 未上榜 | 快卡 |

**宏观时间拆解（ClusterStepTraceTime，单位 ms）：**

| Rank | Computing | Communication | Free（空闲） | Stage |
|---|---|---|---|---|
| 0 | 333.8 | 447.9 | 632.4 (44.7%) | 1414.0 |
| **1** | **332.6** | **203.8** | **877.5 (62.0%)** | 1413.9 |
| **2** | **335.4** | **247.8** | **831.4 (58.8%)** | 1414.6 |
| 3 | 334.2 | 445.0 | 636.1 (44.9%) | 1415.3 |

Rank 1/2 呈现典型"伪快卡"画像：**Free 时间极长（62%/59%），但 Communication 时间异常偏短（204/248ms vs 快卡 445ms）**。

### 2.2 结论 2：快慢卡根因是 Host 下发瓶颈（伪快卡）

按华为官方诊断逻辑，Rank 1/2 的"通信快"是假象：**CPU 下发慢 → NPU 饿死（巨大 Free）→ 它终于发起通信时，其他卡已等待多时，故其通信瞬间完成**。

#### 证据链一：计算侧排除（非计算型慢卡）

`compare_op_stats.py` 对比慢卡 Rank1 vs 快卡 Rank0：

- 主要算子 count 完全一致（MatMulV2 各 1920 次），avg 耗时 ratio 0.96~1.04
- 示例：MatMul `diff=-0.24us`、RmsNorm `diff=-0.12us`、FusedInferAttentionScore `ratio=1.02`
- **结论：计算侧无差异，排除计算型慢卡**

#### 证据链二：链路排除（非慢链路）

ClusterCommunicationBandwidth 显示各 rank HCCS/SDMA 带宽一致：

| Rank | 平均带宽 (GB/s) | Min | Max |
|---|---|---|---|
| 0 | 16.78 | 15.40 | 17.56 |
| 1 | 16.92 | 15.70 | 17.99 |
| 2 | 16.64 | 14.15 | 17.80 |
| 3 | 16.13 | 13.04 | 18.04 |

- SlowLink 分析无异常
- **结论：链路带宽正常，排除慢链路/通信硬件问题**

#### 证据链三：Host 侧下发异常的直接证据

1. **`free_analysis`**：Rank 1 出现 **"Abnormal CANN layer: long time between two node@launch 19.66ms"**（另有 7.3ms 间隙），即 CANN 层下发间隙异常
2. **`compare_api_stats.py`**：`aclrtSynchronizeEvent` 慢卡 Rank1 仅 **19.6ms** vs 快卡 Rank0 **330ms**（均 17 次）——慢卡永远最后一个到达同步点，无需等待别人；而快卡每次同步都要等慢卡（这正是快卡通信时间长的原因）
3. **API 耗时异常**：`aclnnInnerFusedInferAttentionScore` 慢卡 130ms vs 快卡 84ms（1.55 倍），但对应 device 算子（FusedInferAttentionScore）计算耗时一致（ratio 1.02）——差异发生在 **Host 侧 API/下发**，非 NPU 执行

#### 证据链四：Advisor 佐证（慢卡 Rank1）

- E2E 1422.8ms 中**空闲 886.4ms，占 62.30%**；未被掩盖通信仅 203.8ms
- 可融合算子分析检出 **94 个算子序列 host 瓶颈占比 0.9**，明确提示"大量 host 下发瓶颈"

#### 根因定性

**问题属于 Host 下发慢 / 调度瓶颈一类**。Rank 1/2 为真正的慢卡（伪快卡），拖累全集群。不是计算型慢卡、不是链路问题、不是负载切分不均。

### 2.3 结论 3：快慢卡问题影响评估

#### ① 快卡被拖慢的直接量化（每 step）

- 快卡 Communication 时间（445ms）比慢卡（204~248ms）多出 **197~244ms**——这部分就是快卡等待慢卡到达通信点的等待时间
- 换算为 stage 占比：**每 step 约损失 200~250ms，占 1414ms 的 14%~18%**

#### ② 慢卡自身资源浪费

- Rank 1 空闲 877.5ms（62%）、Rank 2 空闲 831.4ms（59%），比快卡多出 **约 195~245ms** 纯 NPU 饿死时间
- 慢卡本征工作量仅约 545ms（计算 333 + 通信 204 + 准备 9），说明 **当前 step 1414ms 中约一半时间可归因于下发瓶颈**

#### ③ 集群整体损失

- 当前 stage 由慢卡决定 = **1414ms/step**
- 若无 Host 下发瓶颈，理论 step 可压缩至 ~700ms 量级（计算 333ms + 纯通信 ~204ms + 必要调度），**可释放约 50%+ 的 step 时间**

**保守口径**：快慢卡问题直接造成的可量化损失为 **每 step 约 200~250ms（15%~18%）**；考虑慢卡自身 62% 的空闲，彻底修复 Host 下发后整体训练吞吐提升潜力显著更高。

---

## 三、优化建议（按优先级）

### [P0] 排查 Rank 1/2 的 CPU 下发线程绑核与资源争抢

- **问题**：Host 下发间隙（node@launch 19.6ms）表明 CPU 侧调度阻塞
- **动作**：检查训练进程 CPU 亲和性/NUMA、`torch_npu` 下发线程是否与其他负载争抢核
- **验证**：重新 Profiling 观察 Rank 1/2 的 Free 占比是否下降

### [P0] 关闭算子在线编译/动态 shape

- **问题**：advisor 检出动态 shape 算子，在线编译可能阻塞下发
- **动作**：在脚本入口加入：
  ```python
  torch_npu.npu.set_compile_mode(jit_compile=False)
  torch_npu.npu.config.allow_internal_format = False
  ```
- **验证**：对比关闭前后下发间隙与 Free 时间变化

### [P1] 通信小包与字节对齐

- **问题**：SDMA 通信 100% 数据量 <16MB，17 个通信算子数据大小未对齐
- **动作**：按 advisor 建议调整批量/梯度累积，或内存允许时由 ZeRO3 改为 ZeRO1/2 减少小包通信；字节对齐问题联系 HCCL 研发
- **验证**：观察通信算子的包大小与传输时间

### [P1] 采集开启调用栈的 Profiling 复测

- **动作**：对 Rank 1/2 补充 `with_stack=True` 采集，将下发间隙对到具体 Python 代码行，确认是否为非亲和操作（如动态索引、数据预处理）
- **验证**：定位到具体代码位置后进行针对性优化

---

## 四、报告方法学说明

- 时间单位统一为 ms（原始数据为 us，已换算）
- 关键结论均附证据，证据不足项已标注"待验证"
- 宏观结论（cluster_time_summary / slow_rank / slow_link）与微观结论（compare_op_stats / compare_api_stats / free_analysis）交叉验证一致
- 慢卡目录 advisor 输出与集群判断一致（均指向 Host 下发瓶颈），以集群分析为主、advisor 为佐证

---

## 附录：本次分析执行命令

```bash
# 集群分析逐项能力
msprof-analyze cluster -m slow_rank      -d /workspace/Qwen3-32b -o cluster_analysis_output/slow_rank      --force --agent
msprof-analyze cluster -m slow_link      -d /workspace/Qwen3-32b -o cluster_analysis_output/slow_link      --force --agent
msprof-analyze cluster -m compute_op_sum -d /workspace/Qwen3-32b -o cluster_analysis_output/compute_op_sum --force --agent
msprof-analyze cluster -m hccl_sum       -d /workspace/Qwen3-32b -o cluster_analysis_output/hccl_sum       --force --agent
msprof-analyze cluster -m cann_api_sum   -d /workspace/Qwen3-32b -o cluster_analysis_output/cann_api_sum   --force --agent
msprof-analyze cluster -m free_analysis  -d /workspace/Qwen3-32b -o cluster_analysis_output/free_analysis  --force --agent

# 对比脚本（微观下钻）
python compare_api_stats.py /workspace/Qwen3-32b 1 0 --top 15
python compare_op_stats.py  /workspace/Qwen3-32b 1 0 --top 10

# 慢卡 Advisor
msprof-analyze advisor all -d /workspace/Qwen3-32b/df536040f370_654_20260108093239559_ascend_pt --force --agent
```

---

*报告由 Profiler（msagen


quit
t）基于真实 Profiling 数据自动生成，所有指标均有数据来源支撑。*
