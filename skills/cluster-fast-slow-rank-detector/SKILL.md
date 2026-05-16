---
name: cluster-fast-slow-rank-detector
description: 专门用于 Ascend 集群 Profiling 性能数据的“快慢卡”诊断专家技能。当用户提供【集群性能数据目录/路径】并要求分析【快慢卡】、【慢节点】、【负载不均衡】或【集群瓶颈】时，必须触发此技能。该技能会自动接收集群路径，调度相关工具输出快慢卡的宏观定性与微观根因（如 Host 下发瓶颈、算子计算劣化）。
---

# 核心流程补充（必须优先执行）

以下四步为本 skill 的**强制主流程**。原有“先验知识库 / MUST DO / SOP / 脚本调用手册”继续保留，但如与本节顺序冲突，以本节为准。

## Step 1. 先确认是否为集群 Profiling 数据，并明确集群卡数

在执行任何分析前，必须先完成以下检查：

1. 判断用户提供的路径是否为**集群 Profiling 数据根目录**，而不是单卡 profiling 目录、单个 csv/db 文件或普通业务目录。
2. 在目录下递归查找 `profiler_info_{rank}.json`，确认这是多 Rank / 多卡数据。
3. 统计可识别的 Rank 数量，并在回复中明确写出：
   * 是否为集群 profiling 数据；
   * 共识别到多少张卡 / 多少个 Rank；
   * 各 Rank 的 profiling 文件夹是否齐全。
4. 若不是集群 profiling 数据，或 Rank 数不足以支撑快慢卡判断，必须先停止后续分析，并明确告知原因。

## Step 2. 必须完整执行 msprof-analyze 集群分析能力

确认输入为集群 profiling 数据后，必须调用 `msprof-analyze` 对该集群路径执行分析。**要求不是只跑 `all`，而是将 README 中与集群分析相关的 `-m` 能力逐项跑全**，至少覆盖下列能力：

* `all`
* `cluster_time_summary`
* `cluster_time_compare_summary`（仅当用户同时提供 benchmark / baseline 集群路径时执行）
* `compute_op_sum`
* `freq_analysis`
* `ep_load_balance`
* `communication_matrix`
* `communication_time`
* `communication_group_map`
* `communication_time_sum`
* `communication_matrix_sum`
* `hccl_sum`
* `pp_chart`
* `slow_rank`
* `cann_api_sum`
* `mstx_sum`
* `mstx2commop`
* `p2p_pairing`

补充说明：

* `all` 只是组合能力，**不能替代逐项执行**。
* `cluster_time_compare_summary` 依赖 `--bp` 标杆集群路径；若用户未提供，则要在结果里明确写“此能力因缺少 benchmark 路径未执行”。
* `compute_op_sum` 可按需结合 `--rank_list` 聚焦特定 Rank，但默认仍应先完成全局统计。
* `mstx2commop`、`p2p_pairing` 属于数据处理类能力，也应执行；若当前数据不满足其前置条件，则要在结果中记录“已尝试执行，但因输入数据条件不足未产出有效结果”。
* 对于 `msprof-analyze` 8.2.0a1 及以上版本，可直接使用 `msprof-analyze -m ... -d ...`；更早版本则使用 `msprof-analyze cluster -m ... -d ...`。

如用户环境允许，优先使用统一输出目录保存每个 `-m` 能力的结果，避免不同分析结果互相覆盖。

## Step 3. 基于集群分析结果做二次判断

完成 Step 2 后，agent 不能直接给出结论，必须先综合各项 `msprof-analyze` 集群分析结果，明确回答以下问题：

1. **是否存在快慢卡现象**；
2. **若存在，问题属于哪一类**：
   * Host 下发慢 / 调度瓶颈；
   * 计算型慢卡；
   * 通信型慢卡 / 慢链路；
   * 负载不均衡；
   * 多种问题叠加；
3. **真正的慢卡 Rank ID 是谁**，以及对应的判断依据；
4. **是否存在“伪快卡/伪慢卡”误判风险**，必须结合原有 Expert Rules 做防误判说明。

此步骤的输出至少要引用 Step 2 中的宏观分析证据，例如：

* `slow_rank` 相关结果；
* `cluster_time_summary` / `cluster_step_trace_time` 中的耗时极差；
* `communication_matrix` / `communication_time` 暴露的通信异常；
* `freq_analysis`、`ep_load_balance`、`cann_api_sum`、`mstx_sum` 等补充证据。

## Step 4. 对慢卡目录执行 advisor，并给出调优建议

当 Step 3 已锁定慢卡 Rank ID 后，必须进入该慢卡对应的 profiling 文件夹，再执行：

```bash
msprof-analyze advisor all -d <slow-rank-profiling-dir>
```

然后基于 advisor 输出，给出该慢卡的**针对性调优建议**。建议内容必须与瓶颈类型对应，例如：

* Host 下发慢：排查下发线程绑核、同步点、launch 间隙、CPU 饥饿、数据准备阻塞；
* 计算型慢卡：排查算子 count 不一致、动态 shape、算子劣化、融合缺失、AICore 利用率问题；
* 通信型慢卡：排查小包通信、并行切分策略、链路带宽、通信域配置、SDMA/HCCL 异常；
* 负载不均衡：排查数据切分、专家负载、pipeline stage 分配、rank 侧 workload 偏斜。

若 advisor 输出与 Step 3 的集群判断不一致，必须显式指出这一点，并说明以哪类证据为主、为什么。

# 新增硬约束（在原有 MUST DO 之外追加）

1. **先做集群识别，再做任何诊断。** 未确认集群 profiling 数据和卡数之前，禁止进入快慢卡结论阶段。
2. **先跑全量 cluster `-m` 能力，再做问题定性。** 禁止只凭 `all` 或单一交付件直接下结论。
3. **先锁定慢卡，再跑单卡 advisor。** 禁止在未确定慢卡 RankID 前，对任意目录盲目执行 advisor 并反推结论。
4. **原有 compare 脚本用于微观对比补充，不替代 Step 2 和 Step 4。**

# 推荐执行顺序（简版）

1. 识别集群 profiling 根目录，统计 Rank 数量。
2. 逐项执行集群 `msprof-analyze -m` 能力并收集结果。
3. 汇总判断是否有快慢卡、问题类型、慢卡 RankID。
4. 进入慢卡 profiling 文件夹执行 `msprof-analyze advisor all`。
5. 结合原有 Expert Rules 和对比脚本，输出最终诊断与调优建议。

# 集群快慢卡诊断

## 1. 技能目标
在 Ascend 多卡/集群场景下，利用 Advisor 工具结合专家规则，自动识别因计算、通信或 Host 下发导致的性能瓶颈卡（慢卡），并下钻定位微观根因。

## 2. 诊断先验知识库 (Expert Rules)
禁止仅凭单项指标字面意思下结论，必须严格遵守以下华为官方诊断逻辑：

* **【Host 下发瓶颈 (伪快卡)】**
    * **现象**：某卡（Rank X）的 `Free Time` 极长（占比 > 10% 或远超均值），且 `Compute` 和 `Communication` 时间异常偏短。
    * **定性**：**Rank X 绝非快卡，而是导致集群阻塞的“慢卡”。** CPU 下发慢导致其 NPU 饿死（产生巨大 Free Time）。当它终于发起通信时，其他卡已等待多时（其他卡 Wait 长），故其通信瞬间完成。
    * **动作**：调用 `scripts/compare_api_stats.py`，重点观察 `launch`、`aclrtSynchronizeDevice` 等下发/同步 API 的耗时与间隙差异。
* **【纯计算快慢卡】**
    * **现象**：各卡 `Free Time` 普遍较短且均匀，但某卡 `Compute Time` 显著大于均值。
    * **定性**：计算型慢卡。若单算子调用次数 (`count`) 不同，为负载切分不均；若次数相同但平均耗时 (`avg_time`) 激增，为算子硬件劣化或动态 Shape 导致。
    * **动作**：调用 `scripts/compare_op_stats.py` 对比算子执行差异。
* **【通信/慢链路瓶颈】**
    * **现象**：各卡通信带宽远低于理论值（如 SDMA < 2GB/s）。
    * **定性**：通常为小包通信（ZeRO3 切分过细）、SDMA 地址未对齐或硬件问题。

## 3. 硬性约束 (MUST DO)

1. **执行已有脚本，严禁造轮子**：微观下钻环节（Step 3）**必须且只能**通过在终端执行 `scripts/` 下的对比脚本获取差异数据。严禁在未执行脚本前自行读取 CSV/DB 进行 Diff 分析。
2. **禁止 Trace 分析**：本技能流程不包含 Timeline 级分析，Step 4 输出报告后立即结束，禁止主动读取 `trace_view.json`。

## 4. 标准操作流程 (SOP)

1. **宏观体检**：强制调用 `msprof-analyze-advisor` 工具输入集群路径，获取总体耗时极差与“慢卡分析”矩阵。
2. **瓶颈定性**：对照【先验知识库】，判定核心瓶颈属于 `Host下发慢`、`计算慢` 还是 `纯通信慢`，并锁定真正的“慢卡 RankID”。
3. **微观下钻**：根据瓶颈类型，在终端直接执行下方对应的对比脚本（计算慢用 OP 脚本，下发慢用 API 脚本）。仅当路径自动发现失败报错时，才可通过补充 `--slow-path` 参数重试。
4. **输出报告**：按以下结构输出最终回复：
   * **诊断结论**：瓶颈类型及真正慢卡的 RankID。
   * **宏观证据**：引用 Advisor 报告中的极差数据（如 Free Time 对比）。
   * **微观根因**：结合脚本输出的 Top 差异数据（特定 API 或算子的耗时比对），解释物理原因。
   * **优化建议**：给出针对性建议（如绑核、切分策略、排查算子Shape差异等）。

## 5. 脚本调用手册

对比脚本统一存放在本技能目录的 `scripts/` 文件夹中，支持自动发现集群目录或手动指定文件（优先 CSV，次选 DB）。

**核心命令模板：**
```bash
# 【计算类瓶颈】调用算子对比脚本（将 <本技能目录> 替换为 get_skill 返回中的路径）
python <本技能目录>/scripts/compare_op_stats.py <集群数据根目录> <慢卡RankID> <快卡RankID> [--top N]

# 【下发类瓶颈】调用 API 对比脚本
python <本技能目录>/scripts/compare_api_stats.py <集群数据根目录> <慢卡RankID> <快卡RankID> [--top N]
```
参数说明：
* cluster_pah: 集群数据根目录（包含 profiler_info_{rank}.json）。 
* slow_rank / fast_rank: 慢卡与快卡（基准）的 Rank ID。 
* --top N: （可选）输出差异最大的前 N 条，默认 20。 
* --slow-path / --fast-path: （可选）当集群自动发现机制报错时，用于手动指定慢/快卡的 *.csv 或 *.db 绝对路径。 
* --json: （可选）以 JSON 格式结构化输出。
