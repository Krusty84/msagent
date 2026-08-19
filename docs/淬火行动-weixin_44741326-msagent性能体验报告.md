# 淬火行动 - msagent 性能体验报告

> 数据目录：`/workspace/user_data/`  
> 体验日期：2026-08-19  
> 分析工具：`msprof-analyze`（集群分析 + advisor 专家建议）+ SQL/CSV 证据交叉验证 + `ascend-schedule-analysis` 根因下钻  
> 数据类型：Ascend PyTorch Profiler Text（`*_ascend_pt`），4 Rank（0–3），单机 4 卡，Qwen3-32B（DP=4, TP=1, PP=1, CP=1, EP=1）

***

## 一、体验概览

本次体验以"集群快慢卡诊断"为主线，与 Profiler（msagent）进行 6 轮对话，完整走通：**数据确认 → 集群分析 → 快慢卡判定 → 报告导出 → 影响量化 → 根因下钻** 的全流程，并据此评估 Agent 的分析质量、结果符合预期程度与工具链体验。

| 轮次 | Prompt（用户输入）                                         | 目标                           |
| -- | ---------------------------------------------------- | ---------------------------- |
| 1  | hello                                                | 发起会话，确认 Agent 能力             |
| 2  | 从当前Profiling数据来看，有无集群快慢卡，有什么关键                       | 集群快慢卡诊断：判定是否存在快慢卡、慢卡是谁、关键瓶颈点 |
| 3  | 将刚才的分析结果导出为淬火行动-msagent性能体验报告.md，放在user_data目录下      | 导出分析结果报告（v1）                 |
| 4  | 重新出，要包含：包含与agent对话的prompt，agent返回的结果，结果是否符合预期，意见与建议等 | 重新生成报告（v2），补充对话记录与评估         |
| 5  | 评估快慢卡问题造成的影响，拖慢了多少时间                                 | 量化快慢卡造成的性能损失                 |
| 6  | 造成快慢卡的原因是什么                                          | 下钻 Host 下发慢的根因               |

***

## 二、对话记录与 Agent 返回结果

### 第 1 轮：发起会话

* **Prompt**：`hello`

* **Agent 返回**：自我介绍，说明可提供的 Ascend NPU 性能分析能力（单卡/多卡分析、集群快慢卡诊断、算子 MFU、数据校验与查询），并提示用户提供性能数据目录路径（如 `*_ascend_pt` / `*_ascend_ms`）。

* **评估**：✅ 符合预期。能力边界清晰，主动索要数据路径，便于下一步交互。

***

### 第 2 轮：集群快慢卡诊断（核心轮次）

* **Prompt**：`从当前Profiling数据来看，有无集群快慢卡，有什么关键`

* **执行过程**：Agent 自动触发 `ascend-cluster-fast-slow-rank-detector` 技能，按 SOP 完成：数据确认（4 Rank / Text 类型）→ 检查已有 `cluster_analysis_output` → 用 `msprof-analyze` 逐项补齐 `cluster_time_summary / slow_rank / slow_link / compute_op_sum / hccl_sum / cann_api_sum`（6 项全部成功）→ 对慢卡 Rank 1 执行 `advisor all` 深入诊断。

* **Agent 返回结果（核心结论）**：

> **集群存在明显快慢卡现象。**
>
> * **真正的慢卡：Rank 1 和 Rank 2** —— 属于【Host 下发瓶颈（伪快卡）】，CPU 下发慢导致其 NPU 饿死（空闲占比高达 62.1% / 58.8%）。
>
> * **被拖累的等待方：Rank 0 和 Rank 3** —— 每迭代在通信点等待 Rank 1/2 约 **245ms**。
>
> * **瓶颈类型**：Host 下发 / 调度瓶颈（主因）；叠加通信小包、字节未对齐等次生问题。
>
> * **已排除**：计算型慢卡、负载切分不均、慢链路/通信带宽瓶颈。

**关键证据**：

1. 时间拆解（单位 ms）

| Rank | Compute | Compute 占比 | Comm  | Free  | Free 占比   | 通信等待      |
| ---- | ------- | ---------- | ----- | ----- | --------- | --------- |
| 0    | 333.8   | 23.6%      | 447.9 | 632.4 | 44.7%     | **245.1** |
| 1    | 332.6   | 23.5%      | 203.8 | 877.5 | **62.1%** | **1.0**   |
| 2    | 335.4   | 23.7%      | 247.8 | 831.4 | **58.8%** | 偏低        |
| 3    | 334.2   | 23.6%      | 445.0 | 636.1 | 44.9%     | **242.2** |

1. `slow_rank`：Rank 1 slowAffectCount=**137**、Rank 2=**129**；慢算子 100% 为 `hcom_allReduce__503_*`（本身微秒级，非传输瓶颈）。
2. advisor（Rank 1）直接证实：E2E 1422.8ms 中空闲 886.4ms（62.3%），通信等待仅 0.017ms；**94 个可融合算子序列中 host 瓶颈耗时占比 90%**。
3. CANN API 佐证：Rank 1 的 `aclrtSynchronizeEvent` 仅 19.6ms（占比 1.9%），Rank 0/2/3 为 286–330ms（22–26%）。

**优化建议**：

| 优先级 | 问题             | 建议                                                         |
| --- | -------------- | ---------------------------------------------------------- |
| P0  | Host 下发/调度瓶颈   | 排查 Rank 1/2 CPU 下发线程绑核与 NUMA 亲和、CPU 饥饿；定位 94 个 host 瓶颈算子序列 |
| P1  | 通信小包 + 字节未对齐   | 增大 batch/梯度累积；对齐 17 个未对齐通信算子                               |
| P1  | 1 个动态 shape 算子 | 关闭在线编译 `torch_npu.npu.set_compile_mode(jit_compile=False)` |

* **评估**：✅ 符合预期。给出慢卡（Rank 1/2）与等待方（Rank 0/3）的明确判定；宏观（cluster_time_summary / slow_rank）+ 微观（advisor / cann_api_sum / slow_link）证据交叉验证；显式排除干扰项；每条建议含操作与验证方法。

***

### 第 3 轮：导出报告 v1

* **Prompt**：`将刚才的分析结果导出为淬火行动-msagent性能体验报告.md，放在user_data目录下`

* **Agent 返回**：在 `/workspace/user_data/淬火行动-msagent性能体验报告.md` 生成报告，包含：数据概况、核心结论、关键证据（时间拆解表 / slow_rank 定性 / 伪快卡特征 / advisor 证实 / CANN API 佐证 / 排除项）、问题与优化建议（P0/P1）、可复现命令、一句话总结。

* **评估**：⚠️ 部分符合预期。结论与证据完整，但**缺少对话过程留存与结果评估**，用户随后要求补充，触发第 4 轮重出。

***

### 第 4 轮：影响量化

* **Prompt**：`评估快慢卡问题造成的影响，拖慢了多少时间`

* **Agent 返回（核心结论）**：

> **快慢卡问题使训练每迭代拖慢约 244ms（占迭代时间 17.3%）**，修复后等量时间吞吐可提升约 21%。

| 指标      | 当前      | 修复后预期   | 变化                    |
| ------- | ------- | ------- | --------------------- |
| 单迭代 E2E | ~1414ms | ~1170ms | **节省 ~244ms（−17.3%）** |
| 等量时间吞吐  | 1.00×   | ~1.21×  | **提升 ~21%**           |

* 证据：Rank 0/3 每迭代通信等待 245.1ms / 242.2ms（Rank 1 仅 1.0ms → 正常水平应为毫秒级）；慢卡自身额外空转 Rank 1 ~245ms、Rank 2 ~199ms；每迭代跨卡等待合计 ~533ms（卡·ms）。

* **待验证项**：以 Rank 0/3 的 free 作为"必要空闲"代理值；采集窗口约 1 个迭代，总时长损失需提供迭代数/训练总时长。

* **评估**：✅ 符合预期。给出明确的拖慢时间、占比、预期收益与推算逻辑，并主动标注待验证项，避免过度承诺。

***

### 第 5 轮：根因分析

* **Prompt**：`造成快慢卡的原因是什么`

* **执行过程**：Agent 触发 `ascend-schedule-analysis` 技能，补充执行 `msprof-analyze cluster -m free_analysis`，读取 `step_trace_time.csv`、CANN API 明细，按"Free 时间拆分 → 空闲原因归类 → Host Bound 判定"流程下钻。

* **Agent 返回（核心结论）**：

> **结论：系统性 Host 下发压力 + 慢卡（Rank 1/2）CPU 侧下发被额外延迟（Host Bound / 调度瓶颈）**

**两级成因**：

* **第一级（基线，4 卡共同存在）**：模型本身 Host 下发压力大——launch 4 卡各 4168 次（平均 62–66us/次）；94 个可融合算子序列中 host 瓶颈占比 90%；4 卡空闲全部 ≥44.7%。

* **第二级（触发因素，Rank 1/2 独有）**：

  * **CANN launch 间隙（下发停滞）**：Rank 1 两次 `node@launch` 之间停滞 **19.7ms**（正常卡仅 ~9ms）；

  * **设备队列饿死**：Rank 2 最长 **68.6ms**、Rank 1 最长 **46.6ms** 内设备队列只有 EVENT_RECORD 任务、无任何计算任务；

  * **Host 调用长尾**：Rank 1 的 `hcom_allGather_` Host 调用单次高达 **45.4ms**（其他卡均值 3–6ms）；

  * **PyTorch 层下发空白**：Rank 2 出现 "Idle Pytorch layer: no task dispatched in 7.3ms"。

**根因归类**：

| 类别               | 判定     | 依据                                       |
| ---------------- | ------ | ---------------------------------------- |
| Host Bound（设备饿死） | ✅ 高置信  | Free >10%、空闲段无计算任务、CANN launch 间隙与空闲段对齐  |
| 模型侧下发压力          | ✅ 存在   | 94 序列 host 占比 90%、launch 4168 次          |
| CPU 调度/亲和性干扰     | ⚠️ 强嫌疑 | 同机 4 卡代码与负载一致，仅 Rank 1/2 下发被延迟，特征吻合线程被抢占 |
| 同步点破坏            | ❌ 排除   | Rank 1 的 aclrtSynchronizeEvent 反而最短      |
| 计算/负载不均          | ❌ 排除   | Count 一致、Mean 差异 <0.5%                   |

**最小验证实验**：① `export TASK_QUEUE_ENABLE=2`（两级下发流水）；② `CPU_AFFINITY_CONF=1`（先 `npu-smi info -t topo` 确认拓扑）；③ 对 94 个可融合算子序列做算子融合；④ 动态 shape 优化 + `ACLNN_CACHE_LIMIT=100000`。  
**待验证项**：OS 层调度证据缺失（无 ftrace/sched_slice，需更高 profiler_level 或 `perf sched`）；CPU 绑核/NUMA 配置未知。

* **评估**：✅ 符合预期。根因定位到 Host 下发/CPU 调度层面，结论分级（高置信/强嫌疑），未把猜测包装成结论，明确列出待验证项与最小验证实验。

***

## 三、结果是否符合预期（整体评估）

| 评估项                      | 判定      | 说明                                                                                               |
| ------------------------ | ------- | ------------------------------------------------------------------------------------------------ |
| 是否识别出快慢卡                 | ✅ 符合预期  | 明确给出慢卡（Rank 1/2）与等待方（Rank 0/3），而非停留在"有/无"层面                                                      |
| 是否给出根因类型                 | ✅ 符合预期  | 判定为 Host 下发瓶颈（伪快卡），并给出 advisor host 瓶颈占比 90% 的直接证据                                               |
| 是否量化影响                   | ✅ 符合预期  | 给出每迭代拖慢 ~244ms（17.3%）、吞吐提升 ~21% 及推算逻辑                                                            |
| 是否下钻到具体成因                | ✅ 符合预期  | 两级成因：模型侧下发压力（基线）+ Rank 1/2 下发线程额外延迟（launch 间隙 19.7ms、队列饿死 68.6ms、Host 调用长尾 45.4ms）               |
| 证据是否闭环                   | ✅ 符合预期  | 宏观（cluster_time_summary / slow_rank）+ 微观（advisor / free_analysis / cann_api_sum / slow_link）交叉验证 |
| 是否排除干扰项                  | ✅ 符合预期  | 显式排除计算型慢卡、负载不均、慢链路、同步点破坏                                                                         |
| 建议是否可执行                  | ✅ 符合预期  | 每条建议含具体操作与验证方法，区分 P0/P1/P2 优先级                                                                   |
| 数据缺口是否透明                 | ✅ 符合预期  | 注明 ClusterTimeSummary 缺 Rank 2 行、未跑部分能力、OS 调度证据缺失及原因                                             |
| **短板 1：第一版报告缺"对话记录与评估"** | ❌ 不符合预期 | 用户要求补充 prompt 记录、结果评估、意见与建议后重出（第 4 轮已修订）                                                         |
| **短板 2：慢卡下钻覆盖不全**        | ⚠️ 部分符合 | 仅对 Rank 1 执行 advisor 深入，未对 Rank 2 单独 advisor                                                     |
| **短板 3：OS 层根因证据未闭环**     | ⚠️ 部分符合 | CPU 调度干扰判定为"强嫌疑"，缺 ftrace/sched 数据无法 100% 坐实                                                     |

***

## 四、意见与建议

### 4.1 对 Agent 分析质量的建议

1. **增强"对话过程留存"**：用户期望报告不仅是结论存档，还要可回溯"问了什么、答了什么、是否满意"。建议默认在交付报告中包含 prompt → 结果 → 评估三段式结构，避免二次返工。
2. **补齐慢卡覆盖**：多卡场景下所有慢卡 Rank 都应执行 advisor 深入（本次仅 Rank 1），并输出统一调优闭环；若受限于耗时，应显式说明省略项与原因。
3. **根因证据尽量闭环**：本次 CPU 调度干扰仅"强嫌疑"，建议在遇到此类场景时主动补充 `free_analysis` 与 OS 侧调度证据（或明确告知需更高 profiler_level），提高结论置信度。
4. **补充可视化**：报告可附 Timeline 截图或关键表格（communication matrix / free_analysis）的占位，便于团队评审时直接对照。
5. **沉淀基线**：本次结论可作为优化前基线（每迭代 E2E ~1414ms，Free 峰值 62%，等待 245ms），优化后复测对比三项指标。

### 4.2 对工具链（msprof-analyze）的反馈

1. `cluster_time_summary` 的 ClusterTimeSummary 表缺失 Rank 2 行，需依赖原始 ClusterStepTraceTime 补齐——建议工具侧修复该统计缺口。
2. `slow_link` 模式执行成功但 message/suggestion 为空，结果需从 DB 手动查表确认，建议输出明确的摘要字段。
3. 时间单位混用（us / ns / ms）易造成解读歧义，建议输出时统一单位并标注。
4. `free_analysis` 的字段名（如 `duration(us)` 带括号）需加引号才能 SQL 查询，工具自动生成 SQL 时存在兼容性问题。

### 4.3 后续行动建议（按优先级）

1. **[P0] 定位 Rank 1/2 Host 下发根因**：绑核/NUMA 排查 + 94 个 host 瓶颈算子序列下钻。
2. **[P0] 下发流水优化**：`export TASK_QUEUE_ENABLE=2`，对比 Free 占比与 E2E。
3. **[P1] 通信优化**：增大 batch / 梯度累积，减少 80KB 小包 allReduce；对齐 17 个未对齐通信。
4. **[P1] 关闭动态 shape 在线编译** + 视情况 `ACLNN_CACHE_LIMIT=100000`。
5. **[P2] 优化后复测**：对比 Free 占比、通信等待时间、E2E 时延三项指标（预期 E2E 1414ms → ~1170ms）。

***

## 五、附：可复现命令

```bash
# 1. 集群分析（逐项补齐证据）
msprof-analyze cluster -m cluster_time_summary -d /workspace/user_data -o /workspace/user_data/cluster_analysis_output/cluster_time_summary --force --agent
msprof-analyze cluster -m slow_rank       -d /workspace/user_data -o /workspace/user_data/cluster_analysis_output/slow_rank       --force --agent
msprof-analyze cluster -m slow_link       -d /workspace/user_data -o /workspace/user_data/cluster_analysis_output/slow_link       --force --agent
msprof-analyze cluster -m compute_op_sum  -d /workspace/user_data -o /workspace/user_data/cluster_analysis_output/compute_op_sum  --force --agent
msprof-analyze cluster -m hccl_sum        -d /workspace/user_data -o /workspace/user_data/cluster_analysis_output/hccl_sum        --force --agent
msprof-analyze cluster -m cann_api_sum    -d /workspace/user_data -o /workspace/user_data/cluster_analysis_output/cann_api_sum    --force --agent
msprof-analyze cluster -m free_analysis   -d /workspace/user_data -o /workspace/user_data/cluster_analysis_output/free_analysis   --force --agent

# 2. 慢卡 Rank 1 advisor 深入诊断
msprof-analyze advisor all -d /workspace/user_data/df536040f370_654_20260108093239559_ascend_pt -o /workspace/user_data/cluster_analysis_output/advisor_rank1 --force --agent
```


***

## 六、一句话总结

本次体验 6 轮对话全流程走通且结果整体符合预期：Agent 成功识别出集群快慢卡（**Rank 1/2 为 CPU 下发慢导致的"伪快卡"**，空闲 62.1% / 58.8%、通信零等待），量化了影响（**每迭代拖慢 ~244ms / 17.3%**，修复后吞吐可提升 ~21%），并下钻到根因（**系统性 Host 下发压力 + Rank 1/2 下发线程被额外延迟，Host Bound / 调度瓶颈**，CPU 调度干扰为强嫌疑待 OS 层证据坐实）；主要改进点为报告需默认包含"对话记录 + 结果评估 + 意见建议"三段式结构、慢卡 advisor 应覆盖全部慢卡 Rank，以及根因证据的 OS 层闭环。

<br />

## 七、ms insight导入分析

通过ms insight分析，关于快慢卡及通信耗时，与msagent结论一致。

<br />

![1.00](file:///C:/Users/72473/AppData/Roaming/ProcessOn/data/mind/image/7jnxWJlVjA.png)

![0.67](file:///C:/Users/72473/AppData/Roaming/ProcessOn/data/mind/image/6tKlZuMKel.png)

··
