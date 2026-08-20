## Offloaded at 2026-08-19T8:45:18.290842+00:00

Human: 从当前Profiling数据来看，有无集群快慢卡，有什么关键证据
AI: [{'name': 'get_skill', 'args': {'name': 'ascend-cluster-fast-slow-rank-detector'}, 'id': 'call_00_4JFVrNItatjHkdnCVkWK9902', 'type': 'tool_call'}]
Tool: ---
name: ascend-cluster-fast-slow-rank-detector
description: 专门用于 Ascend 集群 Profiling 性能数据的“快慢卡”诊断专家技能。当用户提供【集群性能数据目录/路径】并要求分析【快慢卡】、【慢节点】、【负载不均衡】或【集群瓶颈】时，必须触发此技能。该技能会自动接收集群路径，调度相关工具输出快慢卡的宏观定性与微观根因（如 Host 下发瓶颈、算子计算劣化）。
---

# 集群快慢卡诊断

## 1. 技能目标
在 Ascend 多卡/集群场景下，利用msprof-analyze命令工具结合专家规则，自动识别因计算、通信或 Host 下发导致的性能瓶颈卡（慢卡），并下钻定位微观根因。

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

## 3. 标准操作流程 (SOP)
请严格按照以下流程执行：

1. 输入数据类型判断

先判断用户提供的路径或文件属于哪一类，并把后续分析所需证据统一整理为“集群级宏观证据”和“Rank 级明细证据”两类：

（1）明确当前输入数据的类型
* 路径下若存在Profiling数据：
  * 共识别到多少个 Rank；
  * Profilng数据的类型是DB or Text，若路径下存在ascend_pytorch_profiler_{rank_id}.db文件，则为 DB 类型数据，否则为 Text；
  * 各 Rank 的 profiling 文件夹是否齐全。
* 路径下是否已存在 `msprof-analyze` 的分析结果目录`cluster_analysis_output`：若存在，输出已包含哪些内容
* 告知用户，当前数据类型、Rank数量等信息

（2）路由后续操作
* 用户给的输入数据是 text 格式：请参考 `skills/ascend-msprof-analyze-cli` SKILL 的能力二：专家建议分析 (Advisor) 部分进行分析，然后直接进入流程 3 基于证据做快慢卡判定
* 用户给的输入数据是 db 格式，判断是否已经存在`cluster_analysis_output` 结果文件夹：
  * 若存在，检查是否包含 `cluster_time_summary`、`compute_op_sum`、`hccl_sum`、`slow_rank`、`slow_link`、`cann_api_sum` 等结果。记录缺失项，直接进入流程 2 调用 `msprof-analyze` 集群分析能力补齐；如果结果已完整，直接进入流程 3 基于证据做快慢卡判定
  * 不存在，直接进入流程 2 调用 `msprof-analyze` 集群分析能力
* 用户输入只存在 `cluster_analysis_output` 结果文件夹：直接进入流程 3 基于证据做快慢卡判定

2. 调用 `msprof-analyze` 集群分析能力
流程 2 只负责生成分析结果，不负责直接下结论。  
必须调用 `msprof-analyze` 执行集群分析。请参考 `skills/ascend-msprof-analyze-cli` SKILL 中 “集群综合分析 (Cluster)” 的部分
**要求不是只跑 `all`，而是将 README 中与集群分析相关的 `-m` 能力逐项跑全**，至少覆盖下列能力：

| 分析能力 | 介绍 |
|--|--------|
| all | 组合能力，可作为补充，但**不能替代逐项执行**。 |
| cluster_time_summary | 提供集群训练过程中迭代耗时拆解，作为判断 Free/Compute/Communication/Wait 异常的宏观入口。 |
| compute_op_sum | 汇总 device 侧计算类算子，用于定位计算型慢卡和负载切分不均。 |
| hccl_sum | 汇总通信类算子，用于定位通信型慢卡、慢链路和通信带宽异常。 |
| slow_rank | 展示各 Rank 的快慢卡影响次数和慢卡候选原因。 |
| slow_link | 分析集群异常耗时链路，用于辅助识别慢链路或通信瓶颈。 |
| cann_api_sum | 汇总 CANN 层 API，用于定位 Host 下发、同步点和 launch 间隙问题。 |

命令示例：
```bash
msprof-analyze -m cluster_time_summary -d ./cluster_data -o ./output/cluster_time_summary
```

如环境允许，优先为每个 `-m` 能力使用独立输出目录，避免不同分析结果互相覆盖。若某项能力执行失败，必须记录失败命令、错误摘要和缺失影响，后续结论中不得把该项当成已验证证据。

3. 基于证据做快慢卡判定

流程 3 只在证据集可用后执行。agent 不能只引用单项指标直接给结论，必须综合流程 1/2 得到的宏观证据和必要的 Rank 级明细证据，明确回答以下问题：

（1）**是否存在快慢卡现象**；  
（2）**真正的慢卡 Rank ID 是谁**，以及候选快卡 Rank ID 是谁；  
（3）**问题属于哪一类**：
   * Host 下发慢 / 调度瓶颈；
   * 计算型慢卡；
   * 通信型慢卡 / 慢链路；
   * 负载不均衡；
   * 多种问题叠加；
   * 证据不足，暂不能判定。

（4）**判定依据是什么**：至少引用 `cluster_time_summary`、`slow_rank` 或 `slow_link` 中的一类宏观证据；若涉及计算型或 Host 下发瓶颈，还应继续调用流程 4 中的对比脚本，用 `compare_op_stats.py` 或 `compare_api_stats.py` 给出微观证据。


4. 快卡vs慢卡比对

对比脚本统一存放在本技能目录的 `scripts/` 文件夹中，支持自动发现集群目录或手动指定文件（优先 CSV，次选 DB）。

**核心命令模板：**
```bash
# 【计算类瓶颈】调用算子对比脚本（将 <本技能目录> 替换为 get_skill 返回中的路径）
python <本技能目录>/scripts/compare_op_stats.py <集群数据根目录> <慢卡RankID> <快卡RankID> [--top N]

# 【下发类瓶颈】调用 API 对比脚本
python <本技能目录>/scripts/compare_api_stats.py <集群数据根目录> <慢卡RankID> <快卡RankID> [--top N]
```
参数说明：
* cluster_path: 集群数据根目录（包含 profiler_info_{rank}.json）。 
* slow_rank / fast_rank: 慢卡与快卡（基准）的 Rank ID。 
* --top N: （可选）输出差异最大的前 N 条，默认 20。 
* --slow-path / --fast-path: （可选）当集群自动发现机制报错时，用于手动指定慢/快卡的 *.csv 或 *.db 绝对路径。 
* --json: （可选）以 JSON 格式结构化输出。

5. 对慢卡目录执行 advisor，并给出调优建议（可选）

当流程3已锁定慢卡 Rank ID 后，必须进入该慢卡对应的 profiling 文件夹，再执行：

```bash
msprof-analyze advisor all -d <slow-rank-profiling-dir>
```

然后基于 advisor 输出，给出该慢卡的**针对性调优建议**。建议内容必须与瓶颈类型对应，例如：

* Host 下发慢：排查下发线程绑核、同步点、launch 间隙、CPU 饥饿、数据准备阻塞；
* 计算型慢卡：排查算子 count 不一致、动态 shape、算子劣化、融合缺失、AICore 利用率问题；
* 通信型慢卡：排查小包通信、并行切分策略、链路带宽、通信域配置、SDMA/HCCL 异常；
* 负载不均衡：排查数据切分、专家负载、pipeline stage 分配、rank 侧 workload 偏斜。

若 advisor 输出与流程3的集群判断不一致，必须显式指出这一点，并说明以哪类证据为主、为什么。

AI: 我先定位当前 Profiling 数据位置，并确认集群分析能力。[{'name': 'ls', 'args': {'path': '/workspace'}, 'id': 'call_00_ATKPz2RqilzWaj2zRdxh4015', 'type': 'tool_call'}, {'name': 'ls', 'args': {'path': '/workspace/user_data'}, 'id': 'call_01_yhDzk3lMbHaCQvSS4gW87875', 'type': 'tool_call'}, {'name': 'ls', 'args': {'path': '/workspace/shared_assets'}, 'id': 'call_02_Pg3GaC07TiPiloFYFR5u0225', 'type': 'tool_call'}]
Tool: ['/workspace/.msagent/', '/workspace/.venv/', '/workspace/Qwen3-8B/', '/workspace/shared_assets/', '/workspace/user_data/']
Tool: ['/workspace/user_data/.msagent/', '/workspace/user_data/3.2qwen3-8B-w8a8.yaml', '/workspace/user_data/=0.22.0', '/workspace/user_data/Qwen3-32B.zip', '/workspace/user_data/cluster_analysis_output/', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/', '/workspace/user_data/df536040f370_654_20260108093239559_ascend_pt/', '/workspace/user_data/df536040f370_657_20260108093239560_ascend_pt/', '/workspace/user_data/df536040f370_664_20260108093239560_ascend_pt/', '/workspace/user_data/qwen3-8B-w8a8.yaml']
Tool: ['/workspace/shared_assets/QuenchingAction/', '/workspace/shared_assets/README.md', '/workspace/shared_assets/RanxingAscend/', '/workspace/shared_assets/datasets/', '/workspace/shared_assets/models/']
AI: 找到数据：`/workspace/user_data` 下有 4 个 `*_ascend_pt` 目录（疑似 4 卡集群）及一个 `cluster_analysis_output`。先确认数据类型与已有分析结果。[{'name': 'ls', 'args': {'path': '/workspace/user_data/cluster_analysis_output'}, 'id': 'call_00_Vo0hZCVqWbeTsj3P1joH0841', 'type': 'tool_call'}, {'name': 'ls', 'args': {'path': '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt'}, 'id': 'call_01_umlWGXWGpKruPCy6KrFa9897', 'type': 'tool_call'}, {'name': 'get_skill', 'args': {'name': 'ascend-msprof-analyze-cli'}, 'id': 'call_02_a7ImrfGOwvIx7smd7nzU0791', 'type': 'tool_call'}]
Tool: ['/workspace/user_data/cluster_analysis_output/cluster_analysis.db']
Tool: ['/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/FRAMEWORK/', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/PROF_000001_20260108093239564_FRFFKRQQNGLHGMBB/', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/logs/', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/profiler_info_0.json', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/profiler_metadata.json']
Tool: ---
name: ascend-msprof-analyze-cli
description: MindStudio Profiler Analyze（msprof-analyze）是面向 AI 训练与推理场景的性能分析工具，基于采集得到的 profiling 数据进行统计、比对和诊断，帮助定位计算、通信、调度及集群场景下的性能瓶颈。
---

# Ascend 性能数据综合分析

目前提供两个功能，根据用户需求自动选择对应工具：

| 能力                     | 工具命令 | 侧重点                                                |
|------------------------| --- |----------------------------------------------------|
| 集群分析 (cluster analyse) | `msprof-analyze -m <mode>` | 涵盖集群多维信息汇总、拆解对比、通信瓶颈定位和下发问题分析等专题。 |
| 专家建议 (advisor)         | `msprof-analyze advisor <subcommand>` | 基于性能数据自动识别计算、调度、通信等潜在问题，并输出优化建议。                   |

---

## 功能选择指南

> ⚠️ **重要**：在开始分析前，必须先根据以下规则确定使用哪个功能。

### 选择规则

| 条件                        | 优先使用的分析能力 | 原因                      |
|---------------------------| --- |-------------------------|
| 卡数 ≥ 64 或数据量很大（db大于1G）    | 集群分析 | 集群分析专门优化了大数据量场景，能快速汇总数据 |
| 数据格式为 **db**              | 集群分析 | 集群分析仅支持 db 格式           |
| 数据格式为 **text** (json/csv) | 专家建议 | 专家建议支持 text 和 db 格式     |
| 需要分析慢卡/慢链路原因              | 集群分析后，再用专家建议 | 先定位慢卡，再用专家建议深入诊断        |
| 需要算子优化建议                  | 专家建议 | 专家建议输出详细的调优建议           |
| 单卡数据一般以'ascend_pt'或者'ascend_ms'结尾  | 专家建议 | 直接使用专家建议进行诊断            |

### 典型决策流程

```
用户提出分析需求
        │
        ▼
┌─────────────────────────────┐
│ 数据量评估：卡数、数据规模    │
└─────────────────────────────┘
        │
        ▼
┌─────────────────────────────┐
│ 数据格式判断：db 还是 text？  │
└─────────────────────────────┘
        │
        ├──────────────────────────────┐
        ▼                              ▼
   【db格式且为多卡数据】                    【text格式或者单卡数据】
        │                              │
        ▼                              ▼
  优先集群分析                  只能专家建议
        │                        (跳过第3步)
        ▼
┌─────────────────────────────┐
│ 集群分析是否找到慢卡？        │
└─────────────────────────────┘
        │
        ├─────────────┬─────────────┐
        ▼             ▼             ▼
     是慢卡        不是慢卡        无瓶颈
        │             │             │
        ▼             │             ▼
  对该慢卡用      分析其他问题     结论输出
  专家建议深入
```

### 场景与能力匹配表

| 用户需求场景 | 推荐能力 | 理由 |
| --- | --- | --- |
| "分析这个 16 卡集群的慢卡问题" | 集群分析 → 专家建议 | 先定位慢卡，再深入诊断 |
| "这个单卡的数据有算子执行慢" | 专家建议 | 单卡场景专家建议更全面 |
| "帮我看看这个 profiling 数据" | 先判断数据格式 | 格式决定能力选择 |
| "通信带宽低是什么原因" | 集群分析 | 通信矩阵分析是集群分析强项 |
| "这个算子为什么慢" | 专家建议 | 算子问题诊断是专家建议强项 |
| "数据格式是 csv 的" | 专家建议 | 集群分析仅支持 db |

---

## 能力一：集群综合分析 (Cluster)

### 1.1 技能目标
在 Ascend 多卡/集群场景下，利用 msprof-analyze 的 `-m` 参数指定分析能力，对集群训练数据进行综合分析。用户只需说明要分析什么，系统自动选择对应的 `-m` 参数执行分析。

### 1.2 分析模式选择 (-m 参数)

| 分析能力 | 介绍 |
| --- | --- |
| cluster_time_summary | 提供集群训练过程中迭代耗时的拆解，帮助用户找到性能瓶颈。 |
| compute_op_sum | device侧运行的计算类算子汇总。 |
| freq_analysis | 识别aicore是否存在空闲（频率为800MHz）、异常（频率不为1800MHz或800MHz）的情况并给出分析结果。 |
| ep_load_balance | moe负载信息汇总分析。 |
| communication_time_sum | 集群场景通信时间和带宽汇总分析。 |
| hccl_sum | 通信类算子信息汇总。 |
| slow_rank | 根据当前的快慢卡统计算法，展示各个rank得出的快慢卡影响次数，识别慢卡出现的原因。 |
| cann_api_sum | CANN层API的汇总。 |
| free_analysis | 提供对Device侧大块空闲时间的自动分析能力，能够识别空闲时间产生的原因，帮助用户定位性能问题。 |
| all | 同时解析通信矩阵communication_matrix和通信耗时数据communication_time。 |

### 1.3 集群分析命令模板

```bash
# 【集群综合分析】
msprof-analyze -m <mode> -d <cluster_data_path> [-o <output_path>] [--force] --agent
```

命令示例：
```bash
msprof-analyze -m cluster_time_summary -d ./cluster_data -o ./output --agent
msprof-analyze -m free_analysis -d ./cluster_data -o ./output --agent
msprof-analyze -d ./cluster_data -o ./output --agent # 默认为all
```

### 1.4. 诊断先验知识库 (Expert Rules)

> ⚠️ 集群分析 (Cluster) 专用诊断规则
禁止仅凭单项指标字面意思下结论，必须严格遵守以下华为官方诊断逻辑：

* **【Host 下发瓶颈 (伪快卡)】**
    * **现象**：某卡（Rank X）的 `Free Time` 极长（占比 > 10% 或远超均值），且 `Compute` 和 `Communication` 时间异常偏短。
    * **定性**：**Rank X 绝非快卡，而是导致集群阻塞的"慢卡"。** CPU 下发慢导致其 NPU 饿死（产生巨大 Free Time）。当它终于发起通信时，其他卡已等待多时（其他卡 Wait 长），故其通信瞬间完成。
    * **验证**：使用 `free_analysis` 分析空闲时间原因，使用 `freq_analysis` 检查频率是否异常低。
* **【纯计算快慢卡】**
    * **现象**：各卡 `Free Time` 普遍较短且均匀，但某卡 `Compute Time` 显著大于均值。
    * **定性**：计算型慢卡。
* **【慢链路定位】**
    * **现象**：某条链路的 `Bandwidth(GB/s)` 显著低于同类型链路的均值。
    * **定性**：根据 Transport Type 判断链路类型，LOCAL（片内拷贝，速度最高）> HCCS/PCIE（节点内片间拷贝）> RDMA（节点间拷贝，速度最低）。同类型链路带宽差异过大表示存在慢链路。
    * **验证**：使用 `communication_matrix_sum` 查看各链路带宽。
* **【频率异常】**
    * **现象**：NPU 频率为 800MHz（空闲状态）或非 1800MHz/800MHz。
    * **定性**：频率异常可能表示算子等待或调度问题。
    * **验证**：使用 `freq_analysis` 检测频率异常。
* **【通信瓶颈】**
    * **现象**：通信耗时占比高，快慢卡明显。
    * **定性**：使用 `communication_bottleneck` 分析是 Host 侧下发慢还是 Device 侧计算慢导致通信等待。

### 1.5. 硬性约束 (MUST DO)

1. **必须使用 msprof-analyze 工具**：使用 `msprof-analyze` 命令进行集群分析。
2. **分析能力选择**：根据提示词选择分析模式，如果根据提示词无法确定使用哪个分析能力，请让用户明确使用哪一个分析能力，不能自己尝试。
3. **禁止孤立分析单卡**：在集群场景下，严禁仅分析单个 Rank 数据而不进行多卡对比，最好只基于最后输出的文件进行分析。命令行失败后不能继续读取各个rank的数据。
4. **必须分析时间占比**：输出报告必须包含各 Rank 的计算、通信、内存拷贝、空闲时间占比分析（适用于 cluster_time_summary 模式）。
5. **时间单位统一规范**：所有原始数据（单位为微秒），报告中必须自动换算为毫秒（ms）展示，并明确标注单位。

### 1.6. 标准操作流程 (SOP)

> ⚠️ 集群分析 (Cluster) 专用流程

1. **确认分析模式**：
   - 根据用户需求确定 `-m` 参数值（参见第2节映射表）。
   - 若用户要求"完整分析"或"全部"或没有明确要求分析哪一方面，默认使用 `all` 模式。

2. **执行分析命令**：
   - 执行命令：`msprof-analyze -m <mode> -d <cluster_data> [-o <output_path>] [--force] --agent`
   - 默认情况下，不需要使用`-o`参数，除非用户明确指定输出路径。

3. **读取输出结果**：
   - 根据命令运行返回的结果，读取生成的文件。
   - 如果返回信息有 error，或者 JSON 里 message 为空，不用往下运行。提示让用户自己执行命令（去掉 --agent 参数），查看日志定位原因。
   - 后续的分析可以结合json中的'suggestion'字段。

4. **数据解读与瓶颈定位**：
   - 如果根据表头无法明确数据的含义，可以参考[references/recipe_output_format_introduct.md](references/recipe_output_format_introduct.md)。
   - 对照【先验知识库】综合判断瓶颈类型
   - 如果从数据中无法做出明确结论，不要给出不准确的结论。

5. **输出报告**：按以下结构输出最终回复：
   - **分析概要**：使用的分析模式、执行的完整命令以及输出结果路径等信息。
   - **详细数据**：表格形式展示分析结果
   - **瓶颈定位**：瓶颈类型、关键证据（引用具体数值和占比）
   - **优化建议**：针对性建议

### 1.7 集群分析故障

* **【时间单位混淆】**
  - **现象**：输出的时间字段单位不明确。
  - **规避**：所有时间相关字段统一使用毫秒（ms），在报告中明确说明。
* **【profiler_level 设置过低】**
  - **现象**：无法获取通信带宽和通信矩阵信息。
  - **规避**：profiler_level 建议设置为 Level1 或更高。
* **【db 类型数据缺失】**
  - **现象**：Recipe 分析能力无法使用，报错缺少 db 文件。
  - **规避**：确认使用 db 类型数据，Ascend PyTorch Profiler 需指定 `export_type=["db"]`。
* **【Rank/Step ID 无效】**
  - **现象**：指定 --rank_list 或 --step_id 后无输出或报错。
  - **规避**：确认配置的 ID 在实际数据中存在。

---

## 能力二：专家建议分析 (Advisor)

### 2.1 技能目标
基于 Ascend PyTorch Profiler 或 MindSpore Profiler 采集的性能数据，使用 msprof-analyze advisor 功能进行自动分析，并输出性能调优建议。

### 2.2 分析模式

使用 `msprof-analyze advisor <subcommand>` 命令格式，支持三种分析模式，默认使用`all`：

| 子命令           | 说明 | 包含功能 |
|---------------| --- | --- |
| `all`         | 总体性能瓶颈 | 全部功能（计算 + 通信 + 调度） |
| `computation` | 计算瓶颈 | computation + Kernel compare |
| `schedule`    | 调度瓶颈 | schedule + API compare |

### 2.3 功能详情表

| 维度 | 功能 | 说明 |
| --- | --- | --- |
| overall | Overall Summary | 计算、通信、空闲等维度拆解 |
| overall | Environment Variable Issues | 环境变量设置推荐 |
| overall | slow rank | 慢卡识别 |
| overall | slow link | 慢链路识别 |
| computation | AICPU Issues | AI CPU 调优 |
| computation | Operator Dynamic Shape Issues | 识别动态Shape算子 |
| computation | AI Core Performance Analysis | MatMul、FlashAttentionScore 等算子分析 |
| computation | Block Dim Issues | Block Dim 算子调优 |
| computation | Operator No Bound Issues | 算子瓶颈分析 |
| computation | Fusion Issues | 融合算子图调优 |
| computation | AI Core Frequency Issues | AI Core 算子降频分析 |
| communication | Packet Analysis | 通信小包检测 |
| communication | Bandwidth Contention Analysis | 通信计算带宽抢占检测 |
| communication | Communication Retransmission Analysis | 通信重传检测 |
| communication | Byte Alignment Analysis | 通信算子字节对齐检测 |
| schedule | Affinity API Issues | 亲和API替换调优 |
| schedule | Operator Dispatch Issues | 识别算子下发问题(路径3/路径5) |
| schedule | SyncBatchNorm Issues | BatchNorm同步检测 |
| schedule | Synchronize Stream Issues | 流同步检测 |
| schedule | GC Analysis | 垃圾回收事件检测 |
| schedule | Fusible Operator Analysis | 检测 Host/MTE 瓶颈算子序列 |
| dataloader | Slow Dataloader Issues | 异常 dataloader 检测 |
| memory | Memory Operator Issues | 识别异常的内存申请释放操作 |
| comparison | Kernel compare | 标杆性能数据 Kernel 对比 |
| comparison | API compare | 标杆性能数据 API 对比 |

### 2.4 Advisor 命令模板

```bash
# 【总体性能瓶颈】
msprof-analyze advisor <subcommand> -d <profiling_path> [-o <output_path>] [-bp <benchmark_path>] [--force] --agent
```

命令示例：
```bash
# 单卡场景）
msprof-analyze advisor all -d ./profiling_data -o ./output --force --agent
# 集群场景
msprof-analyze advisor all -d ./cluster_profiling -o ./output --force --agent
```

### 2.5 数据路径要求

| 场景 | 路径要求 |
| --- | --- |
| 单卡 | 指定到 `*_ascend_pt` 或 `*_ascend_ms` 目录 |
| 多卡/集群 | 指定到 `*_ascend_pt` 或 `*_ascend_ms` 的父目录 |

AI: 数据位于 `/workspace/user_data`，共 4 个 `*_ascend_pt` 目录，已有 `cluster_analysis_output/cluster_analysis.db`（但结果不完整，需补齐）。先确认数据类型与工具可用性。[{'name': 'ls', 'args': {'path': '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT'}, 'id': 'call_00_qLrR1Z00fXZRexZX1E473900', 'type': 'tool_call'}, {'name': 'execute', 'args': {'command': 'which msprof-analyze && msprof-analyze --version 2>/dev/null || echo "msprof-analyze not found"'}, 'id': 'call_01_1kTj1rokeSKl3h22B14J7353', 'type': 'tool_call'}]
Tool: ['/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/analysis.db', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/api_statistic.csv', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_0.db', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/communication.json', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/communication_matrix.json', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/kernel_details.csv', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/op_statistic.csv', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/operator_details.csv', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/step_trace_time.csv', '/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/trace_view.json']
Tool: /usr/local/python3.11.14/bin/msprof-analyze
Version 8.5.2

[Command succeeded with exit code 0]
AI: 确认数据类型与工具版本后，先校验各 Rank DB 齐全性，并检查已有 cluster_analysis.db 内容。[{'name': 'execute', 'args': {'command': 'ls /workspace/user_data/*_ascend_pt/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_*.db 2>/dev/null; echo "---"; ls /workspace/user_data/df536040f370_*_ascend_pt/ASCEND_PROFILER_OUTPUT/ | head -50'}, 'id': 'call_00_7p8qdjIQVFS8PcqvDrDV2499', 'type': 'tool_call'}, {'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/workspace/user_data/cluster_analysis_output/cluster_analysis.db', 'query': "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"}, 'id': 'call_01_LFjswbmdqwBMH8ZTsGWq2638', 'type': 'tool_call'}]
Tool: /workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_0.db
/workspace/user_data/df536040f370_654_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_1.db
/workspace/user_data/df536040f370_657_20260108093239560_ascend_pt/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_2.db
/workspace/user_data/df536040f370_664_20260108093239560_ascend_pt/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_3.db
---
/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/:
analysis.db
api_statistic.csv
ascend_pytorch_profiler_0.db
communication.json
communication_matrix.json
kernel_details.csv
op_statistic.csv
operator_details.csv
step_trace_time.csv
trace_view.json

/workspace/user_data/df536040f370_654_20260108093239559_ascend_pt/ASCEND_PROFILER_OUTPUT/:
analysis.db
api_statistic.csv
ascend_pytorch_profiler_1.db
communication.json
communication_matrix.json
kernel_details.csv
op_statistic.csv
operator_details.csv
step_trace_time.csv
trace_view.json
trace_view_mindstudio_insight_data.db
trace_view_mindstudio_insight_data.db-shm
trace_view_mindstudio_insight_data.db-wal

/workspace/user_data/df536040f370_657_20260108093239560_ascend_pt/ASCEND_PROFILER_OUTPUT/:
analysis.db
api_statistic.csv
ascend_pytorch_profiler_2.db
communication.json
communication_matrix.json
kernel_details.csv
op_statistic.csv
operator_details.csv
step_trace_time.csv
trace_view.json

/workspace/user_data/df536040f370_664_20260108093239560_ascend_pt/ASCEND_PROFILER_OUTPUT/:
analysis.db
api_statistic.csv
ascend_pytorch_profiler_3.db
communication.json
communication_matrix.json
kernel_details.csv
op_statistic.csv
operator_details.csv
step_trace_time.csv
trace_view.json

[Command succeeded with exit code 0]
Tool: [{"type": "text", "text": "{\"rows\": [{\"name\": \"ClusterBaseInfo\"}, {\"name\": \"ClusterCommunicationBandwidth\"}, {\"name\": \"ClusterCommunicationMatrix\"}, {\"name\": \"ClusterCommunicationTime\"}, {\"name\": \"ClusterStepTraceTime\"}, {\"name\": \"CommunicationGroupMapping\"}, {\"name\": \"ExpertDeploymentInfo\"}, {\"name\": \"ExpertHotspotInfo\"}, {\"name\": \"HostInfo\"}, {\"name\": \"RankDeviceMap\"}, {\"name\": \"sqlite_sequence\"}, {\"name\": \"status_info\"}], \"row_count\": 12, \"truncated\": false, \"columns\": [\"name\"], \"summary\": null, \"csv_export\": null}", "id": "lc_65a5dc3e-ec4f-4542-9882-0097d8bc39b2"}]
AI: 数据类型确认为 **DB 格式、4 Rank**。已有 cluster_analysis.db 仅含通信/基础表，缺少 `cluster_time_summary`、`compute_op_sum`、`hccl_sum`、`slow_rank`、`slow_link`、`cann_api_sum` 等快慢卡关键证据，需用 msprof-analyze 逐项补齐。[{'name': 'execute', 'args': {'command': 'msprof-analyze -h 2>&1 | head -60'}, 'id': 'call_00_hzvE2FjJeWiW9UKQvM8H6131', 'type': 'tool_call'}]
Tool: Usage: msprof-analyze [OPTIONS] COMMAND [ARGS]...

  如果没有子命令，默认执行 cluster

Options:
  -V, -v, --version  8.5.2
  -h, -H, --help     Show this message and exit.

Commands:
  cluster          Analyze cluster data to locate performance bottleneck
  compare          Compare the performance differences between GPUs and NPUs.
  advisor          Analyze profiling datasets and give performance...
  auto-completion  Auto complete ma-advisor command in terminal, support
                   "bash(default)/zsh/fish".

Subcommands Options:

  cluster: Analyze cluster data to locate performance bottleneck
    --profiling_path, -d                    <required> path of the profiling data
    --output_path, -o                       <optional> Path of cluster analysis output
    --force                                 <optional> Indicates whether to skip verification of the owner, size, and permissions.
    --parallel_mode                         <optional> context mode
    --export_type                           <optional> recipe export type
    --rank_list                             <optional> Rank id list
    --step_id                               <optional> Step id
    --agent                                 <optional> Agent mode: save logs to temp file, only output structured JSON to terminal

  compare: Compare the performance differences between GPUs and NPUs.
    --profiling_path, -d                    <required> Path of the profiling data
    --benchmark_profiling_path, -bp         <required> Path of the benchmark data
    --enable_profiling_compare              <optional> Enable overall performance comparison
    --enable_operator_compare               <optional> Enable operator performance comparison
    --enable_memory_compare                 <optional> Enable operator memory comparison
    --enable_communication_compare          <optional> Enable communication performance comparison
    --enable_api_compare                    <optional> Enable API performance comparison
    --enable_kernel_compare                 <optional> Enable kernel performance comparison
    --disable_details                       <optional> Hide detailed comparison
    --disable_module                        <optional> Hide module comparison
    --output_path, -o                       <optional> Path of comparison result
    --max_kernel_num                        <optional> The number of kernels per torch op is limited
    --op_name_map                           <optional> The mapping of operator names equivalent to GPUs and NPUs in the form of dictionaries
    --use_input_shape                       <optional> Enable precise matching of operators
    --gpu_flow_cat                          <optional> Identifier of the GPU connection
    --base_step                             <optional> Comparison step for performance data to be compared
    --comparison_step                       <optional> Comparison step for benchmark performance data
    --force                                 <optional> Indicates whether to skip verification of the owner, size, and permissions.
    --use_kernel_type                       <optional> Indicates whether kernel compare use op_statistic.csv

  advisor: Analyze profiling datasets and give...

    advisor all: Analyze timeline fusion operators, operators and graph, operators dispatching and cluster.
      --profiling_path, -d                    <required> Directory of profiling data
      --benchmark_profiling_path, -bp         <optional> Directory of benchmark profiling data, used for compare performance
      --output_path, -o                       <optional> Path of analysis output
      --cann_version, -cv                     <optional> The CANN software version, which can be viewed by executing the following command: "cat /usr/local/Ascend/ascend-toolkit/latest/aarch64-linux/ascend_toolkit_install.info"
      --torch_version, -tv                    <optional> The runtime torch version, which can be detected by exec command "pip show torch"
      -pt, --profiling_type                   <optional> Enter the profiling type, selectable range pytorch, mindspore, mslite ,msprof
      --force                                 <optional> Indicates whether to skip verification of the owner, size, and permissions.
      -l, --language                          <optional> Language of the profiling advisor.
      --agent                                 <optional> Agent mode: save logs to temp file, only output structured JSON to terminal

[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'msprof-analyze cluster -h 2>&1 | head -80'}, 'id': 'call_00_mD5mjjqvrXHr6C3NTsTK1278', 'type': 'tool_call'}]
Tool: [2026-08-19 08:32:24][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
Usage: msprof-analyze cluster [OPTIONS] [ARGS]...

Options:
  -d, --profiling_path DIRECTORY  path of the profiling data  [required]
  -m, --mode [all|communication_time|communication_matrix|calibrate_npu_gpu|cann_api_sum|cluster_time_compare_summary|cluster_time_summary|communication_bottleneck|communication_group_map|communication_matrix_sum|communication_time_sum|computational_op_masking|compute_op_sum|ep_load_balance|export_summary|free_analysis|freq_analysis|hccl_sum|module_statistic|mstx2commop|mstx_sum|operator_mfu|p2p_pairing|pp_chart|slow_link|slow_rank]
  -o, --output_path DIRECTORY     Path of cluster analysis output
  --force                         Indicates whether to skip verification of
                                  the owner, size, and permissions.
  --parallel_mode TEXT            context mode
  --export_type [db|notebook|text]
                                  recipe export type
  --rank_list TEXT                Rank id list
  --step_id INTEGER               Step id
  -V, -v, --version               8.5.2
  --agent                         Agent mode: save logs to temp file, only
                                  output structured JSON to terminal
  -H, -h, --help                  Show this message and exit.

[Command succeeded with exit code 0]
AI: 现在按 skill 要求逐项执行 msprof-analyze 集群分析能力，先跑 `cluster_time_summary`、`slow_rank`、`slow_link`。[{'name': 'execute', 'args': {'command': 'cd /workspace/user_data && msprof-analyze cluster -m cluster_time_summary -d /workspace/user_data -o /workspace/user_data/analysis/cluster_time_summary --force --agent 2>&1 | tail -40', 'timeout': 600}, 'id': 'call_00_C4KyfFqL0pUxL0xB6Cug2251', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:32:29][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
/usr/local/python3.11.14/lib/python3.11/site-packages/msprof_analyze/cluster_analyse/recipes/base_recipe_analysis.py:141: UserWarning: pandas only supports SQLAlchemy connectable (engine/connection) or database string URI or sqlite3 DBAPI2 connection. Other DBAPI2 objects are not tested. Please consider using SQLAlchemy.
  data.to_sql(table_name, conn, if_exists='replace', index=index)
{"status": "success", "code": 0, "message": {}, "suggestion": "", "log_path": "/tmp/msprof_analyze_1787128349.log"}

[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'cd /workspace/user_data && msprof-analyze cluster -m slow_rank -d /workspace/user_data -o /workspace/user_data/analysis/slow_rank --force --agent 2>&1 | tail -20', 'timeout': 600}, 'id': 'call_00_ZgMS6903FcUYKtnSyD9S3169', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:33:04][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
{"status": "success", "code": 0, "message": {"db_path": "/workspace/user_data/analysis/slow_rank/cluster_analysis_output/cluster_analysis.db", "tables": ["SlowRank", "SlowOpStats"]}, "suggestion": "根据当前的快慢卡统计算法，展示各个rank得出的快慢卡影响次数，识别慢卡出现的原因。", "log_path": "/tmp/msprof_analyze_1787128384.log"}

[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'cd /workspace/user_data && msprof-analyze cluster -m slow_link -d /workspace/user_data -o /workspace/user_data/analysis/slow_link --force --agent 2>&1 | tail -20', 'timeout': 600}, 'id': 'call_00_ET_WbGu9ukrydE9H1QFZgIB5686', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:33:38][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
{"status": "error", "code": 1, "message": {"err": "Failed to make directory: cluster_analysis_output"}, "suggestion": "", "log_path": "/tmp/msprof_analyze_1787128418.log"}

[Command succeeded with exit code 0]
AI: slow_link 报 "Failed to make directory: cluster_analysis_output"，检查输出目录结构。[{'name': 'ls', 'args': {'path': '/workspace/user_data/analysis/slow_link'}, 'id': 'call_00_aaH8O0Bpo8rt4JsMhGxP6735', 'type': 'tool_call'}, {'name': 'execute', 'args': {'command': 'ls -la /workspace/user_data/analysis/ 2>/dev/null; echo "---"; find /workspace/user_data/analysis -maxdepth 3 -type d 2>/dev/null | head -30'}, 'id': 'call_01_RkvReZi1PFTw4xaTh8wb9192', 'type': 'tool_call'}]
Tool: []
Tool: total 20
drwxr-xr-x. 5 root root 4096 Aug 19 08:33 .
drwxr-xr-x. 9 root root 4096 Aug 19 08:32 ..
drwxr-x---. 3 root root 4096 Aug 19 08:32 cluster_time_summary
drwxr-x---. 2 root root 4096 Aug 19 08:33 slow_link
drwxr-x---. 3 root root 4096 Aug 19 08:33 slow_rank
---
/workspace/user_data/analysis
/workspace/user_data/analysis/cluster_time_summary
/workspace/user_data/analysis/cluster_time_summary/cluster_analysis_output
/workspace/user_data/analysis/slow_rank
/workspace/user_data/analysis/slow_rank/cluster_analysis_output
/workspace/user_data/analysis/slow_link

[Command succeeded with exit code 0]
AI: slow_link 输出目录为空，重试一次。[{'name': 'execute', 'args': {'command': 'cd /workspace/user_data && rm -rf /workspace/user_data/analysis/slow_link && msprof-analyze cluster -m slow_link -d /workspace/user_data -o /workspace/user_data/analysis/slow_link --force --agent 2>&1 | tail -20', 'timeout': 600}, 'id': 'call_00_XhRZRP9aa0QTcQStW6SZ3318', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:34:31][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
{"status": "error", "code": 1, "message": {"err": "Failed to make directory: cluster_analysis_output"}, "suggestion": "", "log_path": "/tmp/msprof_analyze_1787128471.log"}

[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'tail -40 /tmp/msprof_analyze_1787128471.log'}, 'id': 'call_00_ET_Y0f6Ctw5cu6t22g6WzT17219', 'type': 'tool_call'}]
Tool: [2026-08-19 08:34:32][INFO][cluster_analysis.py:71] Start allocate profiling data.
[2026-08-19 08:34:52][INFO][cluster_analysis.py:77] Finish allocate profiling data: 4 ranks, data type: db, profiling type: pytorch

[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'cd /workspace/user_data && msprof-analyze cluster -m slow_link -d /workspace/user_data -o /workspace/user_data/analysis/slow_link --force 2>&1 | tail -50', 'timeout': 600}, 'id': 'call_00_LUj9VJ7tbOhMqDLRChw37454', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:34:59][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
[2026-08-19 08:34:59][INFO][cluster_analysis.py:71] Start allocate profiling data.
[2026-08-19 08:35:17][INFO][cluster_analysis.py:77] Finish allocate profiling data: 4 ranks, data type: db, profiling type: pytorch
Traceback (most recent call last):
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/msprof_analyze/prof_common/path_manager.py", line 220, in make_dir_safety
    os.makedirs(path, mode=cls.DATA_DIR_AUTHORITY, exist_ok=True)
  File "<frozen os>", line 225, in makedirs
OSError: [Errno 28] No space left on device: '/workspace/user_data/analysis/slow_link/cluster_analysis_output'

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/usr/local/python3.11.14/bin/msprof-analyze", line 7, in <module>
    sys.exit(msprof_analyze_cli())
             ^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/click/core.py", line 1485, in __call__
    return self.main(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/click/core.py", line 1406, in main
    rv = self.invoke(ctx)
         ^^^^^^^^^^^^^^^^
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/click/core.py", line 1873, in invoke
    return _process_result(sub_ctx.command.invoke(sub_ctx))
                           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/click/core.py", line 1269, in invoke
    return ctx.invoke(self.callback, **ctx.params)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/click/core.py", line 824, in invoke
    return callback(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/msprof_analyze/prof_common/json_output.py", line 64, in wrapper
    return func(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/msprof_analyze/cli/cluster_cli.py", line 53, in cluster_cli
    Interface(kwargs).run()
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/msprof_analyze/cluster_analyse/cluster_analysis.py", line 123, in run
    FileManager.create_output_dir(self.cluster_analysis_output_path, is_overwrite=True)
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/msprof_analyze/prof_common/file_manager.py", line 186, in create_output_dir
    PathManager.make_dir_safety(output_path)
  File "/usr/local/python3.11.14/lib/python3.11/site-packages/msprof_analyze/prof_common/path_manager.py", line 222, in make_dir_safety
    raise RuntimeError(msg) from err
RuntimeError: Failed to make directory: cluster_analysis_output

[Command succeeded with exit code 0]
AI: 失败根因是磁盘空间不足（Errno 28）。检查磁盘占用并清理。[{'name': 'execute', 'args': {'command': 'df -h /workspace /tmp 2>/dev/null; echo "---"; du -sh /workspace/user_data/analysis/* 2>/dev/null; echo "---"; du -sh /workspace/user_data/* 2>/dev/null | sort -h | tail -10'}, 'id': 'call_00_0tuVUUq7TdCnRrVUVrpM5041', 'type': 'tool_call'}]
Tool: Filesystem      Size  Used Avail Use% Mounted on
overlay         300G   22G  279G   8% /
overlay         300G   22G  279G   8% /
---
8.0K	/workspace/user_data/analysis/cluster_time_summary
4.0K	/workspace/user_data/analysis/slow_link
60K	/workspace/user_data/analysis/slow_rank
---
512	/workspace/user_data/3.2qwen3-8B-w8a8.yaml
512	/workspace/user_data/qwen3-8B-w8a8.yaml
2.5K	/workspace/user_data/=0.22.0
76K	/workspace/user_data/analysis
380K	/workspace/user_data/cluster_analysis_output
79M	/workspace/user_data/Qwen3-32B.zip
153M	/workspace/user_data/df536040f370_657_20260108093239560_ascend_pt
153M	/workspace/user_data/df536040f370_664_20260108093239560_ascend_pt
168M	/workspace/user_data/df536040f370_651_20260108093239559_ascend_pt
190M	/workspace/user_data/df536040f370_654_20260108093239559_ascend_pt

[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'df -i /workspace 2>/dev/null; echo "---"; stat -f /workspace 2>/dev/null | head -20'}, 'id': 'call_00_Yrxu2dQfVXvjGf8ZOyAl3347', 'type': 'tool_call'}]
Tool: Filesystem        Inodes   IUsed     IFree IUse% Mounted on
overlay        375073536 5393270 369680266    2% /
---
  File: "/workspace"
    ID: fd0200000000 Namelen: 255     Type: overlayfs
Block size: 4096       Fundamental block size: 4096
Blocks: Total: 78643200   Free: 73106376   Available: 73106376
Inodes: Total: 375073536  Free: 369680266

[Command succeeded with exit code 0]
AI: 磁盘与 inode 均充足，可能是瞬时异常。手动创建目录后重试。[{'name': 'execute', 'args': {'command': 'mkdir -p /workspace/user_data/analysis/slow_link/cluster_analysis_output && ls -ld /workspace/user_data/analysis/slow_link/ && cd /workspace/user_data && msprof-analyze cluster -m slow_link -d /workspace/user_data -o /workspace/user_data/analysis/slow_link --force --agent 2>&1 | tail -10', 'timeout': 600}, 'id': 'call_00_KP2kmcz0VkLEHDcVme516944', 'type': 'tool_call'}]
Tool: [stderr] mkdir: cannot create directory '/workspace/user_data/analysis/slow_link/cluster_analysis_output': No space left on device

Exit code: 1
[Command failed with exit code 1]
AI: `mkdir` 也报 ENOSPC，但 df 显示空间充足。排查挂载与配额情况。[{'name': 'execute', 'args': {'command': 'mount | grep -i workspace; echo "---"; mount | grep -E \'user_data|overlay\' | head; echo "---"; cat /proc/mounts | grep -E \'workspace\' | head'}, 'id': 'call_00_apHcorZflliL9vxaOxdD9147', 'type': 'tool_call'}]
Tool: 179.121.63.2:/vol_bigfile on /workspace/user_data type fuse.glusterfs (rw,relatime,user_id=0,group_id=0,default_permissions,allow_other,max_read=131072)
179.121.63.2:/vol_bigfile on /workspace/shared_assets type fuse.glusterfs (ro,relatime,user_id=0,group_id=0,default_permissions,allow_other,max_read=131072)
---
overlay on / type overlay (rw,relatime,seclabel,lowerdir=/mnt/docker/overlay2/l/NF5Q7G33RKBAD6EHOPP6NCU3N6:/mnt/docker/overlay2/l/QT7ZRB6IIJNSZQU6XJU65N2PAV:/mnt/docker/overlay2/l/ZWCHISSI33EXFOSZS4CD47I3J3:/mnt/docker/overlay2/l/OTFP4CF3UVFU4J6F7VHTCVJLWZ:/mnt/docker/overlay2/l/URTR7GM55BGQEUEW7KYRNBEOED:/mnt/docker/overlay2/l/RADQB5GPCNQYWMIA6A7WB3SMW3:/mnt/docker/overlay2/l/4L5ICFP5PUP7DQFBOLV5XC7NAM:/mnt/docker/overlay2/l/IVKCBB2BLZ4NN7YUY4G7VTSVRK:/mnt/docker/overlay2/l/WJEJJDGDOROERD53AK7JRVKDW6:/mnt/docker/overlay2/l/FSYPL5T3ALPB6IH22MP4JUQ7PH:/mnt/docker/overlay2/l/P5OZBOSLZA2EONRLDD5JYUAE7J:/mnt/docker/overlay2/l/LVR3BX4TEETYUWC3CP7M3AIZER:/mnt/docker/overlay2/l/EFGZOYXX5ASNUZCTQDXT4HMMKE:/mnt/docker/overlay2/l/DP2KYU37ROJBQRCSFIH7ZEKVZF:/mnt/docker/overlay2/l/XWLE5Z5EE6X7ZRBRY4U7R7HZ5N:/mnt/docker/overlay2/l/MIWGPBLQY6OSXXTPTQKOGLOYGM:/mnt/docker/overlay2/l/HFYCLRZYCQCTB5HUJWNFS5NEKH,upperdir=/mnt/docker/overlay2/a3464d7f531d0d4e87106c92a3c0f636d2ce8989d68350891a07d1d1b846a979/diff,workdir=/mnt/docker/overlay2/a3464d7f531d0d4e87106c92a3c0f636d2ce8989d68350891a07d1d1b846a979/work)
179.121.63.2:/vol_bigfile on /workspace/user_data type fuse.glusterfs (rw,relatime,user_id=0,group_id=0,default_permissions,allow_other,max_read=131072)
---
179.121.63.2:/vol_bigfile /workspace/user_data fuse.glusterfs rw,relatime,user_id=0,group_id=0,default_permissions,allow_other,max_read=131072 0 0
179.121.63.2:/vol_bigfile /workspace/shared_assets fuse.glusterfs ro,relatime,user_id=0,group_id=0,default_permissions,allow_other,max_read=131072 0 0

[Command succeeded with exit code 0]
AI: `/workspace/user_data` 是 glusterfs 挂载，可能有自己的容量限制。检查该挂载点容量。[{'name': 'execute', 'args': {'command': 'df -h /workspace/user_data; echo "---"; df -i /workspace/user_data'}, 'id': 'call_00_PxxUNky3fSVvBYJeK9vk1811', 'type': 'tool_call'}]
Tool: Filesystem                 Size  Used Avail Use% Mounted on
179.121.63.2:/vol_bigfile   35T   35T  129G 100% /workspace/user_data
---
Filesystem                    Inodes     IUsed     IFree IUse% Mounted on
179.121.63.2:/vol_bigfile 1117627120 190787128 926839992   18% /workspace/user_data

[Command succeeded with exit code 0]
AI: glusterfs 卷已 100% 满（可用 129G 但配额限制导致写失败）。将后续分析输出改到 `/tmp`（本地 overlay，空间充足），并清理失败目录。[{'name': 'execute', 'args': {'command': 'rm -rf /workspace/user_data/analysis/slow_link && mkdir -p /tmp/cluster_analysis && df -h /tmp | tail -1'}, 'id': 'call_00_NuhKGvUJptzPvuqbUoKE4554', 'type': 'tool_call'}]
Tool: overlay         300G  5.9G  295G   2% /

[Command succeeded with exit code 0]
AI: 将输出目录改到 `/tmp` 后继续逐项执行。[{'name': 'execute', 'args': {'command': 'msprof-analyze cluster -m slow_link -d /workspace/user_data -o /tmp/cluster_analysis/slow_link --force --agent 2>&1 | tail -8', 'timeout': 600}, 'id': 'call_00_YCeMAFdMgOLuiQmKOyHp3461', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:36:24][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.

Processing database data...:   0%|          | 0/1 [00:00<?, ?it/s]
Processing database data...: 100%|██████████| 1/1 [00:00<00:00, 1320.62it/s]
{"status": "success", "code": 0, "message": {}, "suggestion": "", "log_path": "/tmp/msprof_analyze_1787128584.log"}

[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'msprof-analyze cluster -m compute_op_sum -d /workspace/user_data -o /tmp/cluster_analysis/compute_op_sum --force --agent 2>&1 | tail -8', 'timeout': 600}, 'id': 'call_00_MWHthBqpOWCu7zvIgZhs8430', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:36:49][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
{"status": "success", "code": 0, "message": {"db_path": "/tmp/cluster_analysis/compute_op_sum/cluster_analysis_output/cluster_analysis.db", "tables": ["ComputeOpAllRankStats", "ComputeOpPerRankStatsByOpType", "ComputeOpPerRankStatsByOpName"]}, "suggestion": "\n    ComputeOpAllRankStats / all_stats.csv：汇总所有 rank 上各类计算算子按 OpType + TaskType 的耗时分布，用它先找整体最重的计算类型。\n    ComputeOpPerRankStatsByOpType / per_rank_stats_by_optype.csv：按 rank 统计各类计算算子的耗时分布，用它定位是哪几个 rank 的某类计算算子异常慢。\n    ComputeOpPerRankStatsByOpName / per_rank_stats_by_opname.csv：按 rank 统计具体算子名、类型和输入 shape 的耗时分布，用它继续下钻到“是哪一个具体算子/shape 组合导致变慢”。\n    ", "log_path": "/tmp/msprof_analyze_1787128609.log"}

[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'msprof-analyze cluster -m hccl_sum -d /workspace/user_data -o /tmp/cluster_analysis/hccl_sum --force --agent 2>&1 | tail -8', 'timeout': 600}, 'id': 'call_00_ET_WMaJQfRfOFd5JyUyEogx9852', 'type': 'tool_call'}, {'name': 'execute', 'args': {'command': 'msprof-analyze cluster -m cann_api_sum -d /workspace/user_data -o /tmp/cluster_analysis/cann_api_sum --force --agent 2>&1 | tail -8', 'timeout': 600}, 'id': 'call_01_ET_KvEHmyTh4vSQpQocSeNq1566', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:37:12][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
{"status": "success", "code": 0, "message": {"db_path": "/tmp/cluster_analysis/hccl_sum/cluster_analysis_output/cluster_analysis.db", "tables": ["HcclAllRankStats", "HcclPerRankStats", "HcclTopOpStats", "HcclGroupNameMap"]}, "suggestion": "\n    HcclAllRankStats / all_stats.csv：汇总所有 rank 上各类 HCCL 通信算子的耗时分布，用它找整体最耗时、波动最大的通信类型，重点关注集合通信算子。\n    HcclPerRankStats / rank_stats.csv：按 rank 统计各类 HCCL 通信算子的耗时分布，用它定位是哪几个 rank 的通信异常。\n    HcclTopOpStats / top_op_stats.csv：统计全局最慢的 Top N 通信算子实例并给出最慢/最快 rank，用它从“通信类型”继续下钻到“具体慢算子”。\n    HcclGroupNameMap / group_name_map.csv：给出通信域和其中包含的 ranks，用它辅助判断异常通信是否集中在某个通信组。\n    ", "log_path": "/tmp/msprof_analyze_1787128632.log"}

[Command succeeded with exit code 0]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:37:12][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
{"status": "success", "code": 0, "message": {"db_path": "/tmp/cluster_analysis/cann_api_sum/cluster_analysis_output/cluster_analysis.db", "tables": ["CannApiSumRank", "CannApiSum"]}, "suggestion": "\n    CannApiSumRank / rank_stats.csv：按 rank 分别统计各类 CANN API 的耗时与分布，用它再定位是哪些 rank 导致该 API 异常。\n    CannApiSum / all_stats.csv：汇总所有 rank 上各类 CANN API 的耗时与分布，用它先定位整体最耗时、波动最大的热点 API。\n    ", "log_path": "/tmp/msprof_analyze_1787128632.log"}

[Command succeeded with exit code 0]
AI: 全部分析能力执行成功。现在读取各模块结果 DB，整理集群级证据。[{'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/workspace/user_data/analysis/cluster_time_summary/cluster_analysis_output/cluster_analysis.db', 'query': "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"}, 'id': 'call_00_GSVglMqjbkSUSX6VCOYP8455', 'type': 'tool_call'}, {'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/workspace/user_data/analysis/slow_rank/cluster_analysis_output/cluster_analysis.db', 'query': "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"}, 'id': 'call_01_TJRmljgPJ1c36FJl7oNa3821', 'type': 'tool_call'}]
Tool: [{"type": "text", "text": "{\n  \"error\": \"FILE_NOT_FOUND\",\n  \"message\": \"Database file not found: /workspace/user_data/analysis/cluster_time_summary/cluster_analysis_output/cluster_analysis.db\"\n}", "id": "lc_1e7455a8-6c74-4ffd-b6e0-5b252aa21481"}]
Tool: [{"type": "text", "text": "{\"rows\": [{\"name\": \"SlowOpStats\"}, {\"name\": \"SlowRank\"}], \"row_count\": 2, \"truncated\": false, \"columns\": [\"name\"], \"summary\": null, \"csv_export\": null}", "id": "lc_b25febbf-08f9-4110-b992-e4132cd883f2"}]
AI: [{'name': 'execute', 'args': {'command': 'find /workspace/user_data/analysis/cluster_time_summary -type f | head -30; echo "==="; find /tmp/cluster_analysis -maxdepth 2 -type d'}, 'id': 'call_00_sZkmWOvct2KBTEdD4xkH1211', 'type': 'tool_call'}]
Tool: ===
/tmp/cluster_analysis
/tmp/cluster_analysis/slow_link
/tmp/cluster_analysis/slow_link/cluster_analysis_output
/tmp/cluster_analysis/compute_op_sum
/tmp/cluster_analysis/compute_op_sum/cluster_analysis_output
/tmp/cluster_analysis/cann_api_sum
/tmp/cluster_analysis/cann_api_sum/cluster_analysis_output
/tmp/cluster_analysis/hccl_sum
/tmp/cluster_analysis/hccl_sum/cluster_analysis_output

[Command succeeded with exit code 0]
AI: cluster_time_summary 输出因 glusterfs 卷满而丢失，重新执行到 `/tmp`。[{'name': 'execute', 'args': {'command': 'msprof-analyze cluster -m cluster_time_summary -d /workspace/user_data -o /tmp/cluster_analysis/cluster_time_summary --force --agent 2>&1 | tail -8', 'timeout': 600}, 'id': 'call_00_LEgD7mQH33HU9NkyQFxb2959', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:37:48][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
{"status": "success", "code": 0, "message": {"db_path": "/tmp/cluster_analysis/cluster_time_summary/cluster_analysis_output/cluster_analysis.db", "tables": ["ClusterTimeSummary"]}, "suggestion": "ClusterTimeSummary / cluster_time_summary.csv：分析计算、通信、内存拷贝、空闲时间占比，找到性能瓶颈。", "log_path": "/tmp/msprof_analyze_1787128668.log"}

[Command succeeded with exit code 0]
AI: [{'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/tmp/cluster_analysis/cluster_time_summary/cluster_analysis_output/cluster_analysis.db', 'query': 'SELECT * FROM ClusterTimeSummary'}, 'id': 'call_00_G6SHXN5Xm3UylwuqUatM4063', 'type': 'tool_call'}, {'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/workspace/user_data/analysis/slow_rank/cluster_analysis_output/cluster_analysis.db', 'query': 'SELECT * FROM SlowRank'}, 'id': 'call_01_A92tT6cxRczcFHCu7G2q2431', 'type': 'tool_call'}]
Tool: [{"type": "text", "text": "{\"rows\": [{\"rank\": 0, \"step\": -1, \"stepTime\": 1414032.6800000002, \"computation\": 333767.98, \"communicationNotOverlapComputation\": 447823.3, \"communicationOverlapComputation\": 79.88, \"communication\": 447903.18, \"free\": 625006.1, \"communicationWaitStageTime\": 441092.125, \"communicationTransmitStageTime\": 6811.055, \"memory\": 7501.58, \"memoryNotOverlapComputationCommunication\": 7435.3}, {\"rank\": 1, \"step\": -1, \"stepTime\": 1413914.26, \"computation\": 332578.72, \"communicationNotOverlapComputation\": 203812.32, \"communicationOverlapComputation\": 0.0, \"communication\": 203812.32, \"free\": 869528.84, \"communicationWaitStageTime\": 197001.265, \"communicationTransmitStageTime\": 6811.055, \"memory\": 8069.56, \"memoryNotOverlapComputationCommunication\": 7994.38}, {\"rank\": 2, \"step\": -1, \"stepTime\": 1414579.9440000001, \"computation\": 335417.581, \"communicationNotOverlapComputation\": 247767.73, \"communicationOverlapComputation\": 0.0, \"communication\": 247767.73, \"free\": 823581.263, \"communicationWaitStageTime\": 240956.67500000002, \"communicationTransmitStageTime\": 6811.055, \"memory\": 7856.312, \"memoryNotOverlapComputationCommunication\": 7813.37}, {\"rank\": 3, \"step\": -1, \"stepTime\": 1415349.123, \"computation\": 334223.605, \"communicationNotOverlapComputation\": 444981.869, \"communicationOverlapComputation\": 0.0, \"communication\": 444981.869, \"free\": 628357.424, \"communicationWaitStageTime\": 438170.814, \"communicationTransmitStageTime\": 6811.055, \"memory\": 7845.209, \"memoryNotOverlapComputationCommunication\": 7786.225}], \"row_count\": 4, \"truncated\": false, \"columns\": [\"rank\", \"step\", \"stepTime\", \"computation\", \"communicationNotOverlapComputation\", \"communicationOverlapComputation\", \"communication\", \"free\", \"communicationWaitStageTime\", \"communicationTransmitStageTime\", \"memory\", \"memoryNotOverlapComputationCommunication\"], \"summary\": null, \"csv_export\": null}", "id": "lc_12bdf46d-280a-4048-8bb4-c3f36a8c9286"}]
Tool: [{"type": "text", "text": "{\"rows\": [{\"rankId\": 1, \"slowAffectCount\": 137}, {\"rankId\": 2, \"slowAffectCount\": 129}], \"row_count\": 2, \"truncated\": false, \"columns\": [\"rankId\", \"slowAffectCount\"], \"summary\": null, \"csv_export\": null}", "id": "lc_ea069423-2b68-4164-810e-2e8bd55ce5e1"}]
AI: 集群级宏观证据已到手，且出现清晰的快慢分化模式。现在读取计算、通信、CANN API 的 Rank 级明细，做交叉验证。[{'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/tmp/cluster_analysis/cann_api_sum/cluster_analysis_output/cluster_analysis.db', 'query': 'SELECT * FROM CannApiSumRank ORDER BY rankId, apiType LIMIT 40'}, 'id': 'call_00_cEb0Nhk28l4gbTHjpjYS5527', 'type': 'tool_call'}, {'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/tmp/cluster_analysis/compute_op_sum/cluster_analysis_output/cluster_analysis.db', 'query': 'SELECT * FROM ComputeOpAllRankStats ORDER BY totalTime DESC LIMIT 15'}, 'id': 'call_01_OtcELmhiRIV6mDLEKi544565', 'type': 'tool_call'}]
Tool: [{"type": "text", "text": "{\n  \"error\": \"SQL_EXECUTION_FAILED\",\n  \"message\": \"Execution failed on sql 'SELECT * FROM CannApiSumRank ORDER BY rankId, apiType LIMIT 40': no such column: rankId\"\n}", "id": "lc_3410a4a1-eff5-44ee-8ec2-0c0a5384892f"}]
Tool: [{"type": "text", "text": "{\n  \"error\": \"SQL_EXECUTION_FAILED\",\n  \"message\": \"Execution failed on sql 'SELECT * FROM ComputeOpAllRankStats ORDER BY totalTime DESC LIMIT 15': no such column: totalTime\"\n}", "id": "lc_86a4221a-c931-463e-9b72-692b01a72fe4"}]
AI: 先查各表 schema。[{'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/tmp/cluster_analysis/cann_api_sum/cluster_analysis_output/cluster_analysis.db', 'query': "SELECT sql FROM sqlite_master WHERE type='table'"}, 'id': 'call_00_rAUMI9WG8VA85ybtLz8Z6743', 'type': 'tool_call'}, {'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/tmp/cluster_analysis/compute_op_sum/cluster_analysis_output/cluster_analysis.db', 'query': "SELECT sql FROM sqlite_master WHERE type='table'"}, 'id': 'call_01_KHIMRtvTmlP6ut3OAvf74612', 'type': 'tool_call'}]
Tool: [{"type": "text", "text": "{\"rows\": [{\"sql\": \"CREATE TABLE \\\"CannApiSumRank\\\" (\\n\\\"name\\\" TEXT,\\n  \\\"durationRatio\\\" REAL,\\n  \\\"totalTimeNs\\\" INTEGER,\\n  \\\"totalCount\\\" INTEGER,\\n  \\\"averageNs\\\" REAL,\\n  \\\"minNs\\\" REAL,\\n  \\\"Q1Ns\\\" REAL,\\n  \\\"medNs\\\" REAL,\\n  \\\"Q3Ns\\\" REAL,\\n  \\\"maxNs\\\" REAL,\\n  \\\"stdev\\\" REAL,\\n  \\\"rank\\\" INTEGER\\n)\"}, {\"sql\": \"CREATE TABLE \\\"CannApiSum\\\" (\\n\\\"name\\\" TEXT,\\n  \\\"timeRatio\\\" REAL,\\n  \\\"totalTimeNs\\\" INTEGER,\\n  \\\"totalCount\\\" INTEGER,\\n  \\\"averageNs\\\" REAL,\\n  \\\"Q1Ns\\\" REAL,\\n  \\\"medNs\\\" REAL,\\n  \\\"Q3Ns\\\" REAL,\\n  \\\"minNs\\\" REAL,\\n  \\\"maxNs\\\" REAL,\\n  \\\"stdev\\\" REAL,\\n  \\\"minRank\\\" TEXT,\\n  \\\"maxRank\\\" TEXT\\n)\"}], \"row_count\": 2, \"truncated\": false, \"columns\": [\"sql\"], \"summary\": null, \"csv_export\": null}", "id": "lc_ed652428-96a1-44d4-8b25-f5c66fa9dc43"}]
Tool: [{"type": "text", "text": "{\"rows\": [{\"sql\": \"CREATE TABLE \\\"ComputeOpAllRankStats\\\" (\\n\\\"OpType\\\" TEXT,\\n  \\\"TaskType\\\" TEXT,\\n  \\\"Count\\\" INTEGER,\\n  \\\"MeanNs\\\" REAL,\\n  \\\"StdNs\\\" REAL,\\n  \\\"MinNs\\\" REAL,\\n  \\\"Q1Ns\\\" REAL,\\n  \\\"MedianNs\\\" REAL,\\n  \\\"Q3Ns\\\" REAL,\\n  \\\"MaxNs\\\" REAL,\\n  \\\"SumNs\\\" REAL\\n)\"}, {\"sql\": \"CREATE TABLE \\\"ComputeOpPerRankStatsByOpType\\\" (\\n\\\"OpType\\\" TEXT,\\n  \\\"TaskType\\\" TEXT,\\n  \\\"Count\\\" INTEGER,\\n  \\\"MeanNs\\\" REAL,\\n  \\\"StdNs\\\" REAL,\\n  \\\"MinNs\\\" REAL,\\n  \\\"Q1Ns\\\" REAL,\\n  \\\"MedianNs\\\" REAL,\\n  \\\"Q3Ns\\\" REAL,\\n  \\\"MaxNs\\\" REAL,\\n  \\\"SumNs\\\" REAL,\\n  \\\"Rank\\\" INTEGER\\n)\"}, {\"sql\": \"CREATE TABLE \\\"ComputeOpPerRankStatsByOpName\\\" (\\n\\\"OpName\\\" TEXT,\\n  \\\"OpType\\\" TEXT,\\n  \\\"TaskType\\\" TEXT,\\n  \\\"InputShapes\\\" TEXT,\\n  \\\"Count\\\" INTEGER,\\n  \\\"MeanNs\\\" REAL,\\n  \\\"StdNs\\\" REAL,\\n  \\\"MinNs\\\" REAL,\\n  \\\"Q1Ns\\\" REAL,\\n  \\\"MedianNs\\\" REAL,\\n  \\\"Q3Ns\\\" REAL,\\n  \\\"MaxNs\\\" REAL,\\n  \\\"SumNs\\\" REAL,\\n  \\\"Rank\\\" INTEGER\\n)\"}], \"row_count\": 3, \"truncated\": false, \"columns\": [\"sql\"], \"summary\": null, \"csv_export\": null}", "id": "lc_fb34aa7e-2e91-4a53-88ba-9320b7c6a8bb"}]
AI: [{'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/tmp/cluster_analysis/cann_api_sum/cluster_analysis_output/cluster_analysis.db', 'query': 'SELECT rank, name, totalTimeNs, totalCount, averageNs, durationRatio FROM CannApiSumRank ORDER BY rank, totalTimeNs DESC'}, 'id': 'call_00_ET_3jJ2y7bnzU8UKMUgUFR43428', 'type': 'tool_call'}, {'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/tmp/cluster_analysis/compute_op_sum/cluster_analysis_output/cluster_analysis.db', 'query': 'SELECT Rank, OpType, TaskType, Count, MeanNs, SumNs FROM ComputeOpPerRankStatsByOpType ORDER BY Rank, SumNs DESC'}, 'id': 'call_01_ET_Rl4PoSkPbW7N9lm0CUxg8957', 'type': 'tool_call'}]
Tool: Tool result too large, the result of this tool call call_00_ET_3jJ2y7bnzU8UKMUgUFR43428 was saved in the filesystem at this path: /large_tool_results/call_00_ET_3jJ2y7bnzU8UKMUgUFR43428

You can read the result from the filesystem by using the read_file tool, but make sure to only read part of the result at a time.

You can do this by specifying an offset and limit in the read_file tool call. For example, to read the first 100 lines, you can use the read_file tool with offset=0 and limit=100.

Here is a preview showing the head and tail of the result (lines of the form `... [N lines truncated] ...` indicate omitted lines in the middle of the content):

     1	{"rows": [{"rank": 0, "name": "aclrtSynchronizeEvent", "totalTimeNs": 329917840, "totalCount": 17, "averageNs": 19406931.8, "durationRatio": 25.54}, {"rank": 0, "name": "launch", "totalTimeNs": 259489830, "totalCount": 4168, "averageNs": 62257.6, "durationRatio": 20.09}, {"rank": 0, "name": "hcom_allReduce_", "totalTimeNs": 89117770, "totalCount": 258, "averageNs": 345417.7, "durationRatio": 6.9}, {"rank": 0, "name": "aclnnInnerFusedInferAttentionScore", "totalTimeNs": 83720360, "totalCount": 1088, "averageNs": 76948.9, "durationRatio": 6.48}, {"rank": 0, "name": "hcom_allGather_", "totalTimeNs": 79986800, "totalCount": 17, "averageNs": 4705105.9, "durationRatio": 6.19}, {"rank": 0, "name": "aclrtRecordEvent", "totalTimeNs": 39733890, "totalCount": 1819, "averageNs": 21843.8, "durationRatio": 3.08}, {"rank": 0, "name": "aclnnInplaceCopyGetWorkspaceSize", "totalTimeNs": 30141850, "totalCount": 1034, "averageNs": 29150.7, "durationRatio": 2.33}, {"rank": 0, "name": "aclnnInplaceCopy", "t

Tool: [{"type": "text", "text": "{\"rows\": [{\"Rank\": 0, \"OpType\": \"MatMulV2_ND_ND_FP16_FP16_false_true_all_98513\", \"TaskType\": \"AI_CORE\", \"Count\": 1920, \"MeanNs\": 70518.0625, \"SumNs\": 135394680.0}, {\"Rank\": 0, \"OpType\": \"MatMulV2_ND_ND_FP16_FP16_false_true_all_98499\", \"TaskType\": \"AI_CORE\", \"Count\": 1920, \"MeanNs\": 40489.354166666664, \"SumNs\": 77739560.0}, {\"Rank\": 0, \"OpType\": \"MatMulV2\", \"TaskType\": \"AI_CORE\", \"Count\": 529, \"MeanNs\": 63891.41776937618, \"SumNs\": 33798560.0}, {\"Rank\": 0, \"OpType\": \"FusedInferAttentionScore_3b093497fc536d61a77a7a3293a524da_5000000000010200203\", \"TaskType\": \"MIX_AIC\", \"Count\": 960, \"MeanNs\": 18895.4375, \"SumNs\": 18139620.0}, {\"Rank\": 0, \"OpType\": \"RmsNorm_normal_all_bf16_high_performance_2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1920, \"MeanNs\": 8224.96875, \"SumNs\": 15791940.0}, {\"Rank\": 0, \"OpType\": \"Cumsum\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 570268.2352941176, \"SumNs\": 9694560.0}, {\"Rank\": 0, \"OpType\": \"AddRmsNorm_3_high_performance_33\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1920, \"MeanNs\": 3299.9375, \"SumNs\": 6335880.0}, {\"Rank\": 0, \"OpType\": \"SwiGlu_3_high_performance_27\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 5374.833333333333, \"SumNs\": 5159840.0}, {\"Rank\": 0, \"OpType\": \"Slice_ee29dafe4fe21e12bb8d56ae91626cba_high_performance_524000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2688, \"MeanNs\": 1719.84375, \"SumNs\": 4622940.0}, {\"Rank\": 0, \"OpType\": \"ApplyRotaryPosEmb_b629c03f5922badb3407bf2e66751e02_high_performance_1\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 4306.375, \"SumNs\": 4134120.0}, {\"Rank\": 0, \"OpType\": \"Sort\", \"TaskType\": \"MIX_AIV\", \"Count\": 17, \"MeanNs\": 232275.29411764705, \"SumNs\": 3948680.0}, {\"Rank\": 0, \"OpType\": \"reshape_and_cache_200000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 2623.5833333333335, \"SumNs\": 2518640.0}, {\"Rank\": 0, \"OpType\": \"RmsNorm\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 258, \"MeanNs\": 9541.162790697674, \"SumNs\": 2461620.0}, {\"Rank\": 0, \"OpType\": \"FusedInferAttentionScore\", \"TaskType\": \"MIX_AIC\", \"Count\": 128, \"MeanNs\": 16822.1875, \"SumNs\": 2153240.0}, {\"Rank\": 0, \"OpType\": \"SoftmaxV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 50760.58823529412, \"SumNs\": 1725860.0}, {\"Rank\": 0, \"OpType\": \"AddRmsNorm\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 256, \"MeanNs\": 4362.578125, \"SumNs\": 1116820.0}, {\"Rank\": 0, \"OpType\": \"Slice\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 480, \"MeanNs\": 2053.1666666666665, \"SumNs\": 985520.0}, {\"Rank\": 0, \"OpType\": \"SwiGlu\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 7349.84375, \"SumNs\": 940780.0}, {\"Rank\": 0, \"OpType\": \"MaskedFill\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 70, \"MeanNs\": 12018.57142857143, \"SumNs\": 841300.0}, {\"Rank\": 0, \"OpType\": \"ApplyRotaryPosEmb\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 5428.125, \"SumNs\": 694800.0}, {\"Rank\": 0, \"OpType\": \"Cast\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 193, \"MeanNs\": 3520.3108808290153, \"SumNs\": 679420.0}, {\"Rank\": 0, \"OpType\": \"ArgMaxV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 34112.94117647059, \"SumNs\": 579920.0}, {\"Rank\": 0, \"OpType\": \"Less\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 36, \"MeanNs\": 13911.111111111111, \"SumNs\": 500800.0}, {\"Rank\": 0, \"OpType\": \"DSARandomUniform\", \"TaskType\": \"DSA_SQE\", \"Count\": 17, \"MeanNs\": 28131.764705882353, \"SumNs\": 478240.0}, {\"Rank\": 0, \"OpType\": \"ReshapeAndCacheNdKernel\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 3215.9375, \"SumNs\": 411640.0}, {\"Rank\": 0, \"OpType\": \"ReduceSum\", \"TaskType\": \"MIX_AIV\", \"Count\": 17, \"MeanNs\": 23428.235294117647, \"SumNs\": 398280.0}, {\"Rank\": 0, \"OpType\": \"RealDiv\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 10783.529411764706, \"SumNs\": 366640.0}, {\"Rank\": 0, \"OpType\": \"LessEqual\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 14094.117647058823, \"SumNs\": 239600.0}, {\"Rank\": 0, \"OpType\": \"GreaterEqual\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 19, \"MeanNs\": 10025.263157894737, \"SumNs\": 190480.0}, {\"Rank\": 0, \"OpType\": \"MemSet\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 11075.29411764706, \"SumNs\": 188280.0}, {\"Rank\": 0, \"OpType\": \"MaskedFill_56fa5818fd7054b95187cc25e525baa3_high_performance_299900007\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 12754.285714285714, \"SumNs\": 178560.0}, {\"Rank\": 0, \"OpType\": \"Transpose\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 11565.333333333334, \"SumNs\": 173480.0}, {\"Rank\": 0, \"OpType\": \"Add\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 9724.70588235294, \"SumNs\": 165320.0}, {\"Rank\": 0, \"OpType\": \"Mul\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 21, \"MeanNs\": 7599.047619047619, \"SumNs\": 159580.0}, {\"Rank\": 0, \"OpType\": \"Neg\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 9244.70588235294, \"SumNs\": 157160.0}, {\"Rank\": 0, \"OpType\": \"Index\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 9025.882352941177, \"SumNs\": 153440.0}, {\"Rank\": 0, \"OpType\": \"ViewCopy\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 9738.666666666666, \"SumNs\": 146080.0}, {\"Rank\": 0, \"OpType\": \"Log\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 8215.29411764706, \"SumNs\": 139660.0}, {\"Rank\": 0, \"OpType\": \"Fill\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 81, \"MeanNs\": 1689.3827160493827, \"SumNs\": 136840.0}, {\"Rank\": 0, \"OpType\": \"GatherElementsV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 3881.176470588235, \"SumNs\": 131960.0}, {\"Rank\": 0, \"OpType\": \"Sub\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 53, \"MeanNs\": 2033.5849056603774, \"SumNs\": 107780.0}, {\"Rank\": 0, \"OpType\": \"GatherV3\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 3017.0588235294117, \"SumNs\": 102580.0}, {\"Rank\": 0, \"OpType\": \"Tile\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 2210.0, \"SumNs\": 75140.0}, {\"Rank\": 0, \"OpType\": \"RmsNorm_normal_all_bf16_high_performance_0\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 4886.666666666667, \"SumNs\": 73300.0}, {\"Rank\": 0, \"OpType\": \"Cast_e87590d11ccda8b259ab6b1ea7212319_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 45, \"MeanNs\": 1292.4444444444443, \"SumNs\": 58160.0}, {\"Rank\": 0, \"OpType\": \"Cast_bbb33f17aca2fbbf645166c6cba610fc_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 30, \"MeanNs\": 1374.6666666666667, \"SumNs\": 41240.0}, {\"Rank\": 0, \"OpType\": \"GreaterEqual_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2880.0, \"SumNs\": 40320.0}, {\"Rank\": 0, \"OpType\": \"GatherV2_7869a97190b9b4d296d9414a005b954b_high_precision_900015010\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2821.4285714285716, \"SumNs\": 39500.0}, {\"Rank\": 0, \"OpType\": \"Less_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2690.0, \"SumNs\": 37660.0}, {\"Rank\": 0, \"OpType\": \"Sub_41dadce325b0f810d03359af2a38990b_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 2404.0, \"SumNs\": 36060.0}, {\"Rank\": 0, \"OpType\": \"Mul_41dadce325b0f810d03359af2a38990b_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 16, \"MeanNs\": 2116.25, \"SumNs\": 33860.0}, {\"Rank\": 0, \"OpType\": \"Equal\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 1823.5294117647059, \"SumNs\": 31000.0}, {\"Rank\": 0, \"OpType\": \"BroadcastTo\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1948.0, \"SumNs\": 29220.0}, {\"Rank\": 0, \"OpType\": \"Mul_41dadce325b0f810d03359af2a38990b_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 1898.5714285714287, \"SumNs\": 26580.0}, {\"Rank\": 0, \"OpType\": \"LogicalAnd_91d2a3cdd75d915a368582ec5f440af1_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1561.3333333333333, \"SumNs\": 23420.0}, {\"Rank\": 0, \"OpType\": \"LogicalNot_c5fb21ce50d8471d9b8df5001b9dc9a5_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1526.6666666666667, \"SumNs\": 22900.0}, {\"Rank\": 0, \"OpType\": \"GatherV2_7869a97190b9b4d296d9414a005b954b_high_precision_900015015\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 10160.0, \"SumNs\": 10160.0}, {\"Rank\": 0, \"OpType\": \"GatherV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 3600.0, \"SumNs\": 7200.0}, {\"Rank\": 0, \"OpType\": \"MaskedFill_56fa5818fd7054b95187cc25e525baa3_high_performance_220000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3300.0, \"SumNs\": 3300.0}, {\"Rank\": 0, \"OpType\": \"LogicalAnd\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 1630.0, \"SumNs\": 3260.0}, {\"Rank\": 0, \"OpType\": \"GreaterEqual_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3180.0, \"SumNs\": 3180.0}, {\"Rank\": 0, \"OpType\": \"LogicalNot\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 1580.0, \"SumNs\": 3160.0}, {\"Rank\": 0, \"OpType\": \"Less_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3040.0, \"SumNs\": 3040.0}, {\"Rank\": 1, \"OpType\": \"MatMulV2_ND_ND_FP16_FP16_false_true_all_98513\", \"TaskType\": \"AI_CORE\", \"Count\": 1920, \"MeanNs\": 70276.07291666667, \"SumNs\": 134930060.0}, {\"Rank\": 1, \"OpType\": \"MatMulV2_ND_ND_FP16_FP16_false_true_all_98499\", \"TaskType\": \"AI_CORE\", \"Count\": 1920, \"MeanNs\": 40207.822916666664, \"SumNs\": 77199020.0}, {\"Rank\": 1, \"OpType\": \"MatMulV2\", \"TaskType\": \"AI_CORE\", \"Count\": 529, \"MeanNs\": 63663.10018903592, \"SumNs\": 33677780.0}, {\"Rank\": 1, \"OpType\": \"FusedInferAttentionScore_3b093497fc536d61a77a7a3293a524da_5000000000010200203\", \"TaskType\": \"MIX_AIC\", \"Count\": 960, \"MeanNs\": 19333.125, \"SumNs\": 18559800.0}, {\"Rank\": 1, \"OpType\": \"RmsNorm_normal_all_bf16_high_performance_2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1920, \"MeanNs\": 8101.666666666667, \"SumNs\": 15555200.0}, {\"Rank\": 1, \"OpType\": \"Cumsum\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 573257.6470588235, \"SumNs\": 9745380.0}, {\"Rank\": 1, \"OpType\": \"AddRmsNorm_3_high_performance_33\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1920, \"MeanNs\": 3289.3229166666665, \"SumNs\": 6315500.0}, {\"Rank\": 1, \"OpType\": \"SwiGlu_3_high_performance_27\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 5166.375, \"SumNs\": 4959720.0}, {\"Rank\": 1, \"OpType\": \"Slice_ee29dafe4fe21e12bb8d56ae91626cba_high_performance_524000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2688, \"MeanNs\": 1690.6026785714287, \"SumNs\": 4544340.0}, {\"Rank\": 1, \"OpType\": \"ApplyRotaryPosEmb_b629c03f5922badb3407bf2e66751e02_high_performance_1\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 4220.104166666667, \"SumNs\": 4051300.0}, {\"Rank\": 1, \"OpType\": \"Sort\", \"TaskType\": \"MIX_AIV\", \"Count\": 17, \"MeanNs\": 233896.4705882353, \"SumNs\": 3976240.0}, {\"Rank\": 1, \"OpType\": \"reshape_and_cache_200000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 2611.0833333333335, \"SumNs\": 2506640.0}, {\"Rank\": 1, \"OpType\": \"RmsNorm\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 258, \"MeanNs\": 9592.170542635658, \"SumNs\": 2474780.0}, {\"Rank\": 1, \"OpType\": \"FusedInferAttentionScore\", \"TaskType\": \"MIX_AIC\", \"Count\": 128, \"MeanNs\": 17470.625, \"SumNs\": 2236240.0}, {\"Rank\": 1, \"OpType\": \"SoftmaxV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 50502.35294117647, \"SumNs\": 1717080.0}, {\"Rank\": 1, \"OpType\": \"AddRmsNorm\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 256, \"MeanNs\": 4310.46875, \"SumNs\": 1103480.0}, {\"Rank\": 1, \"OpType\": \"Slice\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 480, \"MeanNs\": 2044.4583333333333, \"SumNs\": 981340.0}, {\"Rank\": 1, \"OpType\": \"SwiGlu\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 6852.65625, \"SumNs\": 877140.0}, {\"Rank\": 1, \"OpType\": \"MaskedFill\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 70, \"MeanNs\": 11630.285714285714, \"SumNs\": 814120.0}, {\"Rank\": 1, \"OpType\": \"ApplyRotaryPosEmb\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 5233.75, \"SumNs\": 669920.0}, {\"Rank\": 1, \"OpType\": \"Cast\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 193, \"MeanNs\": 3183.937823834197, \"SumNs\": 614500.0}, {\"Rank\": 1, \"OpType\": \"ArgMaxV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 34130.58823529412, \"SumNs\": 580220.0}, {\"Rank\": 1, \"OpType\": \"Less\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 36, \"MeanNs\": 14461.666666666666, \"SumNs\": 520620.0}, {\"Rank\": 1, \"OpType\": \"DSARandomUniform\", \"TaskType\": \"DSA_SQE\", \"Count\": 17, \"MeanNs\": 27922.352941176472, \"SumNs\": 474680.0}, {\"Rank\": 1, \"OpType\": \"ReshapeAndCacheNdKernel\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 3223.28125, \"SumNs\": 412580.0}, {\"Rank\": 1, \"OpType\": \"ReduceSum\", \"TaskType\": \"MIX_AIV\", \"Count\": 17, \"MeanNs\": 23737.647058823528, \"SumNs\": 403540.0}, {\"Rank\": 1, \"OpType\": \"RealDiv\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 11101.764705882353, \"SumNs\": 377460.0}, {\"Rank\": 1, \"OpType\": \"LessEqual\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 14681.176470588236, \"SumNs\": 249580.0}, {\"Rank\": 1, \"OpType\": \"Transpose\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 12442.666666666666, \"SumNs\": 186640.0}, {\"Rank\": 1, \"OpType\": \"MemSet\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 10776.470588235294, \"SumNs\": 183200.0}, {\"Rank\": 1, \"OpType\": \"MaskedFill_56fa5818fd7054b95187cc25e525baa3_high_performance_299900007\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 12522.857142857143, \"SumNs\": 175320.0}, {\"Rank\": 1, \"OpType\": \"GreaterEqual\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 19, \"MeanNs\": 9108.421052631578, \"SumNs\": 173060.0}, {\"Rank\": 1, \"OpType\": \"Log\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 9707.058823529413, \"SumNs\": 165020.0}, {\"Rank\": 1, \"OpType\": \"Neg\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 9071.764705882353, \"SumNs\": 154220.0}, {\"Rank\": 1, \"OpType\": \"Index\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 8997.64705882353, \"SumNs\": 152960.0}, {\"Rank\": 1, \"OpType\": \"Mul\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 21, \"MeanNs\": 7174.285714285715, \"SumNs\": 150660.0}, {\"Rank\": 1, \"OpType\": \"Fill\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 81, \"MeanNs\": 1784.9382716049383, \"SumNs\": 144580.0}, {\"Rank\": 1, \"OpType\": \"ViewCopy\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 9473.333333333334, \"SumNs\": 142100.0}, {\"Rank\": 1, \"OpType\": \"GatherElementsV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 4012.3529411764707, \"SumNs\": 136420.0}, {\"Rank\": 1, \"OpType\": \"Add\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 8001.176470588235, \"SumNs\": 136020.0}, {\"Rank\": 1, \"OpType\": \"Sub\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 53, \"MeanNs\": 2073.9622641509436, \"SumNs\": 109920.0}, {\"Rank\": 1, \"OpType\": \"GatherV3\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 2942.3529411764707, \"SumNs\": 100040.0}, {\"Rank\": 1, \"OpType\": \"Tile\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 2170.0, \"SumNs\": 73780.0}, {\"Rank\": 1, \"OpType\": \"RmsNorm_normal_all_bf16_high_performance_0\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 4852.0, \"SumNs\": 72780.0}, {\"Rank\": 1, \"OpType\": \"Cast_e87590d11ccda8b259ab6b1ea7212319_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 45, \"MeanNs\": 1276.0, \"SumNs\": 57420.0}, {\"Rank\": 1, \"OpType\": \"Cast_bbb33f17aca2fbbf645166c6cba610fc_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 30, \"MeanNs\": 1394.6666666666667, \"SumNs\": 41840.0}, {\"Rank\": 1, \"OpType\": \"GreaterEqual_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2910.0, \"SumNs\": 40740.0}, {\"Rank\": 1, \"OpType\": \"Less_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2835.714285714286, \"SumNs\": 39700.0}, {\"Rank\": 1, \"OpType\": \"GatherV2_7869a97190b9b4d296d9414a005b954b_high_precision_900015010\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2811.4285714285716, \"SumNs\": 39360.0}, {\"Rank\": 1, \"OpType\": \"Sub_41dadce325b0f810d03359af2a38990b_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 2369.3333333333335, \"SumNs\": 35540.0}, {\"Rank\": 1, \"OpType\": \"Mul_41dadce325b0f810d03359af2a38990b_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 16, \"MeanNs\": 2070.0, \"SumNs\": 33120.0}, {\"Rank\": 1, \"OpType\": \"Equal\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 1823.5294117647059, \"SumNs\": 31000.0}, {\"Rank\": 1, \"OpType\": \"Mul_41dadce325b0f810d03359af2a38990b_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 1922.857142857143, \"SumNs\": 26920.0}, {\"Rank\": 1, \"OpType\": \"BroadcastTo\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1689.3333333333333, \"SumNs\": 25340.0}, {\"Rank\": 1, \"OpType\": \"LogicalAnd_91d2a3cdd75d915a368582ec5f440af1_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1636.0, \"SumNs\": 24540.0}, {\"Rank\": 1, \"OpType\": \"LogicalNot_c5fb21ce50d8471d9b8df5001b9dc9a5_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1528.0, \"SumNs\": 22920.0}, {\"Rank\": 1, \"OpType\": \"GatherV2_7869a97190b9b4d296d9414a005b954b_high_precision_900015015\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 10420.0, \"SumNs\": 10420.0}, {\"Rank\": 1, \"OpType\": \"GatherV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 3480.0, \"SumNs\": 6960.0}, {\"Rank\": 1, \"OpType\": \"MaskedFill_56fa5818fd7054b95187cc25e525baa3_high_performance_220000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3320.0, \"SumNs\": 3320.0}, {\"Rank\": 1, \"OpType\": \"GreaterEqual_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3220.0, \"SumNs\": 3220.0}, {\"Rank\": 1, \"OpType\": \"Less_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3180.0, \"SumNs\": 3180.0}, {\"Rank\": 1, \"OpType\": \"LogicalAnd\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 1550.0, \"SumNs\": 3100.0}, {\"Rank\": 1, \"OpType\": \"LogicalNot\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 1370.0, \"SumNs\": 2740.0}, {\"Rank\": 2, \"OpType\": \"MatMulV2_ND_ND_FP16_FP16_false_true_all_98513\", \"TaskType\": \"AI_CORE\", \"Count\": 1920, \"MeanNs\": 70615.36458333333, \"SumNs\": 135581500.0}, {\"Rank\": 2, \"OpType\": \"MatMulV2_ND_ND_FP16_FP16_false_true_all_98499\", \"TaskType\": \"AI_CORE\", \"Count\": 1920, \"MeanNs\": 40421.4328125, \"SumNs\": 77609151.0}, {\"Rank\": 2, \"OpType\": \"MatMulV2\", \"TaskType\": \"AI_CORE\", \"Count\": 529, \"MeanNs\": 63736.64461247637, \"SumNs\": 33716685.0}, {\"Rank\": 2, \"OpType\": \"FusedInferAttentionScore_3b093497fc536d61a77a7a3293a524da_5000000000010200203\", \"TaskType\": \"MIX_AIC\", \"Count\": 960, \"MeanNs\": 20732.338541666668, \"SumNs\": 19903045.0}, {\"Rank\": 2, \"OpType\": \"RmsNorm_normal_all_bf16_high_performance_2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1920, \"MeanNs\": 7976.0671875, \"SumNs\": 15314049.0}, {\"Rank\": 2, \"OpType\": \"Cumsum\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 570222.1176470588, \"SumNs\": 9693776.0}, {\"Rank\": 2, \"OpType\": \"AddRmsNorm_3_high_performance_33\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1920, \"MeanNs\": 3287.289583333333, \"SumNs\": 6311596.0}, {\"Rank\": 2, \"OpType\": \"SwiGlu_3_high_performance_27\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 5531.4625, \"SumNs\": 5310204.0}, {\"Rank\": 2, \"OpType\": \"Slice_ee29dafe4fe21e12bb8d56ae91626cba_high_performance_524000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2688, \"MeanNs\": 1672.9412202380952, \"SumNs\": 4496866.0}, {\"Rank\": 2, \"OpType\": \"ApplyRotaryPosEmb_b629c03f5922badb3407bf2e66751e02_high_performance_1\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 4399.761458333333, \"SumNs\": 4223771.0}, {\"Rank\": 2, \"OpType\": \"Sort\", \"TaskType\": \"MIX_AIV\", \"Count\": 17, \"MeanNs\": 235175.41176470587, \"SumNs\": 3997982.0}, {\"Rank\": 2, \"OpType\": \"reshape_and_cache_200000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 2598.514583333333, \"SumNs\": 2494574.0}, {\"Rank\": 2, \"OpType\": \"RmsNorm\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 258, \"MeanNs\": 9578.798449612403, \"SumNs\": 2471330.0}, {\"Rank\": 2, \"OpType\": \"FusedInferAttentionScore\", \"TaskType\": \"MIX_AIC\", \"Count\": 128, \"MeanNs\": 18048.359375, \"SumNs\": 2310190.0}, {\"Rank\": 2, \"OpType\": \"SoftmaxV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 50686.294117647056, \"SumNs\": 1723334.0}, {\"Rank\": 2, \"OpType\": \"AddRmsNorm\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 256, \"MeanNs\": 4268.609375, \"SumNs\": 1092764.0}, {\"Rank\": 2, \"OpType\": \"SwiGlu\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 7436.6875, \"SumNs\": 951896.0}, {\"Rank\": 2, \"OpType\": \"Slice\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 480, \"MeanNs\": 1959.2479166666667, \"SumNs\": 940439.0}, {\"Rank\": 2, \"OpType\": \"MaskedFill\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 70, \"MeanNs\": 12474.528571428571, \"SumNs\": 873217.0}, {\"Rank\": 2, \"OpType\": \"ApplyRotaryPosEmb\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 5605.25, \"SumNs\": 717472.0}, {\"Rank\": 2, \"OpType\": \"Cast\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 193, \"MeanNs\": 3302.0259067357515, \"SumNs\": 637291.0}, {\"Rank\": 2, \"OpType\": \"ArgMaxV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 34186.58823529412, \"SumNs\": 581172.0}, {\"Rank\": 2, \"OpType\": \"Less\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 36, \"MeanNs\": 14003.25, \"SumNs\": 504117.0}, {\"Rank\": 2, \"OpType\": \"DSARandomUniform\", \"TaskType\": \"DSA_SQE\", \"Count\": 17, \"MeanNs\": 28264.0, \"SumNs\": 480488.0}, {\"Rank\": 2, \"OpType\": \"ReshapeAndCacheNdKernel\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 3245.6875, \"SumNs\": 415448.0}, {\"Rank\": 2, \"OpType\": \"ReduceSum\", \"TaskType\": \"MIX_AIV\", \"Count\": 17, \"MeanNs\": 24289.882352941175, \"SumNs\": 412928.0}, {\"Rank\": 2, \"OpType\": \"RealDiv\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 11959.558823529413, \"SumNs\": 406625.0}, {\"Rank\": 2, \"OpType\": \"LessEqual\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 14628.529411764706, \"SumNs\": 248685.0}, {\"Rank\": 2, \"OpType\": \"Transpose\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 14256.533333333333, \"SumNs\": 213848.0}, {\"Rank\": 2, \"OpType\": \"GreaterEqual\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 19, \"MeanNs\": 10581.421052631578, \"SumNs\": 201047.0}, {\"Rank\": 2, \"OpType\": \"MaskedFill_56fa5818fd7054b95187cc25e525baa3_high_performance_299900007\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 13803.142857142857, \"SumNs\": 193244.0}, {\"Rank\": 2, \"OpType\": \"Index\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 9932.058823529413, \"SumNs\": 168845.0}, {\"Rank\": 2, \"OpType\": \"MemSet\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 9833.0, \"SumNs\": 167161.0}, {\"Rank\": 2, \"OpType\": \"Mul\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 21, \"MeanNs\": 7657.238095238095, \"SumNs\": 160802.0}, {\"Rank\": 2, \"OpType\": \"ViewCopy\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 10268.133333333333, \"SumNs\": 154022.0}, {\"Rank\": 2, \"OpType\": \"Log\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 8985.941176470587, \"SumNs\": 152761.0}, {\"Rank\": 2, \"OpType\": \"Add\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 8759.117647058823, \"SumNs\": 148905.0}, {\"Rank\": 2, \"OpType\": \"Neg\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 8390.70588235294, \"SumNs\": 142642.0}, {\"Rank\": 2, \"OpType\": \"Fill\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 81, \"MeanNs\": 1726.7037037037037, \"SumNs\": 139863.0}, {\"Rank\": 2, \"OpType\": \"GatherElementsV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 3945.9117647058824, \"SumNs\": 134161.0}, {\"Rank\": 2, \"OpType\": \"Sub\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 53, \"MeanNs\": 2078.5471698113206, \"SumNs\": 110163.0}, {\"Rank\": 2, \"OpType\": \"GatherV3\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 3040.0882352941176, \"SumNs\": 103363.0}, {\"Rank\": 2, \"OpType\": \"Tile\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 2211.205882352941, \"SumNs\": 75181.0}, {\"Rank\": 2, \"OpType\": \"RmsNorm_normal_all_bf16_high_performance_0\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 4948.0, \"SumNs\": 74220.0}, {\"Rank\": 2, \"OpType\": \"Cast_e87590d11ccda8b259ab6b1ea7212319_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 45, \"MeanNs\": 1282.2444444444445, \"SumNs\": 57701.0}, {\"Rank\": 2, \"OpType\": \"Cast_bbb33f17aca2fbbf645166c6cba610fc_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 30, \"MeanNs\": 1363.3666666666666, \"SumNs\": 40901.0}, {\"Rank\": 2, \"OpType\": \"GreaterEqual_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2870.0714285714284, \"SumNs\": 40181.0}, {\"Rank\": 2, \"OpType\": \"Less_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2778.785714285714, \"SumNs\": 38903.0}, {\"Rank\": 2, \"OpType\": \"GatherV2_7869a97190b9b4d296d9414a005b954b_high_precision_900015010\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2680.0714285714284, \"SumNs\": 37521.0}, {\"Rank\": 2, \"OpType\": \"Sub_41dadce325b0f810d03359af2a38990b_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 2369.4666666666667, \"SumNs\": 35542.0}, {\"Rank\": 2, \"OpType\": \"Mul_41dadce325b0f810d03359af2a38990b_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 16, \"MeanNs\": 2112.5, \"SumNs\": 33800.0}, {\"Rank\": 2, \"OpType\": \"Equal\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 1820.0588235294117, \"SumNs\": 30941.0}, {\"Rank\": 2, \"OpType\": \"BroadcastTo\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1988.0, \"SumNs\": 29820.0}, {\"Rank\": 2, \"OpType\": \"Mul_41dadce325b0f810d03359af2a38990b_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 1845.7142857142858, \"SumNs\": 25840.0}, {\"Rank\": 2, \"OpType\": \"LogicalAnd_91d2a3cdd75d915a368582ec5f440af1_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1582.6666666666667, \"SumNs\": 23740.0}, {\"Rank\": 2, \"OpType\": \"LogicalNot_c5fb21ce50d8471d9b8df5001b9dc9a5_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1510.6666666666667, \"SumNs\": 22660.0}, {\"Rank\": 2, \"OpType\": \"GatherV2_7869a97190b9b4d296d9414a005b954b_high_precision_900015015\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 11461.0, \"SumNs\": 11461.0}, {\"Rank\": 2, \"OpType\": \"GatherV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 3560.5, \"SumNs\": 7121.0}, {\"Rank\": 2, \"OpType\": \"MaskedFill_56fa5818fd7054b95187cc25e525baa3_high_performance_220000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3500.0, \"SumNs\": 3500.0}, {\"Rank\": 2, \"OpType\": \"GreaterEqual_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3280.0, \"SumNs\": 3280.0}, {\"Rank\": 2, \"OpType\": \"LogicalAnd\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 1620.0, \"SumNs\": 3240.0}, {\"Rank\": 2, \"OpType\": \"Less_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3060.0, \"SumNs\": 3060.0}, {\"Rank\": 2, \"OpType\": \"LogicalNot\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 1450.0, \"SumNs\": 2900.0}, {\"Rank\": 3, \"OpType\": \"MatMulV2_ND_ND_FP16_FP16_false_true_all_98513\", \"TaskType\": \"AI_CORE\", \"Count\": 1920, \"MeanNs\": 70476.39739583334, \"SumNs\": 135314683.0}, {\"Rank\": 3, \"OpType\": \"MatMulV2_ND_ND_FP16_FP16_false_true_all_98499\", \"TaskType\": \"AI_CORE\", \"Count\": 1920, \"MeanNs\": 40180.08697916667, \"SumNs\": 77145767.0}, {\"Rank\": 3, \"OpType\": \"MatMulV2\", \"TaskType\": \"AI_CORE\", \"Count\": 529, \"MeanNs\": 63681.298676748585, \"SumNs\": 33687407.0}, {\"Rank\": 3, \"OpType\": \"FusedInferAttentionScore_3b093497fc536d61a77a7a3293a524da_5000000000010200203\", \"TaskType\": \"MIX_AIC\", \"Count\": 960, \"MeanNs\": 20349.990625, \"SumNs\": 19535991.0}, {\"Rank\": 3, \"OpType\": \"RmsNorm_normal_all_bf16_high_performance_2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1920, \"MeanNs\": 8293.6953125, \"SumNs\": 15923895.0}, {\"Rank\": 3, \"OpType\": \"Cumsum\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 570759.4117647059, \"SumNs\": 9702910.0}, {\"Rank\": 3, \"OpType\": \"AddRmsNorm_3_high_performance_33\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1920, \"MeanNs\": 3284.4734375, \"SumNs\": 6306189.0}, {\"Rank\": 3, \"OpType\": \"SwiGlu_3_high_performance_27\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 5123.245833333333, \"SumNs\": 4918316.0}, {\"Rank\": 3, \"OpType\": \"Slice_ee29dafe4fe21e12bb8d56ae91626cba_high_performance_524000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2688, \"MeanNs\": 1719.4348958333333, \"SumNs\": 4621841.0}, {\"Rank\": 3, \"OpType\": \"ApplyRotaryPosEmb_b629c03f5922badb3407bf2e66751e02_high_performance_1\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 4212.341666666666, \"SumNs\": 4043848.0}, {\"Rank\": 3, \"OpType\": \"Sort\", \"TaskType\": \"MIX_AIV\", \"Count\": 17, \"MeanNs\": 232225.64705882352, \"SumNs\": 3947836.0}, {\"Rank\": 3, \"OpType\": \"RmsNorm\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 258, \"MeanNs\": 9799.565891472868, \"SumNs\": 2528288.0}, {\"Rank\": 3, \"OpType\": \"reshape_and_cache_200000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 960, \"MeanNs\": 2581.371875, \"SumNs\": 2478117.0}, {\"Rank\": 3, \"OpType\": \"FusedInferAttentionScore\", \"TaskType\": \"MIX_AIC\", \"Count\": 128, \"MeanNs\": 18354.140625, \"SumNs\": 2349330.0}, {\"Rank\": 3, \"OpType\": \"SoftmaxV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 50365.64705882353, \"SumNs\": 1712432.0}, {\"Rank\": 3, \"OpType\": \"AddRmsNorm\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 256, \"MeanNs\": 4338.46875, \"SumNs\": 1110648.0}, {\"Rank\": 3, \"OpType\": \"Slice\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 480, \"MeanNs\": 2061.7145833333334, \"SumNs\": 989623.0}, {\"Rank\": 3, \"OpType\": \"SwiGlu\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 6881.796875, \"SumNs\": 880870.0}, {\"Rank\": 3, \"OpType\": \"MaskedFill\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 70, \"MeanNs\": 11793.214285714286, \"SumNs\": 825525.0}, {\"Rank\": 3, \"OpType\": \"ApplyRotaryPosEmb\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 5214.3046875, \"SumNs\": 667431.0}, {\"Rank\": 3, \"OpType\": \"Cast\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 193, \"MeanNs\": 3288.6528497409327, \"SumNs\": 634710.0}, {\"Rank\": 3, \"OpType\": \"ArgMaxV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 34079.82352941176, \"SumNs\": 579357.0}, {\"Rank\": 3, \"OpType\": \"Less\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 36, \"MeanNs\": 14297.388888888889, \"SumNs\": 514706.0}, {\"Rank\": 3, \"OpType\": \"DSARandomUniform\", \"TaskType\": \"DSA_SQE\", \"Count\": 17, \"MeanNs\": 28366.29411764706, \"SumNs\": 482227.0}, {\"Rank\": 3, \"OpType\": \"ReshapeAndCacheNdKernel\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 128, \"MeanNs\": 3244.859375, \"SumNs\": 415342.0}, {\"Rank\": 3, \"OpType\": \"ReduceSum\", \"TaskType\": \"MIX_AIV\", \"Count\": 17, \"MeanNs\": 23410.529411764706, \"SumNs\": 397979.0}, {\"Rank\": 3, \"OpType\": \"RealDiv\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 11677.70588235294, \"SumNs\": 397042.0}, {\"Rank\": 3, \"OpType\": \"LessEqual\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 14580.235294117647, \"SumNs\": 247864.0}, {\"Rank\": 3, \"OpType\": \"Transpose\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 12906.466666666667, \"SumNs\": 193597.0}, {\"Rank\": 3, \"OpType\": \"GreaterEqual\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 19, \"MeanNs\": 9867.21052631579, \"SumNs\": 187477.0}, {\"Rank\": 3, \"OpType\": \"Mul\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 21, \"MeanNs\": 8439.809523809523, \"SumNs\": 177236.0}, {\"Rank\": 3, \"OpType\": \"MaskedFill_56fa5818fd7054b95187cc25e525baa3_high_performance_299900007\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 12435.785714285714, \"SumNs\": 174101.0}, {\"Rank\": 3, \"OpType\": \"MemSet\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 10186.882352941177, \"SumNs\": 173177.0}, {\"Rank\": 3, \"OpType\": \"Neg\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 9530.411764705883, \"SumNs\": 162017.0}, {\"Rank\": 3, \"OpType\": \"Index\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 8986.764705882353, \"SumNs\": 152775.0}, {\"Rank\": 3, \"OpType\": \"Add\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 8546.764705882353, \"SumNs\": 145295.0}, {\"Rank\": 3, \"OpType\": \"ViewCopy\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 9530.333333333334, \"SumNs\": 142955.0}, {\"Rank\": 3, \"OpType\": \"Log\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 8146.764705882353, \"SumNs\": 138495.0}, {\"Rank\": 3, \"OpType\": \"GatherElementsV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 3993.3823529411766, \"SumNs\": 135775.0}, {\"Rank\": 3, \"OpType\": \"Fill\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 81, \"MeanNs\": 1673.4567901234568, \"SumNs\": 135550.0}, {\"Rank\": 3, \"OpType\": \"Sub\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 53, \"MeanNs\": 2041.6415094339623, \"SumNs\": 108207.0}, {\"Rank\": 3, \"OpType\": \"GatherV3\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 2980.323529411765, \"SumNs\": 101331.0}, {\"Rank\": 3, \"OpType\": \"Tile\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 34, \"MeanNs\": 2204.235294117647, \"SumNs\": 74944.0}, {\"Rank\": 3, \"OpType\": \"RmsNorm_normal_all_bf16_high_performance_0\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 4688.2, \"SumNs\": 70323.0}, {\"Rank\": 3, \"OpType\": \"Cast_e87590d11ccda8b259ab6b1ea7212319_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 45, \"MeanNs\": 1244.1333333333334, \"SumNs\": 55986.0}, {\"Rank\": 3, \"OpType\": \"GreaterEqual_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2841.6428571428573, \"SumNs\": 39783.0}, {\"Rank\": 3, \"OpType\": \"Cast_bbb33f17aca2fbbf645166c6cba610fc_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 30, \"MeanNs\": 1322.1333333333334, \"SumNs\": 39664.0}, {\"Rank\": 3, \"OpType\": \"Less_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2726.0, \"SumNs\": 38164.0}, {\"Rank\": 3, \"OpType\": \"GatherV2_7869a97190b9b4d296d9414a005b954b_high_precision_900015010\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 2720.0714285714284, \"SumNs\": 38081.0}, {\"Rank\": 3, \"OpType\": \"Sub_41dadce325b0f810d03359af2a38990b_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 2307.0, \"SumNs\": 34605.0}, {\"Rank\": 3, \"OpType\": \"Mul_41dadce325b0f810d03359af2a38990b_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 16, \"MeanNs\": 1985.25, \"SumNs\": 31764.0}, {\"Rank\": 3, \"OpType\": \"Equal\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 17, \"MeanNs\": 1809.5882352941176, \"SumNs\": 30763.0}, {\"Rank\": 3, \"OpType\": \"BroadcastTo\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1900.3333333333333, \"SumNs\": 28505.0}, {\"Rank\": 3, \"OpType\": \"Mul_41dadce325b0f810d03359af2a38990b_high_performance_223000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 14, \"MeanNs\": 1907.2142857142858, \"SumNs\": 26701.0}, {\"Rank\": 3, \"OpType\": \"LogicalAnd_91d2a3cdd75d915a368582ec5f440af1_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1533.4666666666667, \"SumNs\": 23002.0}, {\"Rank\": 3, \"OpType\": \"LogicalNot_c5fb21ce50d8471d9b8df5001b9dc9a5_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 15, \"MeanNs\": 1469.4666666666667, \"SumNs\": 22042.0}, {\"Rank\": 3, \"OpType\": \"GatherV2_7869a97190b9b4d296d9414a005b954b_high_precision_900015015\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 10422.0, \"SumNs\": 10422.0}, {\"Rank\": 3, \"OpType\": \"GatherV2\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 3370.0, \"SumNs\": 6740.0}, {\"Rank\": 3, \"OpType\": \"LogicalAnd\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 1630.0, \"SumNs\": 3260.0}, {\"Rank\": 3, \"OpType\": \"MaskedFill_56fa5818fd7054b95187cc25e525baa3_high_performance_220000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 3200.0, \"SumNs\": 3200.0}, {\"Rank\": 3, \"OpType\": \"LogicalNot\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 2, \"MeanNs\": 1470.0, \"SumNs\": 2940.0}, {\"Rank\": 3, \"OpType\": \"GreaterEqual_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 2920.0, \"SumNs\": 2920.0}, {\"Rank\": 3, \"OpType\": \"Less_1ea4da630f56dc1c2d6e7c9a9dd17b11_high_performance_210000000\", \"TaskType\": \"AI_VECTOR_CORE\", \"Count\": 1, \"MeanNs\": 2880.0, \"SumNs\": 2880.0}], \"row_count\": 252, \"truncated\": false, \"columns\": [\"Rank\", \"OpType\", \"TaskType\", \"Count\", \"MeanNs\", \"SumNs\"], \"summary\": null, \"csv_export\": null}", "id": "lc_c562f777-dbe9-4651-8e5c-37c19e023f7d"}]
AI: 计算侧各 Rank 完全均衡（MatMul 等算子 Count/Mean 几乎一致），排除计算型慢卡。现在读 CANN API 对比结果，重点看 launch/同步 API。[{'name': 'read_file', 'args': {'file_path': '/large_tool_results/call_00_ET_3jJ2y7bnzU8UKMUgUFR43428', 'limit': 120}, 'id': 'call_00_XeZ5jIMuLq3GskH1lJZc0458', 'type': 'tool_call'}]
Tool:      1	{"rows": [{"rank": 0, "name": "aclrtSynchronizeEvent", "totalTimeNs": 329917840, "totalCount": 17, "averageNs": 19406931.8, "durationRatio": 25.54}, {"rank": 0, "name": "launch", "totalTimeNs": 259489830, "totalCount": 4168, "averageNs": 62257.6, "durationRatio": 20.09}, {"rank": 0, "name": "hcom_allReduce_", "totalTimeNs": 89117770, "totalCount": 258, "averageNs": 345417.7, "durationRatio": 6.9}, {"rank": 0, "name": "aclnnInnerFusedInferAttentionScore", "totalTimeNs": 83720360, "totalCount": 1088, "averageNs": 76948.9, "durationRatio": 6.48}, {"rank": 0, "name": "hcom_allGather_", "totalTimeNs": 79986800, "totalCount": 17, "averageNs": 4705105.9, "durationRatio": 6.19}, {"rank": 0, "name": "aclrtRecordEvent", "totalTimeNs": 39733890, "totalCount": 1819, "averageNs": 21843.8, "durationRatio": 3.08}, {"rank": 0, "name": "aclnnInplaceCopyGetWorkspaceSize", "totalTimeNs": 30141850, "totalCount": 1034, "averageNs": 29150.7, "durationRatio": 2.33}, {"rank": 0, "name": "aclnnInplaceCopy", "totalTimeNs": 29833930, "totalCount": 1220, "averageNs": 24454.0, "durationRatio": 2.31}, {"rank": 0, "name": "aclnnInnerFusedInferAttentionScoreGetWorkspaceSize", "totalTimeNs": 28843100, "totalCount": 1088, "averageNs": 26510.2, "durationRatio": 2.23}, {"rank": 0, "name": "aclnnRmsNorm", "totalTimeNs": 22373660, "totalCount": 258, "averageNs": 86719.6, "durationRatio": 1.73}, {"rank": 0, "name": "aclnnContiguous", "totalTimeNs": 22305160, "totalCount": 320, "averageNs": 69703.6, "durationRatio": 1.73}, {"rank": 0, "name": "aclnnReduceSumGetWorkspaceSize", "totalTimeNs": 21506910, "totalCount": 2, "averageNs": 10753455.0, "durationRatio": 1.66}, {"rank": 0, "name": "aclnnRmsNormGetWorkspaceSize", "totalTimeNs": 16059050, "totalCount": 258, "averageNs": 62244.4, "durationRatio": 1.24}, {"rank": 0, "name": "aclnnIndexGetWorkspaceSize", "totalTimeNs": 15601630, "totalCount": 4, "averageNs": 3900407.5, "durationRatio": 1.21}, {"rank": 0, "name": "aclnnContiguousGetWorkspaceSize", "totalTimeNs": 14052140, "totalCount": 320, "averageNs": 43912.9, "durationRatio": 1.09}, {"rank": 0, "name": "aclnnReduceSum", "totalTimeNs": 11967230, "totalCount": 17, "averageNs": 703954.7, "durationRatio": 0.93}, {"rank": 0, "name": "aclrtSynchronizeDeviceWithTimeout", "totalTimeNs": 11924020, "totalCount": 16, "averageNs": 745251.3, "durationRatio": 0.92}, {"rank": 0, "name": "aclnnInplaceFillScalar", "totalTimeNs": 11618380, "totalCount": 81, "averageNs": 143436.8, "durationRatio": 0.9}, {"rank": 0, "name": "Launch_Ffts", "totalTimeNs": 11187390, "totalCount": 275, "averageNs": 40681.4, "durationRatio": 0.87}, {"rank": 0, "name": "aclnnMm", "totalTimeNs": 10096110, "totalCount": 512, "averageNs": 19719.0, "durationRatio": 0.78}, {"rank": 0, "name": "ReshapeAndCacheOperation::Execute", "totalTimeNs": 8790810, "totalCount": 128, "averageNs": 68678.2, "durationRatio": 0.68}, {"rank": 0, "name": "aclnnAddRmsNorm", "totalTimeNs": 7047480, "totalCount": 256, "averageNs": 27529.2, "durationRatio": 0.55}, {"rank": 0, "name": "Slice_Tiling", "totalTimeNs": 6194050, "totalCount": 322, "averageNs": 19236.2, "durationRatio": 0.48}, {"rank": 0, "name": "ReshapeAndCacheOperation::Setup", "totalTimeNs": 5821070, "totalCount": 128, "averageNs": 45477.1, "durationRatio": 0.45}, {"rank": 0, "name": "aclrtStreamWaitEvent", "totalTimeNs": 5509690, "totalCount": 567, "averageNs": 9717.3, "durationRatio": 0.43}, {"rank": 0, "name": "aclnnInplaceFillScalarGetWorkspaceSize", "totalTimeNs": 4347880, "totalCount": 22, "averageNs": 197630.9, "durationRatio": 0.34}, {"rank": 0, "name": "aclnnCumsumGetWorkspaceSize", "totalTimeNs": 4215210, "totalCount": 2, "averageNs": 2107605.0, "durationRatio": 0.33}, {"rank": 0, "name": "aclrtStreamGetId", "totalTimeNs": 4090390, "totalCount": 4760, "averageNs": 859.3, "durationRatio": 0.32}, {"rank": 0, "name": "aclnnCumsum", "totalTimeNs": 3707890, "totalCount": 17, "averageNs": 218111.2, "durationRatio": 0.29}, {"rank": 0, "name": "aclnnInplaceMaskedFillScalar", "totalTimeNs": 3667590, "totalCount": 70, "averageNs": 52394.1, "durationRatio": 0.28}, {"rank": 0, "name": "aclrtDestroyEvent", "totalTimeNs": 3640440, "totalCount": 556, "averageNs": 6547.6, "durationRatio": 0.28}, {"rank": 0, "name": "aclnnSoftmaxGetWorkspaceSize", "totalTimeNs": 3515260, "totalCount": 3, "averageNs": 1171753.3, "durationRatio": 0.27}, {"rank": 0, "name": "aclnnRsubs", "totalTimeNs": 3443190, "totalCount": 34, "averageNs": 101270.3, "durationRatio": 0.27}, {"rank": 0, "name": "aclnnInplaceAdds", "totalTimeNs": 3168130, "totalCount": 17, "averageNs": 186360.6, "durationRatio": 0.25}, {"rank": 0, "name": "aclnnGeScalar", "totalTimeNs": 3155540, "totalCount": 19, "averageNs": 166081.1, "durationRatio": 0.24}, {"rank": 0, "name": "aclnnSubs", "totalTimeNs": 3046060, "totalCount": 17, "averageNs": 179180.0, "durationRatio": 0.24}, {"rank": 0, "name": "aclnnAddRmsNormGetWorkspaceSize", "totalTimeNs": 3030660, "totalCount": 256, "averageNs": 11838.5, "durationRatio"
   1.1	: 0.23}, {"rank": 0, "name": "aclnnEqScalar", "totalTimeNs": 2977670, "totalCount": 17, "averageNs": 175157.1, "durationRatio": 0.23}, {"rank": 0, "name": "aclnnLtTensor", "totalTimeNs": 2913460, "totalCount": 34, "averageNs": 85690.0, "durationRatio": 0.23}, {"rank": 0, "name": "aclnnGatherGetWorkspaceSize", "totalTimeNs": 2756870, "totalCount": 3, "averageNs": 918956.7, "durationRatio": 0.21}, {"rank": 0, "name": "aclnnLeTensor", "totalTimeNs": 2676620, "totalCount": 17, "averageNs": 157448.2, "durationRatio": 0.21}, {"rank": 0, "name": "aclnnInnerApplyRotaryPosEmb", "totalTimeNs": 2558320, "totalCount": 128, "averageNs": 19986.9, "durationRatio": 0.2}, {"rank": 0, "name": "aclnnEqScalarGetWorkspaceSize", "totalTimeNs": 2481210, "totalCount": 2, "averageNs": 1240605.0, "durationRatio": 0.19}, {"rank": 0, "name": "aclnnSortGetWorkspaceSize", "totalTimeNs": 2308150, "totalCount": 2, "averageNs": 1154075.0, "durationRatio": 0.18}, {"rank": 0, "name": "aclnnLeTensorGetWorkspaceSize", "totalTimeNs": 2305390, "totalCount": 2, "averageNs": 1152695.0, "durationRatio": 0.18}, {"rank": 0, "name": "aclnnArgMaxGetWorkspaceSize", "totalTimeNs": 2251920, "totalCount": 2, "averageNs": 1125960.0, "durationRatio": 0.17}, {"rank": 0, "name": "aclrtMemcpyAsync", "totalTimeNs": 2194930, "totalCount": 145, "averageNs": 15137.4, "durationRatio": 0.17}, {"rank": 0, "name": "aclnnSwiGlu", "totalTimeNs": 2101310, "totalCount": 128, "averageNs": 16416.5, "durationRatio": 0.16}, {"rank": 0, "name": "aclnnCast", "totalTimeNs": 2084200, "totalCount": 51, "averageNs": 40866.7, "durationRatio": 0.16}, {"rank": 0, "name": "Fill_Tiling", "totalTimeNs": 2050150, "totalCount": 22, "averageNs": 93188.6, "durationRatio": 0.16}, {"rank": 0, "name": "aclnnGather", "totalTimeNs": 1832870, "totalCount": 34, "averageNs": 53907.9, "durationRatio": 0.14}, {"rank": 0, "name": "aclnnRsubsGetWorkspaceSize", "totalTimeNs": 1733290, "totalCount": 4, "averageNs": 433322.5, "durationRatio": 0.13}, {"rank": 0, "name": "aclnnInnerApplyRotaryPosEmbGetWorkspaceSize", "totalTimeNs": 1702470, "totalCount": 128, "averageNs": 13300.5, "durationRatio": 0.13}, {"rank": 0, "name": "aclrtCreateEventExWithFlag", "totalTimeNs": 1675120, "totalCount": 568, "averageNs": 2949.2, "durationRatio": 0.13}, {"rank": 0, "name": "aclnnIndex", "totalTimeNs": 1663030, "totalCount": 17, "averageNs": 97825.3, "durationRatio": 0.13}, {"rank": 0, "name": "aclnnInplaceMaskedFillScalarGetWorkspaceSize", "totalTimeNs": 1527680, "totalCount": 7, "averageNs": 218240.0, "durationRatio": 0.12}, {"rank": 0, "name": "aclnnLogGetWorkspaceSize", "totalTimeNs": 1514450, "totalCount": 2, "averageNs": 757225.0, "durationRatio": 0.12}, {"rank": 0, "name": "aclnnSwiGluGetWorkspaceSize", "totalTimeNs": 1509560, "totalCount": 128, "averageNs": 11793.4, "durationRatio": 0.12}, {"rank": 0, "name": "aclnnSubsGetWorkspaceSize", "totalTimeNs": 1498230, "totalCount": 2, "averageNs": 749115.0, "durationRatio": 0.12}, {"rank": 0, "name": "aclnnAddsGetWorkspaceSize", "totalTimeNs": 1488810, "totalCount": 2, "averageNs": 744405.0, "durationRatio": 0.12}, {"rank": 0, "name": "aclnnInnerFusedInferAttentionScoreTiling", "totalTimeNs": 1482540, "totalCount": 17, "averageNs": 87208.2, "durationRatio": 0.11}, {"rank": 0, "name": "aclnnLtTensorGetWorkspaceSize", "totalTimeNs": 1463210, "totalCount": 3, "averageNs": 487736.7, "durationRatio": 0.11}, {"rank": 0, "name": "aclnnGeScalarGetWorkspaceSize", "totalTimeNs": 1452330, "totalCount": 2, "averageNs": 726165.0, "durationRatio": 0.11}, {"rank": 0, "name": "aclnnArgMax", "totalTimeNs": 1407270, "totalCount": 17, "averageNs": 82780.6, "durationRatio": 0.11}, {"rank": 0, "name": "aclnnSort", "totalTimeNs": 1371830, "totalCount": 17, "averageNs": 80695.9, "durationRatio": 0.11}, {"rank": 0, "name": "aclnnSoftmax", "totalTimeNs": 1353510, "totalCount": 34, "averageNs": 39809.1, "durationRatio": 0.1}, {"rank": 0, "name": "aclnnNegGetWorkspaceSize", "totalTimeNs": 1296050, "totalCount": 2, "averageNs": 648025.0, "durationRatio": 0.1}, {"rank": 0, "name": "aclnnInplaceDiv", "totalTimeNs": 1276550, "totalCount": 34, "averageNs": 37545.6, "durationRatio": 0.1}, {"rank": 0, "name": "aclrtQueryEventStatus", "totalTimeNs": 1252290, "totalCount": 264, "averageNs": 4743.5, "durationRatio": 0.1}, {"rank": 0, "name": "aclnnNeg", "totalTimeNs": 1149830, "totalCount": 17, "averageNs": 67637.1, "durationRatio": 0.09}, {"rank": 0, "name": "aclnnInplaceUniform", "totalTimeNs": 1122010, "totalCount": 17, "averageNs": 66000.6, "durationRatio": 0.09}, {"rank": 0, "name": "aclnnInplaceUniformGetWorkspaceSize", "totalTimeNs": 1099680, "totalCount": 17, "averageNs": 64687.1, "durationRatio": 0.09}, {"rank": 0, "name": "aclrtSetDevice", "totalTimeNs": 979430, "totalCount": 9, "averageNs": 108825.6, "durationRatio": 0.08}, {"rank": 0, "name": "aclnnInplaceLog", "totalTimeNs": 895030, "totalCount": 17, "averageNs": 52648.8, "durationRatio": 0.07}, {"rank": 0, "name": "aclmdlRIExecuteAsync", "totalT
   1.2	imeNs": 838990, "totalCount": 15, "averageNs": 55932.7, "durationRatio": 0.06}, {"rank": 0, "name": "aclnnIndexSelect", "totalTimeNs": 826310, "totalCount": 34, "averageNs": 24303.2, "durationRatio": 0.06}, {"rank": 0, "name": "aclnnMatmulGetWorkspaceSize", "totalTimeNs": 729720, "totalCount": 2, "averageNs": 364860.0, "durationRatio": 0.06}, {"rank": 0, "name": "aclrtGetCurrentContext", "totalTimeNs": 639350, "totalCount": 275, "averageNs": 2324.9, "durationRatio": 0.05}, {"rank": 0, "name": "aclnnRepeat", "totalTimeNs": 592480, "totalCount": 34, "averageNs": 17425.9, "durationRatio": 0.05}, {"rank": 0, "name": "aclnnMatmul", "totalTimeNs": 591710, "totalCount": 17, "averageNs": 34806.5, "durationRatio": 0.05}, {"rank": 0, "name": "aclnnCastGetWorkspaceSize", "totalTimeNs": 541090, "totalCount": 6, "averageNs": 90181.7, "durationRatio": 0.04}, {"rank": 0, "name": "Cast_Tiling", "totalTimeNs": 487710, "totalCount": 19, "averageNs": 25668.9, "durationRatio": 0.04}, {"rank": 0, "name": "ReduceSum_Tiling", "totalTimeNs": 485470, "totalCount": 4, "averageNs": 121367.5, "durationRatio": 0.04}, {"rank": 0, "name": "aclnnInplaceMuls", "totalTimeNs": 441140, "totalCount": 17, "averageNs": 25949.4, "durationRatio": 0.03}, {"rank": 0, "name": "aclrtMallocHostWithCfg", "totalTimeNs": 421210, "totalCount": 27, "averageNs": 15600.4, "durationRatio": 0.03}, {"rank": 0, "name": "MatMulV2_Tiling", "totalTimeNs": 407450, "totalCount": 4, "averageNs": 101862.5, "durationRatio": 0.03}, {"rank": 0, "name": "aclrtIpcMemSetImportPid", "totalTimeNs": 403020, "totalCount": 27, "averageNs": 14926.7, "durationRatio": 0.03}, {"rank": 0, "name": "aclrtNotifyGetExportKey", "totalTimeNs": 329130, "totalCount": 12, "averageNs": 27427.5, "durationRatio": 0.03}, {"rank": 0, "name": "aclrtMemcpyAsyncWithCondition", "totalTimeNs": 316250, "totalCount": 17, "averageNs": 18602.9, "durationRatio": 0.02}, {"rank": 0, "name": "Sub_Tiling", "totalTimeNs": 207510, "totalCount": 6, "averageNs": 34585.0, "durationRatio": 0.02}, {"rank": 0, "name": "aclnnDivGetWorkspaceSize", "totalTimeNs": 179210, "totalCount": 4, "averageNs": 44802.5, "durationRatio": 0.01}, {"rank": 0, "name": "MaskedFill_Tiling", "totalTimeNs": 154680, "totalCount": 7, "averageNs": 22097.1, "durationRatio": 0.01}, {"rank": 0, "name": "aclrtFreeHost", "totalTimeNs": 147660, "totalCount": 27, "averageNs": 5468.9, "durationRatio": 0.01}, {"rank": 0, "name": "SoftmaxV2_Tiling", "totalTimeNs": 134030, "totalCount": 6, "averageNs": 22338.3, "durationRatio": 0.01}, {"rank": 0, "name": "RealDiv_Tiling", "totalTimeNs": 133690, "totalCount": 4, "averageNs": 33422.5, "durationRatio": 0.01}, {"rank": 0, "name": "GatherElementsV2_Tiling", "totalTimeNs": 133500, "totalCount": 6, "averageNs": 22250.0, "durationRatio": 0.01}, {"rank": 0, "name": "Transpose_Tiling", "totalTimeNs": 129660, "totalCount": 2, "averageNs": 64830.0, "durationRatio": 0.01}, {"rank": 0, "name": "aclrtGetNotifyId", "totalTimeNs": 121040, "totalCount": 72, "averageNs": 1681.1, "durationRatio": 0.01}, {"rank": 0, "name": "LessEqual_Tiling", "totalTimeNs": 105430, "totalCount": 2, "averageNs": 52715.0, "durationRatio": 0.01}, {"rank": 0, "name": "Less_Tiling", "totalTimeNs": 91640, "totalCount": 3, "averageNs": 30546.7, "durationRatio": 0.01}, {"rank": 0, "name": "Neg_Tiling", "totalTimeNs": 87840, "totalCount": 2, "averageNs": 43920.0, "durationRatio": 0.01}, {"rank": 0, "name": "Add_Tiling", "totalTimeNs": 82500, "totalCount": 2, "averageNs": 41250.0, "durationRatio": 0.01}, {"rank": 0, "name": "BroadcastTo_Tiling", "totalTimeNs": 76770, "totalCount": 1, "averageNs": 76770.0, "durationRatio": 0.01}, {"rank": 0, "name": "GreaterEqual_Tiling", "totalTimeNs": 75860, "totalCount": 2, "averageNs": 37930.0, "durationRatio": 0.01}, {"rank": 0, "name": "aclnnInplaceMulsGetWorkspaceSize", "totalTimeNs": 70770, "totalCount": 2, "averageNs": 35385.0, "durationRatio": 0.01}, {"rank": 0, "name": "Equal_Tiling", "totalTimeNs": 66250, "totalCount": 2, "averageNs": 33125.0, "durationRatio": 0.01}, {"rank": 0, "name": "Index_Tiling", "totalTimeNs": 59320, "totalCount": 4, "averageNs": 14830.0, "durationRatio": 0.0}, {"rank": 0, "name": "aclnnMuls", "totalTimeNs": 50880, "totalCount": 2, "averageNs": 25440.0, "durationRatio": 0.0}, {"rank": 0, "name": "Sort_Tiling", "totalTimeNs": 50280, "totalCount": 4, "averageNs": 12570.0, "durationRatio": 0.0}, {"rank": 0, "name": "aclnnSub", "totalTimeNs": 47540, "totalCount": 2, "averageNs": 23770.0, "durationRatio": 0.0}, {"rank": 0, "name": "aclnnLtScalar", "totalTimeNs": 47250, "totalCount": 2, "averageNs": 23625.0, "durationRatio": 0.0}, {"rank": 0, "name": "aclrtResetDevice", "totalTimeNs": 45110, "totalCount": 9, "averageNs": 5012.2, "durationRatio": 0.0}, {"rank": 0, "name": "Mul_Tiling", "totalTimeNs": 40620, "totalCount": 2, "averageNs": 20310.0, "durationRatio": 0.0}, {"rank": 0, "name": "aclnnMul", "totalTimeNs": 37130, "totalCount": 2, "averageNs": 18565.0, "durationRatio": 0.0}, {"rank": 0
   1.3	, "name": "aclnnBitwiseAndTensor", "totalTimeNs": 29140, "totalCount": 2, "averageNs": 14570.0, "durationRatio": 0.0}, {"rank": 0, "name": "aclnnBitwiseNot", "totalTimeNs": 28140, "totalCount": 2, "averageNs": 14070.0, "durationRatio": 0.0}, {"rank": 0, "name": "ArgMaxV2_Tiling", "totalTimeNs": 27370, "totalCount": 2, "averageNs": 13685.0, "durationRatio": 0.0}, {"rank": 0, "name": "aclnnEmbedding", "totalTimeNs": 26420, "totalCount": 2, "averageNs": 13210.0, "durationRatio": 0.0}, {"rank": 0, "name": "Log_Tiling", "totalTimeNs": 25900, "totalCount": 2, "averageNs": 12950.0, "durationRatio": 0.0}, {"rank": 0, "name": "Cumsum_Tiling", "totalTimeNs": 17660, "totalCount": 2, "averageNs": 8830.0, "durationRatio": 0.0}, {"rank": 0, "name": "ViewCopy_Tiling", "totalTimeNs": 17400, "totalCount": 1, "averageNs": 17400.0, "durationRatio": 0.0}, {"rank": 0, "name": "aclrtGetResInCurrentThread", "totalTimeNs": 7260, "totalCount": 2, "averageNs": 3630.0, "durationRatio": 0.0}, {"rank": 1, "name": "launch", "totalTimeNs": 276833270, "totalCount": 4168, "averageNs": 66418.7, "durationRatio": 26.82}, {"rank": 1, "name": "aclnnInnerFusedInferAttentionScore", "totalTimeNs": 130070010, "totalCount": 1088, "averageNs": 119549.6, "durationRatio": 12.6}, {"rank": 1, "name": "hcom_allReduce_", "totalTimeNs": 70109340, "totalCount": 258, "averageNs": 271741.6, "durationRatio": 6.79}, {"rank": 1, "name": "aclrtRecordEvent", "totalTimeNs": 62734450, "totalCount": 1819, "averageNs": 34488.4, "durationRatio": 6.08}, {"rank": 1, "name": "hcom_allGather_", "totalTimeNs": 55669980, "totalCount": 17, "averageNs": 3274704.7, "durationRatio": 5.39}, {"rank": 1, "name": "aclnnInplaceCopy", "totalTimeNs": 31641100, "totalCount": 1220, "averageNs": 25935.3, "durationRatio": 3.07}, {"rank": 1, "name": "aclnnInnerFusedInferAttentionScoreGetWorkspaceSize", "totalTimeNs": 31586470, "totalCount": 1088, "averageNs": 29031.7, "durationRatio": 3.06}, {"rank": 1, "name": "aclnnInplaceCopyGetWorkspaceSize", "totalTimeNs": 27534170, "totalCount": 1034, "averageNs": 26628.8, "durationRatio": 2.67}, {"rank": 1, "name": "aclnnRmsNorm", "totalTimeNs": 23718440, "totalCount": 258, "averageNs": 91931.9, "durationRatio": 2.3}, {"rank": 1, "name": "aclnnContiguous", "totalTimeNs": 22402740, "totalCount": 320, "averageNs": 70008.6, "durationRatio": 2.17}, {"rank": 1, "name": "aclnnReduceSumGetWorkspaceSize", "totalTimeNs": 20012470, "totalCount": 2, "averageNs": 10006235.0, "durationRatio": 1.94}, {"rank": 1, "name": "aclrtSynchronizeEvent", "totalTimeNs": 19557910, "totalCount": 17, "averageNs": 1150465.3, "durationRatio": 1.89}, {"rank": 1, "name": "aclnnIndexGetWorkspaceSize", "totalTimeNs": 17700630, "totalCount": 4, "averageNs": 4425157.5, "durationRatio": 1.72}, {"rank": 1, "name": "aclnnRmsNormGetWorkspaceSize", "totalTimeNs": 15049560, "totalCount": 258, "averageNs": 58331.6, "durationRatio": 1.46}, {"rank": 1, "name": "aclnnInplaceFillScalar", "totalTimeNs": 13341690, "totalCount": 81, "averageNs": 164712.2, "durationRatio": 1.29}, {"rank": 1, "name": "aclnnContiguousGetWorkspaceSize", "totalTimeNs": 12316250, "totalCount": 320, "averageNs": 38488.3, "durationRatio": 1.19}, {"rank": 1, "name": "aclnnMm", "totalTimeNs": 12079430, "totalCount": 512, "averageNs": 23592.6, "durationRatio": 1.17}, {"rank": 1, "name": "aclnnReduceSum", "totalTimeNs": 10108360, "totalCount": 17, "averageNs": 594609.4, "durationRatio": 0.98}, {"rank": 1, "name": "aclrtSynchronizeDeviceWithTimeout", "totalTimeNs": 10014720, "totalCount": 16, "averageNs": 625920.0, "durationRatio": 0.97}, {"rank": 1, "name": "ReshapeAndCacheOperation::Execute", "totalTimeNs": 9718370, "totalCount": 128, "averageNs": 75924.8, "durationRatio": 0.94}, {"rank": 1, "name": "Launch_Ffts", "totalTimeNs": 8902960, "totalCount": 275, "averageNs": 32374.4, "durationRatio": 0.86}, {"rank": 1, "name": "aclnnAddRmsNorm", "totalTimeNs": 8544690, "totalCount": 256, "averageNs": 33377.7, "durationRatio": 0.83}, {"rank": 1, "name": "aclrtStreamWaitEvent", "totalTimeNs": 7373570, "totalCount": 567, "averageNs": 13004.5, "durationRatio": 0.71}, {"rank": 1, "name": "aclnnSoftmaxGetWorkspaceSize", "totalTimeNs": 6125650, "totalCount": 2, "averageNs": 3062825.0, "durationRatio": 0.59}, {"rank": 1, "name": "ReshapeAndCacheOperation::Setup", "totalTimeNs": 6104000, "totalCount": 128, "averageNs": 47687.5, "durationRatio": 0.59}, {"rank": 1, "name": "Slice_Tiling", "totalTimeNs": 5701760, "totalCount": 322, "averageNs": 17707.3, "durationRatio": 0.55}, {"rank": 1, "name": "aclnnInplaceFillScalarGetWorkspaceSize", "totalTimeNs": 4407700, "totalCount": 22, "averageNs": 200350.0, "durationRatio": 0.43}, {"rank": 1, "name": "aclrtMemcpyAsync", "totalTimeNs": 4202740, "totalCount": 145, "averageNs": 28984.4, "durationRatio": 0.41}, {"rank": 1, "name": "aclnnCumsum", "totalTimeNs": 4059350, "totalCount": 17, "averageNs": 238785.3, "durationRatio": 0.39}, {"rank": 1, "name": "aclnnCumsumGetWorkspaceSize", "totalTimeNs": 4024100
   1.4	, "totalCount": 2, "averageNs": 2012050.0, "durationRatio": 0.39}, {"rank": 1, "name": "aclnnInplaceMaskedFillScalar", "totalTimeNs": 3807060, "totalCount": 70, "averageNs": 54386.6, "durationRatio": 0.37}, {"rank": 1, "name": "aclnnRsubs", "totalTimeNs": 3328820, "totalCount": 34, "averageNs": 97906.5, "durationRatio": 0.32}, {"rank": 1, "name": "aclnnSubs", "totalTimeNs": 2971960, "totalCount": 17, "averageNs": 174821.2, "durationRatio": 0.29}, {"rank": 1, "name": "aclnnEqScalar", "totalTimeNs": 2971680, "totalCount": 17, "averageNs": 174804.7, "durationRatio": 0.29}, {"rank": 1, "name": "aclnnAddRmsNormGetWorkspaceSize", "totalTimeNs": 2927750, "totalCount": 256, "averageNs": 11436.5, "durationRatio": 0.28}, {"rank": 1, "name": "aclnnInnerApplyRotaryPosEmb", "totalTimeNs": 2895950, "totalCount": 128, "averageNs": 22624.6, "durationRatio": 0.28}, {"rank": 1, "name": "aclnnGeScalar", "totalTimeNs": 2862930, "totalCount": 19, "averageNs": 150680.5, "durationRatio": 0.28}, {"rank": 1, "name": "aclnnInplaceAdds", "totalTimeNs": 2799970, "totalCount": 17, "averageNs": 164704.1, "durationRatio": 0.27}, {"rank": 1, "name": "aclnnLtTensor", "totalTimeNs": 2735640, "totalCount": 34, "averageNs": 80460.0, "durationRatio": 0.27}, {"rank": 1, "name": "aclnnLeTensor", "totalTimeNs": 2598560, "totalCount": 17, "averageNs": 152856.5, "durationRatio": 0.25}, {"rank": 1, "name": "aclrtStreamGetId", "totalTimeNs": 2562900, "totalCount": 2696, "averageNs": 950.6, "durationRatio": 0.25}, {"rank": 1, "name": "aclnnSwiGlu", "totalTimeNs": 2530060, "totalCount": 128, "averageNs": 19766.1, "durationRatio": 0.25}, {"rank": 1, "name": "aclnnCast", "totalTimeNs": 2506860, "totalCount": 51, "averageNs": 49154.1, "durationRatio": 0.24}, {"rank": 1, "name": "aclnnSortGetWorkspaceSize", "totalTimeNs": 2504870, "totalCount": 2, "averageNs": 1252435.0, "durationRatio": 0.24}, {"rank": 1, "name": "aclnnEqScalarGetWorkspaceSize", "totalTimeNs": 2407990, "totalCount": 2, "averageNs": 1203995.0, "durationRatio": 0.23}, {"rank": 1, "name": "aclnnLeTensorGetWorkspaceSize", "totalTimeNs": 2318530, "totalCount": 2, "averageNs": 1159265.0, "durationRatio": 0.22}, {"rank": 1, "name": "aclnnInplaceDiv", "totalTimeNs": 2217530, "totalCount": 34, "averageNs": 65221.5, "durationRatio": 0.21}, {"rank": 1, "name": "aclnnIndex", "totalTimeNs": 2160270, "totalCount": 17, "averageNs": 127074.7, "durationRatio": 0.21}, {"rank": 1, "name": "aclnnGather", "totalTimeNs": 2111590, "totalCount": 34, "averageNs": 62105.6, "durationRatio": 0.2}, {"rank": 1, "name": "aclnnArgMaxGetWorkspaceSize", "totalTimeNs": 2090590, "totalCount": 2, "averageNs": 1045295.0, "durationRatio": 0.2}, {"rank": 1, "name": "Fill_Tiling", "totalTimeNs": 1887840, "totalCount": 22, "averageNs": 85810.9, "durationRatio": 0.18}, {"rank": 1, "name": "aclnnInnerFusedInferAttentionScoreTiling", "totalTimeNs": 1865030, "totalCount": 18, "averageNs": 103612.8, "durationRatio": 0.18}, {"rank": 1, "name": "aclrtCreateEventExWithFlag", "totalTimeNs": 1776650, "totalCount": 568, "averageNs": 3127.9, "durationRatio": 0.17}, {"rank": 1, "name": "aclnnSoftmax", "totalTimeNs": 1673080, "totalCount": 34, "averageNs": 49208.2, "durationRatio": 0.16}, {"rank": 1, "name": "aclnnSort", "totalTimeNs": 1648810, "totalCount": 17, "averageNs": 96988.8, "durationRatio": 0.16}, {"rank": 1, "name": "aclnnGatherGetWorkspaceSize", "totalTimeNs": 1578730, "totalCount": 2, "averageNs": 789365.0, "durationRatio": 0.15}, {"rank": 1, "name": "aclnnInnerApplyRotaryPosEmbGetWorkspaceSize", "totalTimeNs": 1574980, "totalCount": 128, "averageNs": 12304.5, "durationRatio": 0.15}, {"rank": 1, "name": "aclnnAddsGetWorkspaceSize", "totalTimeNs": 1550910, "totalCount": 2, "averageNs": 775455.0, "durationRatio": 0.15}, {"rank": 1, "name": "aclnnInplaceMaskedFillScalarGetWorkspaceSize", "totalTimeNs": 1532400, "totalCount": 7, "averageNs": 218914.3, "durationRatio": 0.15}, {"rank": 1, "name": "aclnnSubsGetWorkspaceSize", "totalTimeNs": 1523270, "totalCount": 2, "averageNs": 761635.0, "durationRatio": 0.15}, {"rank": 1, "name": "aclnnLtTensorGetWorkspaceSize", "totalTimeNs": 1481560, "totalCount": 3, "averageNs": 493853.3, "durationRatio": 0.14}, {"rank": 1, "name": "aclnnLogGetWorkspaceSize", "totalTimeNs": 1479250, "totalCount": 2, "averageNs": 739625.0, "durationRatio": 0.14}, {"rank": 1, "name": "aclnnRsubsGetWorkspaceSize", "totalTimeNs": 1454720, "totalCount": 4, "averageNs": 363680.0, "durationRatio": 0.14}, {"rank": 1, "name": "aclnnInplaceUniform", "totalTimeNs": 1447170, "totalCount": 17, "averageNs": 85127.6, "durationRatio": 0.14}, {"rank": 1, "name": "aclrtDestroyEvent", "totalTimeNs": 1446330, "totalCount": 555, "averageNs": 2606.0, "durationRatio": 0.14}, {"rank": 1, "name": "aclnnIndexSelect", "totalTimeNs": 1439070, "totalCount": 34, "averageNs": 42325.6, "durationRatio": 0.14}, {"rank": 1, "name": "aclnnGeScalarGetWorkspaceSize", "totalTimeNs": 1386090, "totalCount": 2, "averageNs": 693045.0, "durationRatio": 0.13},
   1.5	 {"rank": 1, "name": "aclnnSwiGluGetWorkspaceSize", "totalTimeNs": 1355420, "totalCount": 128, "averageNs": 10589.2, "durationRatio": 0.13}, {"rank": 1, "name": "aclnnNeg", "totalTimeNs": 1274620, "totalCount": 17, "averageNs": 74977.6, "durationRatio": 0.12}, {"rank": 1, "name": "aclnnArgMax", "totalTimeNs": 1191270, "totalCount": 17, "averageNs": 70074.7, "durationRatio": 0.12}, {"rank": 1, "name": "aclnnRepeat", "totalTimeNs": 1065550, "totalCount": 34, "averageNs": 31339.7, "durationRatio": 0.1}, {"rank": 1, "name": "aclnnNegGetWorkspaceSize", "totalTimeNs": 1015610, "totalCount": 2, "averageNs": 507805.0, "durationRatio": 0.1}, {"rank": 1, "name": "aclnnInplaceUniformGetWorkspaceSize", "totalTimeNs": 1007440, "totalCount": 17, "averageNs": 59261.2, "durationRatio": 0.1}, {"rank": 1, "name": "aclmdlRIExecuteAsync", "totalTimeNs": 993300, "totalCount": 15, "averageNs": 66220.0, "durationRatio": 0.1}, {"rank": 1, "name": "aclrtSetDevice", "totalTimeNs": 983160, "totalCount": 9, "averageNs": 109240.0, "durationRatio": 0.1}, {"rank": 1, "name": "aclnnInplaceLog", "totalTimeNs": 934160, "totalCount": 17, "averageNs": 54950.6, "durationRatio": 0.09}, {"rank": 1, "name": "aclnnMatmul", "totalTimeNs": 868980, "totalCount": 17, "averageNs": 51116.5, "durationRatio": 0.08}, {"rank": 1, "name": "aclrtGetCurrentContext", "totalTimeNs": 839870, "totalCount": 275, "averageNs": 3054.1, "durationRatio": 0.08}, {"rank": 1, "name": "aclnnInplaceMuls", "totalTimeNs": 678230, "totalCount": 17, "averageNs": 39895.9, "durationRatio": 0.07}, {"rank": 1, "name": "aclrtMemcpyAsyncWithCondition", "totalTimeNs": 660970, "totalCount": 17, "averageNs": 38880.6, "durationRatio": 0.06}, {"rank": 1, "name": "aclrtQueryEventStatus", "totalTimeNs": 646860, "totalCount": 263, "averageNs": 2459.5, "durationRatio": 0.06}, {"rank": 1, "name": "aclnnMatmulGetWorkspaceSize", "totalTimeNs": 600870, "totalCount": 2, "averageNs": 300435.0, "durationRatio": 0.06}, {"rank": 1, "name": "aclnnCastGetWorkspaceSize", "totalTimeNs": 520390, "totalCount": 6, "averageNs": 86731.7, "durationRatio": 0.05}, {"rank": 1, "name": "Cast_Tiling", "totalTimeNs": 482260, "totalCount": 18, "averageNs": 26792.2, "durationRatio": 0.05}, {"rank": 1, "name": "aclnnDivGetWorkspaceSize", "totalTimeNs": 405740, "totalCount": 4, "averageNs": 101435.0, "durationRatio": 0.04}, {"rank": 1, "name": "MatMulV2_Tiling", "totalTimeNs": 393440, "totalCount": 4, "averageNs": 98360.0, "durationRatio": 0.04}, {"rank": 1, "name": "aclrtNotifyGetExportKey", "totalTimeNs": 353500, "totalCount": 12, "averageNs": 29458.3, "durationRatio": 0.03}, {"rank": 1, "name": "ReduceSum_Tiling", "totalTimeNs": 341130, "totalCount": 4, "averageNs": 85282.5, "durationRatio": 0.03}, {"rank": 1, "name": "aclrtIpcMemSetImportPid", "totalTimeNs": 304780, "totalCount": 27, "averageNs": 11288.1, "durationRatio": 0.03}, {"rank": 1, "name": "Transpose_Tiling", "totalTimeNs": 288210, "totalCount": 2, "averageNs": 144105.0, "durationRatio": 0.03}, {"rank": 1, "name": "aclrtMallocHostWithCfg", "totalTimeNs": 287890, "totalCount": 27, "averageNs": 10662.6, "durationRatio": 0.03}, {"rank": 1, "name": "Sub_Tiling", "totalTimeNs": 210000, "totalCount": 6, "averageNs": 35000.0, "durationRatio": 0.02}, {"rank": 1, "name": "MaskedFill_Tiling", "totalTimeNs": 178530, "totalCount": 7, "averageNs": 25504.3, "durationRatio": 0.02}, {"rank": 1, "name": "RealDiv_Tiling", "totalTimeNs": 167910, "totalCount": 4, "averageNs": 41977.5, "durationRatio": 0.02}, {"rank": 1, "name": "aclnnMuls", "totalTimeNs": 163180, "totalCount": 2, "averageNs": 81590.0, "durationRatio": 0.02}, {"rank": 1, "name": "GatherElementsV2_Tiling", "totalTimeNs": 159360, "totalCount": 4, "averageNs": 39840.0, "durationRatio": 0.02}, {"rank": 1, "name": "aclrtFreeHost", "totalTimeNs": 148100, "totalCount": 27, "averageNs": 5485.2, "durationRatio": 0.01}, {"rank": 1, "name": "aclrtGetNotifyId", "totalTimeNs": 122900, "totalCount": 72, "averageNs": 1706.9, "durationRatio": 0.01}, {"rank": 1, "name": "SoftmaxV2_Tiling", "totalTimeNs": 121570, "totalCount": 4, "averageNs": 30392.5, "durationRatio": 0.01}, {"rank": 1, "name": "aclnnEmbedding", "totalTimeNs": 118610, "totalCount": 2, "averageNs": 59305.0, "durationRatio": 0.01}, {"rank": 1, "name": "aclnnSub", "totalTimeNs": 111120, "totalCount": 2, "averageNs": 55560.0, "durationRatio": 0.01}, {"rank": 1, "name": "Less_Tiling", "totalTimeNs": 105390, "totalCount": 3, "averageNs": 35130.0, "durationRatio": 0.01}, {"rank": 1, "name": "aclnnInnerApplyRotaryPosEmbTiling", "totalTimeNs": 103250, "totalCount": 1, "averageNs": 103250.0, "durationRatio": 0.01}, {"rank": 1, "name": "Sort_Tiling", "totalTimeNs": 102120, "totalCount": 4, "averageNs": 25530.0, "durationRatio": 0.01}, {"rank": 1, "name": "aclnnInplaceMulsGetWorkspaceSize", "totalTimeNs": 96220, "totalCount": 2, "averageNs": 48110.0, "durationRatio": 0.01}, {"rank": 1, "name": "aclnnRmsNormTiling", "totalTimeNs": 94730, "totalCount": 2, "averageNs": 47365.0, 
   1.6	"durationRatio": 0.01}, {"rank": 1, "name": "aclnnLtScalar", "totalTimeNs": 89720, "totalCount": 2, "averageNs": 44860.0, "durationRatio": 0.01}, {"rank": 1, "name": "aclnnAddRmsNormTiling", "totalTimeNs": 87860, "totalCount": 1, "averageNs": 87860.0, "durationRatio": 0.01}, {"rank": 1, "name": "LessEqual_Tiling", "totalTimeNs": 74400, "totalCount": 2, "averageNs": 37200.0, "durationRatio": 0.01}, {"rank": 1, "name": "aclnnMul", "totalTimeNs": 72720, "totalCount": 2, "averageNs": 36360.0, "durationRatio": 0.01}, {"rank": 1, "name": "Equal_Tiling", "totalTimeNs": 58660, "totalCount": 2, "averageNs": 29330.0, "durationRatio": 0.01}, {"rank": 1, "name": "Add_Tiling", "totalTimeNs": 58630, "totalCount": 2, "averageNs": 29315.0, "durationRatio": 0.01}, {"rank": 1, "name": "GreaterEqual_Tiling", "totalTimeNs": 55780, "totalCount": 2, "averageNs": 27890.0, "durationRatio": 0.01}, {"rank": 1, "name": "Mul_Tiling", "totalTimeNs": 55490, "totalCount": 2, "averageNs": 27745.0, "durationRatio": 0.01}, {"rank": 1, "name": "aclnnBitwiseAndTensor", "totalTimeNs": 55400, "totalCount": 2, "averageNs": 27700.0, "durationRatio": 0.01}, {"rank": 1, "name": "ArgMaxV2_Tiling", "totalTimeNs": 48780, "totalCount": 2, "averageNs": 24390.0, "durationRatio": 0.0}, {"rank": 1, "name": "Index_Tiling", "totalTimeNs": 46510, "totalCount": 4, "averageNs": 11627.5, "durationRatio": 0.0}, {"rank": 1, "name": "BroadcastTo_Tiling", "totalTimeNs": 45260, "totalCount": 1, "averageNs": 45260.0, "durationRatio": 0.0}, {"rank": 1, "name": "aclrtResetDevice", "totalTimeNs": 37090, "totalCount": 9, "averageNs": 4121.1, "durationRatio": 0.0}, {"rank": 1, "name": "aclnnBitwiseNot", "totalTimeNs": 34340, "totalCount": 2, "averageNs": 17170.0, "durationRatio": 0.0}, {"rank": 1, "name": "Neg_Tiling", "totalTimeNs": 23690, "totalCount": 2, "averageNs": 11845.0, "durationRatio": 0.0}, {"rank": 1, "name": "Log_Tiling", "totalTimeNs": 22410, "totalCount": 2, "averageNs": 11205.0, "durationRatio": 0.0}, {"rank": 1, "name": "Cumsum_Tiling", "totalTimeNs": 16640, "totalCount": 2, "averageNs": 8320.0, "durationRatio": 0.0}, {"rank": 1, "name": "ViewCopy_Tiling", "totalTimeNs": 13420, "totalCount": 1, "averageNs": 13420.0, "durationRatio": 0.0}, {"rank": 1, "name": "aclrtGetResInCurrentThread", "totalTimeNs": 5720, "totalCount": 2, "averageNs": 2860.0, "durationRatio": 0.0}, {"rank": 2, "name": "aclrtSynchronizeEvent", "totalTimeNs": 285744680, "totalCount": 17, "averageNs": 16808510.6, "durationRatio": 22.35}, {"rank": 2, "name": "launch", "totalTimeNs": 272946550, "totalCount": 4168, "averageNs": 65486.2, "durationRatio": 21.35}, {"rank": 2, "name": "hcom_allGather_", "totalTimeNs": 102764470, "totalCount": 17, "averageNs": 6044968.8, "durationRatio": 8.04}, {"rank": 2, "name": "aclnnInnerFusedInferAttentionScore", "totalTimeNs": 88328210, "totalCount": 1088, "averageNs": 81184.0, "durationRatio": 6.91}, {"rank": 2, "name": "hcom_allReduce_", "totalTimeNs": 70265870, "totalCount": 258, "averageNs": 272348.3, "durationRatio": 5.5}, {"rank": 2, "name": "aclrtRecordEvent", "totalTimeNs": 40574810, "totalCount": 1819, "averageNs": 22306.1, "durationRatio": 3.17}, {"rank": 2, "name": "aclnnInnerFusedInferAttentionScoreGetWorkspaceSize", "totalTimeNs": 29504420, "totalCount": 1088, "averageNs": 27118.0, "durationRatio": 2.31}, {"rank": 2, "name": "aclnnInplaceCopyGetWorkspaceSize", "totalTimeNs": 28932580, "totalCount": 1034, "averageNs": 27981.2, "durationRatio": 2.26}, {"rank": 2, "name": "aclnnInplaceCopy", "totalTimeNs": 28838870, "totalCount": 1220, "averageNs": 23638.4, "durationRatio": 2.26}, {"rank": 2, "name": "aclnnRmsNorm", "totalTimeNs": 23447850, "totalCount": 258, "averageNs": 90883.1, "durationRatio": 1.83}, {"rank": 2, "name": "aclnnContiguous", "totalTimeNs": 22519800, "totalCount": 320, "averageNs": 70374.4, "durationRatio": 1.76}, {"rank": 2, "name": "aclnnReduceSumGetWorkspaceSize", "totalTimeNs": 21210370, "totalCount": 2, "averageNs": 10605185.0, "durationRatio": 1.66}, {"rank": 2, "name": "aclnnIndexGetWorkspaceSize", "totalTimeNs": 15960480, "totalCount": 4, "averageNs": 3990120.0, "durationRatio": 1.25}, {"rank": 2, "name": "aclnnRmsNormGetWorkspaceSize", "totalTimeNs": 15543560, "totalCount": 258, "averageNs": 60246.4, "durationRatio": 1.22}, {"rank": 2, "name": "aclnnContiguousGetWorkspaceSize", "totalTimeNs": 13482900, "totalCount": 320, "averageNs": 42134.1, "durationRatio": 1.05}, {"rank": 2, "name": "aclnnInplaceFillScalar", "totalTimeNs": 12635050, "totalCount": 81, "averageNs": 155988.3, "durationRatio": 0.99}, {"rank": 2, "name": "aclnnReduceSum", "totalTimeNs": 12208830, "totalCount": 17, "averageNs": 718166.5, "durationRatio": 0.95}, {"rank": 2, "name": "aclrtSynchronizeDeviceWithTimeout", "totalTimeNs": 12192360, "totalCount": 16, "averageNs": 762022.5, "durationRatio": 0.95}, {"rank": 2, "name": "Launch_Ffts", "totalTimeNs": 9932600, "totalCount": 275, "averageNs": 36118.5, "durationRatio": 0.78}, {"rank": 2, "name": "aclnnMm", "
   1.7	totalTimeNs": 9411900, "totalCount": 512, "averageNs": 18382.6, "durationRatio": 0.74}, {"rank": 2, "name": "ReshapeAndCacheOperation::Execute", "totalTimeNs": 8650090, "totalCount": 128, "averageNs": 67578.8, "durationRatio": 0.68}, {"rank": 2, "name": "aclnnAddRmsNorm", "totalTimeNs": 7182450, "totalCount": 256, "averageNs": 28056.4, "durationRatio": 0.56}, {"rank": 2, "name": "aclnnLtTensor", "totalTimeNs": 6831690, "totalCount": 34, "averageNs": 200932.1, "durationRatio": 0.53}, {"rank": 2, "name": "Slice_Tiling", "totalTimeNs": 6127280, "totalCount": 322, "averageNs": 19028.8, "durationRatio": 0.48}, {"rank": 2, "name": "aclrtStreamWaitEvent", "totalTimeNs": 5644320, "totalCount": 567, "averageNs": 9954.7, "durationRatio": 0.44}, {"rank": 2, "name": "ReshapeAndCacheOperation::Setup", "totalTimeNs": 5555390, "totalCount": 128, "averageNs": 43401.5, "durationRatio": 0.43}, {"rank": 2, "name": "aclrtDestroyEvent", "totalTimeNs": 4606250, "totalCount": 556, "averageNs": 8284.6, "durationRatio": 0.36}, {"rank": 2, "name": "aclnnEqScalar", "totalTimeNs": 4455080, "totalCount": 17, "averageNs": 262063.5, "durationRatio": 0.35}, {"rank": 2, "name": "aclnnCumsum", "totalTimeNs": 4312170, "totalCount": 17, "averageNs": 253657.1, "durationRatio": 0.34}, {"rank": 2, "name": "aclnnCumsumGetWorkspaceSize", "totalTimeNs": 4307890, "totalCount": 2, "averageNs": 2153945.0, "durationRatio": 0.34}, {"rank": 2, "name": "aclnnInplaceFillScalarGetWorkspaceSize", "totalTimeNs": 4264790, "totalCount": 22, "averageNs": 193854.1, "durationRatio": 0.33}, {"rank": 2, "name": "aclnnInplaceMaskedFillScalar", "totalTimeNs": 4226160, "totalCount": 70, "averageNs": 60373.7, "durationRatio": 0.33}, {"rank": 2, "name": "aclnnSubsGetWorkspaceSize", "totalTimeNs": 3852860, "totalCount": 2, "averageNs": 1926430.0, "durationRatio": 0.3}, {"rank": 2, "name": "aclnnRsubs", "totalTimeNs": 3645120, "totalCount": 34, "averageNs": 107209.4, "durationRatio": 0.29}, {"rank": 2, "name": "aclnnSoftmaxGetWorkspaceSize", "totalTimeNs": 3499640, "totalCount": 3, "averageNs": 1166546.7, "durationRatio": 0.27}, {"rank": 2, "name": "aclnnSubs", "totalTimeNs": 3367550, "totalCount": 17, "averageNs": 198091.2, "durationRatio": 0.26}, {"rank": 2, "name": "aclnnInplaceAdds", "totalTimeNs": 3064590, "totalCount": 17, "averageNs": 180270.0, "durationRatio": 0.24}, {"rank": 2, "name": "aclnnGeScalar", "totalTimeNs": 3004710, "totalCount": 19, "averageNs": 158142.6, "durationRatio": 0.23}, {"rank": 2, "name": "aclnnAddRmsNormGetWorkspaceSize", "totalTimeNs": 2997680, "totalCount": 256, "averageNs": 11709.7, "durationRatio": 0.23}, {"rank": 2, "name": "aclnnLeTensor", "totalTimeNs": 2899040, "totalCount": 17, "averageNs": 170531.8, "durationRatio": 0.23}, {"rank": 2, "name": "aclnnInnerApplyRotaryPosEmb", "totalTimeNs": 2677890, "totalCount": 128, "averageNs": 20921.0, "durationRatio": 0.21}, {"rank": 2, "name": "aclnnArgMaxGetWorkspaceSize", "totalTimeNs": 2540210, "totalCount": 2, "averageNs": 1270105.0, "durationRatio": 0.2}, {"rank": 2, "name": "aclrtQueryEventStatus", "totalTimeNs": 2528970, "totalCount": 264, "averageNs": 9579.4, "durationRatio": 0.2}, {"rank": 2, "name": "aclnnEqScalarGetWorkspaceSize", "totalTimeNs": 2449270, "totalCount": 2, "averageNs": 1224635.0, "durationRatio": 0.19}, {"rank": 2, "name": "aclrtStreamGetId", "totalTimeNs": 2431710, "totalCount": 2696, "averageNs": 902.0, "durationRatio": 0.19}, {"rank": 2, "name": "aclnnLeTensorGetWorkspaceSize", "totalTimeNs": 2400630, "totalCount": 2, "averageNs": 1200315.0, "durationRatio": 0.19}, {"rank": 2, "name": "aclrtMemcpyAsync", "totalTimeNs": 2389080, "totalCount": 145, "averageNs": 16476.4, "durationRatio": 0.19}, {"rank": 2, "name": "aclnnSwiGlu", "totalTimeNs": 2338940, "totalCount": 128, "averageNs": 18273.0, "durationRatio": 0.18}, {"rank": 2, "name": "aclnnSortGetWorkspaceSize", "totalTimeNs": 2296190, "totalCount": 2, "averageNs": 1148095.0, "durationRatio": 0.18}, {"rank": 2, "name": "Fill_Tiling", "totalTimeNs": 2191090, "totalCount": 22, "averageNs": 99595.0, "durationRatio": 0.17}, {"rank": 2, "name": "aclnnGather", "totalTimeNs": 2058260, "totalCount": 34, "averageNs": 60537.1, "durationRatio": 0.16}, {"rank": 2, "name": "aclnnCast", "totalTimeNs": 1961830, "totalCount": 51, "averageNs": 38467.3, "durationRatio": 0.15}, {"rank": 2, "name": "aclrtCreateEventExWithFlag", "totalTimeNs": 1923780, "totalCount": 568, "averageNs": 3386.9, "durationRatio": 0.15}, {"rank": 2, "name": "aclnnGatherGetWorkspaceSize", "totalTimeNs": 1783320, "totalCount": 3, "averageNs": 594440.0, "durationRatio": 0.14}, {"rank": 2, "name": "aclnnIndex", "totalTimeNs": 1704230, "totalCount": 17, "averageNs": 100248.8, "durationRatio": 0.13}, {"rank": 2, "name": "aclnnInnerApplyRotaryPosEmbGetWorkspaceSize", "totalTimeNs": 1610840, "totalCount": 128, "averageNs": 12584.7, "durationRatio": 0.13}, {"rank": 2, "name": "aclnnAddsGetWorkspaceSize", "totalTimeNs": 1544370, "totalCount": 2, "averageNs": 772185.0, "du
   1.8	rationRatio": 0.12}, {"rank": 2, "name": "aclnnRsubsGetWorkspaceSize", "totalTimeNs": 1520170, "totalCount": 4, "averageNs": 380042.5, "durationRatio": 0.12}, {"rank": 2, "name": "aclnnLtTensorGetWorkspaceSize", "totalTimeNs": 1502070, "totalCount": 3, "averageNs": 500690.0, "durationRatio": 0.12}, {"rank": 2, "name": "aclnnLogGetWorkspaceSize", "totalTimeNs": 1495300, "totalCount": 2, "averageNs": 747650.0, "durationRatio": 0.12}, {"rank": 2, "name": "aclnnGeScalarGetWorkspaceSize", "totalTimeNs": 1479390, "totalCount": 2, "averageNs": 739695.0, "durationRatio": 0.12}, {"rank": 2, "name": "aclnnInplaceMaskedFillScalarGetWorkspaceSize", "totalTimeNs": 1459650, "totalCount": 7, "averageNs": 208521.4, "durationRatio": 0.11}, {"rank": 2, "name": "aclnnSoftmax", "totalTimeNs": 1444360, "totalCount": 34, "averageNs": 42481.2, "durationRatio": 0.11}, {"rank": 2, "name": "aclnnInnerFusedInferAttentionScoreTiling", "totalTimeNs": 1403890, "totalCount": 17, "averageNs": 82581.8, "durationRatio": 0.11}, {"rank": 2, "name": "aclnnNegGetWorkspaceSize", "totalTimeNs": 1381460, "totalCount": 2, "averageNs": 690730.0, "durationRatio": 0.11}, {"rank": 2, "name": "aclnnArgMax", "totalTimeNs": 1365280, "totalCount": 17, "averageNs": 80310.6, "durationRatio": 0.11}, {"rank": 2, "name": "aclnnSwiGluGetWorkspaceSize", "totalTimeNs": 1360680, "totalCount": 128, "averageNs": 10630.3, "durationRatio": 0.11}, {"rank": 2, "name": "aclnnSort", "totalTimeNs": 1346810, "totalCount": 17, "averageNs": 79224.1, "durationRatio": 0.11}, {"rank": 2, "name": "aclnnNeg", "totalTimeNs": 1123590, "totalCount": 17, "averageNs": 66093.5, "durationRatio": 0.09}, {"rank": 2, "name": "aclrtSetDevice", "totalTimeNs": 1121110, "totalCount": 9, "averageNs": 124567.8, "durationRatio": 0.09}, {"rank": 2, "name": "aclnnInplaceDiv", "totalTimeNs": 1092610, "totalCount": 34, "averageNs": 32135.6, "durationRatio": 0.09}, {"rank": 2, "name": "aclnnInplaceUniformGetWorkspaceSize", "totalTimeNs": 1038160, "totalCount": 17, "averageNs": 61068.2, "durationRatio": 0.08}, {"rank": 2, "name": "aclnnIndexSelect", "totalTimeNs": 1034130, "totalCount": 34, "averageNs": 30415.6, "durationRatio": 0.08}, {"rank": 2, "name": "aclnnInplaceUniform", "totalTimeNs": 986650, "totalCount": 17, "averageNs": 58038.2, "durationRatio": 0.08}, {"rank": 2, "name": "aclnnInplaceLog", "totalTimeNs": 886430, "totalCount": 17, "averageNs": 52142.9, "durationRatio": 0.07}, {"rank": 2, "name": "aclnnRepeat", "totalTimeNs": 845610, "totalCount": 34, "averageNs": 24870.9, "durationRatio": 0.07}, {"rank": 2, "name": "aclmdlRIExecuteAsync", "totalTimeNs": 823210, "totalCount": 15, "averageNs": 54880.7, "durationRatio": 0.06}, {"rank": 2, "name": "aclrtGetCurrentContext", "totalTimeNs": 632540, "totalCount": 275, "averageNs": 2300.1, "durationRatio": 0.05}, {"rank": 2, "name": "aclnnMatmul", "totalTimeNs": 621850, "totalCount": 17, "averageNs": 36579.4, "durationRatio": 0.05}, {"rank": 2, "name": "aclnnMatmulGetWorkspaceSize", "totalTimeNs": 607860, "totalCount": 2, "averageNs": 303930.0, "durationRatio": 0.05}, {"rank": 2, "name": "aclnnInplaceMuls", "totalTimeNs": 573950, "totalCount": 17, "averageNs": 33761.8, "durationRatio": 0.04}, {"rank": 2, "name": "aclnnCastGetWorkspaceSize", "totalTimeNs": 513130, "totalCount": 6, "averageNs": 85521.7, "durationRatio": 0.04}, {"rank": 2, "name": "ReduceSum_Tiling", "totalTimeNs": 498260, "totalCount": 4, "averageNs": 124565.0, "durationRatio": 0.04}, {"rank": 2, "name": "Cast_Tiling", "totalTimeNs": 475130, "totalCount": 19, "averageNs": 25006.8, "durationRatio": 0.04}, {"rank": 2, "name": "MatMulV2_Tiling", "totalTimeNs": 356820, "totalCount": 4, "averageNs": 89205.0, "durationRatio": 0.03}, {"rank": 2, "name": "aclrtMemcpyAsyncWithCondition", "totalTimeNs": 323420, "totalCount": 17, "averageNs": 19024.7, "durationRatio": 0.03}, {"rank": 2, "name": "aclrtMallocHostWithCfg", "totalTimeNs": 319380, "totalCount": 27, "averageNs": 11828.9, "durationRatio": 0.02}, {"rank": 2, "name": "aclrtIpcMemSetImportPid", "totalTimeNs": 310250, "totalCount": 27, "averageNs": 11490.7, "durationRatio": 0.02}, {"rank": 2, "name": "aclrtNotifyGetExportKey", "totalTimeNs": 307200, "totalCount": 12, "averageNs": 25600.0, "durationRatio": 0.02}, {"rank": 2, "name": "Sub_Tiling", "totalTimeNs": 222150, "totalCount": 6, "averageNs": 37025.0, "durationRatio": 0.02}, {"rank": 2, "name": "MaskedFill_Tiling", "totalTimeNs": 195770, "totalCount": 7, "averageNs": 27967.1, "durationRatio": 0.02}, {"rank": 2, "name": "Transpose_Tiling", "totalTimeNs": 183070, "totalCount": 2, "averageNs": 91535.0, "durationRatio": 0.01}, {"rank": 2, "name": "aclnnDivGetWorkspaceSize", "totalTimeNs": 153810, "totalCount": 4, "averageNs": 38452.5, "durationRatio": 0.01}, {"rank": 2, "name": "GatherElementsV2_Tiling", "totalTimeNs": 153810, "totalCount": 6, "averageNs": 25635.0, "durationRatio": 0.01}, {"rank": 2, "name": "aclrtFreeHost", "totalTimeNs": 126440, "totalCount": 27, "averageNs": 4683.0, "dura
   1.9	tionRatio": 0.01}, {"rank": 2, "name": "aclrtGetNotifyId", "totalTimeNs": 121130, "totalCount": 72, "averageNs": 1682.4, "durationRatio": 0.01}, {"rank": 2, "name": "RealDiv_Tiling", "totalTimeNs": 113970, "totalCount": 4, "averageNs": 28492.5, "durationRatio": 0.01}, {"rank": 2, "name": "Less_Tiling", "totalTimeNs": 112550, "totalCount": 3, "averageNs": 37516.7, "durationRatio": 0.01}, {"rank": 2, "name": "SoftmaxV2_Tiling", "totalTimeNs": 108310, "totalCount": 6, "averageNs": 18051.7, "durationRatio": 0.01}, {"rank": 2, "name": "Equal_Tiling", "totalTimeNs": 96170, "totalCount": 2, "averageNs": 48085.0, "durationRatio": 0.01}, {"rank": 2, "name": "Add_Tiling", "totalTimeNs": 93720, "totalCount": 2, "averageNs": 46860.0, "durationRatio": 0.01}, {"rank": 2, "name": "LessEqual_Tiling", "totalTimeNs": 76370, "totalCount": 2, "averageNs": 38185.0, "durationRatio": 0.01}, {"rank": 2, "name": "Mul_Tiling", "totalTimeNs": 75750, "totalCount": 2, "averageNs": 37875.0, "durationRatio": 0.01}, {"rank": 2, "name": "aclnnEmbedding", "totalTimeNs": 72400, "totalCount": 2, "averageNs": 36200.0, "durationRatio": 0.01}, {"rank": 2, "name": "GreaterEqual_Tiling", "totalTimeNs": 69950, "totalCount": 2, "averageNs": 34975.0, "durationRatio": 0.01}, {"rank": 2, "name": "aclnnInplaceMulsGetWorkspaceSize", "totalTimeNs": 68510, "totalCount": 2, "averageNs": 34255.0, "durationRatio": 0.01}, {"rank": 2, "name": "aclnnMuls", "totalTimeNs": 66570, "totalCount": 2, "averageNs": 33285.0, "durationRatio": 0.01}, {"rank": 2, "name": "BroadcastTo_Tiling", "totalTimeNs": 59570, "totalCount": 1, "averageNs": 59570.0, "durationRatio": 0.0}, {"rank": 2, "name": "Neg_Tiling", "totalTimeNs": 58260, "totalCount": 2, "averageNs": 29130.0, "durationRatio": 0.0}, {"rank": 2, "name": "aclnnSub", "totalTimeNs": 51470, "totalCount": 2, "averageNs": 25735.0, "durationRatio": 0.0}, {"rank": 2, "name": "aclnnLtScalar", "totalTimeNs": 50030, "totalCount": 2, "averageNs": 25015.0, "durationRatio": 0.0}, {"rank": 2, "name": "Index_Tiling", "totalTimeNs": 48570, "totalCount": 4, "averageNs": 12142.5, "durationRatio": 0.0}, {"rank": 2, "name": "aclnnMul", "totalTimeNs": 44170, "totalCount": 2, "averageNs": 22085.0, "durationRatio": 0.0}, {"rank": 2, "name": "Sort_Tiling", "totalTimeNs": 43050, "totalCount": 4, "averageNs": 10762.5, "durationRatio": 0.0}, {"rank": 2, "name": "aclrtResetDevice", "totalTimeNs": 41700, "totalCount": 9, "averageNs": 4633.3, "durationRatio": 0.0}, {"rank": 2, "name": "aclnnBitwiseAndTensor", "totalTimeNs": 38950, "totalCount": 2, "averageNs": 19475.0, "durationRatio": 0.0}, {"rank": 2, "name": "aclnnBitwiseNot", "totalTimeNs": 33740, "totalCount": 2, "averageNs": 16870.0, "durationRatio": 0.0}, {"rank": 2, "name": "Log_Tiling", "totalTimeNs": 27400, "totalCount": 2, "averageNs": 13700.0, "durationRatio": 0.0}, {"rank": 2, "name": "ArgMaxV2_Tiling", "totalTimeNs": 24270, "totalCount": 2, "averageNs": 12135.0, "durationRatio": 0.0}, {"rank": 2, "name": "Cumsum_Tiling", "totalTimeNs": 14060, "totalCount": 2, "averageNs": 7030.0, "durationRatio": 0.0}, {"rank": 2, "name": "ViewCopy_Tiling", "totalTimeNs": 11570, "totalCount": 1, "averageNs": 11570.0, "durationRatio": 0.0}, {"rank": 2, "name": "aclrtGetResInCurrentThread", "totalTimeNs": 6770, "totalCount": 2, "averageNs": 3385.0, "durationRatio": 0.0}, {"rank": 3, "name": "aclrtSynchronizeEvent", "totalTimeNs": 308672570, "totalCount": 17, "averageNs": 18157210.0, "durationRatio": 23.79}, {"rank": 3, "name": "launch", "totalTimeNs": 272194210, "totalCount": 4168, "averageNs": 65305.7, "durationRatio": 20.97}, {"rank": 3, "name": "hcom_allGather_", "totalTimeNs": 100316480, "totalCount": 17, "averageNs": 5900969.4, "durationRatio": 7.73}, {"rank": 3, "name": "aclnnInnerFusedInferAttentionScore", "totalTimeNs": 88060800, "totalCount": 1088, "averageNs": 80938.2, "durationRatio": 6.79}, {"rank": 3, "name": "hcom_allReduce_", "totalTimeNs": 72131380, "totalCount": 258, "averageNs": 279579.0, "durationRatio": 5.56}, {"rank": 3, "name": "aclrtRecordEvent", "totalTimeNs": 40500070, "totalCount": 1819, "averageNs": 22265.0, "durationRatio": 3.12}, {"rank": 3, "name": "aclnnInplaceCopy", "totalTimeNs": 32881130, "totalCount": 1220, "averageNs": 26951.7, "durationRatio": 2.53}, {"rank": 3, "name": "aclnnInnerFusedInferAttentionScoreGetWorkspaceSize", "totalTimeNs": 29475000, "totalCount": 1088, "averageNs": 27091.0, "durationRatio": 2.27}, {"rank": 3, "name": "aclnnInplaceCopyGetWorkspaceSize", "totalTimeNs": 28935770, "totalCount": 1034, "averageNs": 27984.3, "durationRatio": 2.23}, {"rank": 3, "name": "aclnnRmsNorm", "totalTimeNs": 23716990, "totalCount": 258, "averageNs": 91926.3, "durationRatio": 1.83}, {"rank": 3, "name": "aclnnContiguous", "totalTimeNs": 23087570, "totalCount": 320, "averageNs": 72148.7, "durationRatio": 1.78}, {"rank": 3, "name": "aclnnReduceSumGetWorkspaceSize", "totalTimeNs": 21179030, "totalCount": 2, "averageNs": 10589515.0, "durationRatio": 1.63}, {"rank": 3, "name
  1.10	": "aclnnRmsNormGetWorkspaceSize", "totalTimeNs": 15945840, "totalCount": 258, "averageNs": 61805.6, "durationRatio": 1.23}, {"rank": 3, "name": "aclnnIndexGetWorkspaceSize", "totalTimeNs": 15922720, "totalCount": 4, "averageNs": 3980680.0, "durationRatio": 1.23}, {"rank": 3, "name": "aclnnContiguousGetWorkspaceSize", "totalTimeNs": 13916710, "totalCount": 320, "averageNs": 43489.7, "durationRatio": 1.07}, {"rank": 3, "name": "aclnnReduceSum", "totalTimeNs": 12602520, "totalCount": 17, "averageNs": 741324.7, "durationRatio": 0.97}, {"rank": 3, "name": "aclrtSynchronizeDeviceWithTimeout", "totalTimeNs": 12168510, "totalCount": 16, "averageNs": 760531.9, "durationRatio": 0.94}, {"rank": 3, "name": "aclnnInplaceFillScalar", "totalTimeNs": 11572030, "totalCount": 81, "averageNs": 142864.6, "durationRatio": 0.89}, {"rank": 3, "name": "ReshapeAndCacheOperation::Execute", "totalTimeNs": 10002310, "totalCount": 128, "averageNs": 78143.0, "durationRatio": 0.77}, {"rank": 3, "name": "aclnnMm", "totalTimeNs": 9908740, "totalCount": 512, "averageNs": 19353.0, "durationRatio": 0.76}, {"rank": 3, "name": "Launch_Ffts", "totalTimeNs": 9243380, "totalCount": 275, "averageNs": 33612.3, "durationRatio": 0.71}, {"rank": 3, "name": "aclnnAddRmsNorm", "totalTimeNs": 7211280, "totalCount": 256, "averageNs": 28169.1, "durationRatio": 0.56}, {"rank": 3, "name": "Slice_Tiling", "totalTimeNs": 6457320, "totalCount": 322, "averageNs": 20053.8, "durationRatio": 0.5}, {"rank": 3, "name": "ReshapeAndCacheOperation::Setup", "totalTimeNs": 6047840, "totalCount": 128, "averageNs": 47248.8, "durationRatio": 0.47}, {"rank": 3, "name": "aclrtStreamWaitEvent", "totalTimeNs": 5991070, "totalCount": 567, "averageNs": 10566.3, "durationRatio": 0.46}, {"rank": 3, "name": "aclnnNeg", "totalTimeNs": 4899450, "totalCount": 17, "averageNs": 288202.9, "durationRatio": 0.38}, {"rank": 3, "name": "aclnnCumsumGetWorkspaceSize", "totalTimeNs": 4261580, "totalCount": 2, "averageNs": 2130790.0, "durationRatio": 0.33}, {"rank": 3, "name": "aclnnInplaceFillScalarGetWorkspaceSize", "totalTimeNs": 4090920, "totalCount": 22, "averageNs": 185950.9, "durationRatio": 0.32}, {"rank": 3, "name": "aclnnSubs", "totalTimeNs": 3872540, "totalCount": 17, "averageNs": 227796.5, "durationRatio": 0.3}, {"rank": 3, "name": "aclnnInplaceMaskedFillScalar", "totalTimeNs": 3858440, "totalCount": 70, "averageNs": 55120.6, "durationRatio": 0.3}, {"rank": 3, "name": "aclnnSoftmaxGetWorkspaceSize", "totalTimeNs": 3479020, "totalCount": 3, "averageNs": 1159673.3, "durationRatio": 0.27}, {"rank": 3, "name": "aclnnCumsum", "totalTimeNs": 3288630, "totalCount": 17, "averageNs": 193448.8, "durationRatio": 0.25}, {"rank": 3, "name": "aclnnRsubs", "totalTimeNs": 3207120, "totalCount": 34, "averageNs": 94327.1, "durationRatio": 0.25}, {"rank": 3, "name": "aclnnInplaceAdds", "totalTimeNs": 3171650, "totalCount": 17, "averageNs": 186567.6, "durationRatio": 0.24}, {"rank": 3, "name": "aclnnGeScalar", "totalTimeNs": 3111040, "totalCount": 19, "averageNs": 163738.9, "durationRatio": 0.24}, {"rank": 3, "name": "aclrtDestroyEvent", "totalTimeNs": 2972950, "totalCount": 556, "averageNs": 5347.0, "durationRatio": 0.23}, {"rank": 3, "name": "aclnnLtTensor", "totalTimeNs": 2920640, "totalCount": 34, "averageNs": 85901.2, "durationRatio": 0.23}, {"rank": 3, "name": "aclnnAddRmsNormGetWorkspaceSize", "totalTimeNs": 2913270, "totalCount": 256, "averageNs": 11380.0, "durationRatio": 0.22}, {"rank": 3, "name": "aclnnEqScalar", "totalTimeNs": 2910860, "totalCount": 17, "averageNs": 171227.1, "durationRatio": 0.22}, {"rank": 3, "name": "aclnnInnerApplyRotaryPosEmb", "totalTimeNs": 2852430, "totalCount": 128, "averageNs": 22284.6, "durationRatio": 0.22}, {"rank": 3, "name": "aclnnLeTensor", "totalTimeNs": 2681540, "totalCount": 17, "averageNs": 157737.6, "durationRatio": 0.21}, {"rank": 3, "name": "aclnnSwiGlu", "totalTimeNs": 2579130, "totalCount": 128, "averageNs": 20149.5, "durationRatio": 0.2}, {"rank": 3, "name": "aclnnEqScalarGetWorkspaceSize", "totalTimeNs": 2568260, "totalCount": 2, "averageNs": 1284130.0, "durationRatio": 0.2}, {"rank": 3, "name": "aclnnLeTensorGetWorkspaceSize", "totalTimeNs": 2436910, "totalCount": 2, "averageNs": 1218455.0, "durationRatio": 0.19}, {"rank": 3, "name": "Fill_Tiling", "totalTimeNs": 2386470, "totalCount": 22, "averageNs": 108475.9, "durationRatio": 0.18}, {"rank": 3, "name": "aclrtStreamGetId", "totalTimeNs": 2368750, "totalCount": 2696, "averageNs": 878.6, "durationRatio": 0.18}, {"rank": 3, "name": "aclnnSortGetWorkspaceSize", "totalTimeNs": 2334550, "totalCount": 2, "averageNs": 1167275.0, "durationRatio": 0.18}, {"rank": 3, "name": "aclrtMemcpyAsync", "totalTimeNs": 2189400, "totalCount": 145, "averageNs": 15099.3, "durationRatio": 0.17}, {"rank": 3, "name": "aclnnArgMaxGetWorkspaceSize", "totalTimeNs": 2186560, "totalCount": 2, "averageNs": 1093280.0, "durationRatio": 0.17}, {"rank": 3, "name": "aclnnCast", "totalTimeNs": 1944210, "totalCount": 51, "averageNs": 38
  1.11	121.8, "durationRatio": 0.15}, {"rank": 3, "name": "aclnnGather", "totalTimeNs": 1859940, "totalCount": 34, "averageNs": 54704.1, "durationRatio": 0.14}, {"rank": 3, "name": "aclnnIndex", "totalTimeNs": 1742520, "totalCount": 17, "averageNs": 102501.2, "durationRatio": 0.13}, {"rank": 3, "name": "aclrtCreateEventExWithFlag", "totalTimeNs": 1741820, "totalCount": 568, "averageNs": 3066.6, "durationRatio": 0.13}, {"rank": 3, "name": "aclnnGatherGetWorkspaceSize", "totalTimeNs": 1688690, "totalCount": 3, "averageNs": 562896.7, "durationRatio": 0.13}, {"rank": 3, "name": "aclnnInnerApplyRotaryPosEmbGetWorkspaceSize", "totalTimeNs": 1611250, "totalCount": 128, "averageNs": 12587.9, "durationRatio": 0.12}, {"rank": 3, "name": "aclnnRsubsGetWorkspaceSize", "totalTimeNs": 1574420, "totalCount": 4, "averageNs": 393605.0, "durationRatio": 0.12}, {"rank": 3, "name": "aclnnAddsGetWorkspaceSize", "totalTimeNs": 1536170, "totalCount": 2, "averageNs": 768085.0, "durationRatio": 0.12}, {"rank": 3, "name": "aclnnLtTensorGetWorkspaceSize", "totalTimeNs": 1507700, "totalCount": 3, "averageNs": 502566.7, "durationRatio": 0.12}, {"rank": 3, "name": "aclnnLogGetWorkspaceSize", "totalTimeNs": 1493930, "totalCount": 2, "averageNs": 746965.0, "durationRatio": 0.12}, {"rank": 3, "name": "aclnnGeScalarGetWorkspaceSize", "totalTimeNs": 1491980, "totalCount": 2, "averageNs": 745990.0, "durationRatio": 0.11}, {"rank": 3, "name": "aclnnSubsGetWorkspaceSize", "totalTimeNs": 1476420, "totalCount": 2, "averageNs": 738210.0, "durationRatio": 0.11}, {"rank": 3, "name": "aclnnInplaceMaskedFillScalarGetWorkspaceSize", "totalTimeNs": 1439930, "totalCount": 7, "averageNs": 205704.3, "durationRatio": 0.11}, {"rank": 3, "name": "aclnnSwiGluGetWorkspaceSize", "totalTimeNs": 1425560, "totalCount": 128, "averageNs": 11137.2, "durationRatio": 0.11}, {"rank": 3, "name": "aclnnSoftmax", "totalTimeNs": 1422640, "totalCount": 34, "averageNs": 41842.4, "durationRatio": 0.11}, {"rank": 3, "name": "aclnnNegGetWorkspaceSize", "totalTimeNs": 1387900, "totalCount": 2, "averageNs": 693950.0, "durationRatio": 0.11}, {"rank": 3, "name": "aclnnSort", "totalTimeNs": 1336880, "totalCount": 17, "averageNs": 78640.0, "durationRatio": 0.1}, {"rank": 3, "name": "aclnnArgMax", "totalTimeNs": 1317630, "totalCount": 17, "averageNs": 77507.6, "durationRatio": 0.1}, {"rank": 3, "name": "aclrtQueryEventStatus", "totalTimeNs": 1296850, "totalCount": 264, "averageNs": 4912.3, "durationRatio": 0.1}, {"rank": 3, "name": "aclnnInnerFusedInferAttentionScoreTiling", "totalTimeNs": 1249920, "totalCount": 17, "averageNs": 73524.7, "durationRatio": 0.1}, {"rank": 3, "name": "aclnnInplaceDiv", "totalTimeNs": 1172140, "totalCount": 34, "averageNs": 34474.7, "durationRatio": 0.09}, {"rank": 3, "name": "aclrtSetDevice", "totalTimeNs": 1072830, "totalCount": 9, "averageNs": 119203.3, "durationRatio": 0.08}, {"rank": 3, "name": "aclnnInplaceUniform", "totalTimeNs": 1010330, "totalCount": 17, "averageNs": 59431.2, "durationRatio": 0.08}, {"rank": 3, "name": "aclnnInplaceUniformGetWorkspaceSize", "totalTimeNs": 998930, "totalCount": 17, "averageNs": 58760.6, "durationRatio": 0.08}, {"rank": 3, "name": "aclnnIndexSelect", "totalTimeNs": 892820, "totalCount": 34, "averageNs": 26259.4, "durationRatio": 0.07}, {"rank": 3, "name": "aclnnInplaceLog", "totalTimeNs": 836210, "totalCount": 17, "averageNs": 49188.8, "durationRatio": 0.06}, {"rank": 3, "name": "aclmdlRIExecuteAsync", "totalTimeNs": 733680, "totalCount": 15, "averageNs": 48912.0, "durationRatio": 0.06}, {"rank": 3, "name": "aclnnMatmulGetWorkspaceSize", "totalTimeNs": 684020, "totalCount": 2, "averageNs": 342010.0, "durationRatio": 0.05}, {"rank": 3, "name": "aclrtGetCurrentContext", "totalTimeNs": 640940, "totalCount": 275, "averageNs": 2330.7, "durationRatio": 0.05}, {"rank": 3, "name": "aclnnMatmul", "totalTimeNs": 626830, "totalCount": 17, "averageNs": 36872.4, "durationRatio": 0.05}, {"rank": 3, "name": "aclnnRepeat", "totalTimeNs": 623880, "totalCount": 34, "averageNs": 18349.4, "durationRatio": 0.05}, {"rank": 3, "name": "aclnnCastGetWorkspaceSize", "totalTimeNs": 509750, "totalCount": 6, "averageNs": 84958.3, "durationRatio": 0.04}, {"rank": 3, "name": "aclnnInplaceMuls", "totalTimeNs": 493550, "totalCount": 17, "averageNs": 29032.4, "durationRatio": 0.04}, {"rank": 3, "name": "Cast_Tiling", "totalTimeNs": 446960, "totalCount": 19, "averageNs": 23524.2, "durationRatio": 0.03}, {"rank": 3, "name": "ReduceSum_Tiling", "totalTimeNs": 434460, "totalCount": 4, "averageNs": 108615.0, "durationRatio": 0.03}, {"rank": 3, "name": "MatMulV2_Tiling", "totalTimeNs": 393740, "totalCount": 4, "averageNs": 98435.0, "durationRatio": 0.03}, {"rank": 3, "name": "aclrtNotifyGetExportKey", "totalTimeNs": 356340, "totalCount": 12, "averageNs": 29695.0, "durationRatio": 0.03}, {"rank": 3, "name": "aclrtIpcMemSetImportPid", "totalTimeNs": 316040, "totalCount": 27, "averageNs": 11705.2, "durationRatio": 0.02}, {"rank": 3, "name": "aclrtMallocHostWithCfg", 
  1.12	"totalTimeNs": 272110, "totalCount": 27, "averageNs": 10078.1, "durationRatio": 0.02}, {"rank": 3, "name": "aclrtMemcpyAsyncWithCondition", "totalTimeNs": 269460, "totalCount": 17, "averageNs": 15850.6, "durationRatio": 0.02}, {"rank": 3, "name": "Sub_Tiling", "totalTimeNs": 249230, "totalCount": 6, "averageNs": 41538.3, "durationRatio": 0.02}, {"rank": 3, "name": "MaskedFill_Tiling", "totalTimeNs": 175660, "totalCount": 7, "averageNs": 25094.3, "durationRatio": 0.01}, {"rank": 3, "name": "GatherElementsV2_Tiling", "totalTimeNs": 174880, "totalCount": 6, "averageNs": 29146.7, "durationRatio": 0.01}, {"rank": 3, "name": "aclnnDivGetWorkspaceSize", "totalTimeNs": 146910, "totalCount": 4, "averageNs": 36727.5, "durationRatio": 0.01}, {"rank": 3, "name": "aclrtFreeHost", "totalTimeNs": 140470, "totalCount": 27, "averageNs": 5202.6, "durationRatio": 0.01}, {"rank": 3, "name": "SoftmaxV2_Tiling", "totalTimeNs": 133170, "totalCount": 6, "averageNs": 22195.0, "durationRatio": 0.01}, {"rank": 3, "name": "Less_Tiling", "totalTimeNs": 130410, "totalCount": 3, "averageNs": 43470.0, "durationRatio": 0.01}, {"rank": 3, "name": "aclrtGetNotifyId", "totalTimeNs": 128760, "totalCount": 72, "averageNs": 1788.3, "durationRatio": 0.01}, {"rank": 3, "name": "RealDiv_Tiling", "totalTimeNs": 114970, "totalCount": 4, "averageNs": 28742.5, "durationRatio": 0.01}, {"rank": 3, "name": "aclnnSub", "totalTimeNs": 110290, "totalCount": 2, "averageNs": 55145.0, "durationRatio": 0.01}, {"rank": 3, "name": "Transpose_Tiling", "totalTimeNs": 108490, "totalCount": 2, "averageNs": 54245.0, "durationRatio": 0.01}, {"rank": 3, "name": "aclnnMuls", "totalTimeNs": 105590, "totalCount": 2, "averageNs": 52795.0, "durationRatio": 0.01}, {"rank": 3, "name": "Equal_Tiling", "totalTimeNs": 89660, "totalCount": 2, "averageNs": 44830.0, "durationRatio": 0.01}, {"rank": 3, "name": "aclnnLtScalar", "totalTimeNs": 86750, "totalCount": 2, "averageNs": 43375.0, "durationRatio": 0.01}, {"rank": 3, "name": "Add_Tiling", "totalTimeNs": 78420, "totalCount": 2, "averageNs": 39210.0, "durationRatio": 0.01}, {"rank": 3, "name": "LessEqual_Tiling", "totalTimeNs": 67380, "totalCount": 2, "averageNs": 33690.0, "durationRatio": 0.01}, {"rank": 3, "name": "aclrtResetDevice", "totalTimeNs": 66320, "totalCount": 9, "averageNs": 7368.9, "durationRatio": 0.01}, {"rank": 3, "name": "GreaterEqual_Tiling", "totalTimeNs": 64230, "totalCount": 2, "averageNs": 32115.0, "durationRatio": 0.0}, {"rank": 3, "name": "aclnnInplaceMulsGetWorkspaceSize", "totalTimeNs": 62420, "totalCount": 2, "averageNs": 31210.0, "durationRatio": 0.0}, {"rank": 3, "name": "Neg_Tiling", "totalTimeNs": 54730, "totalCount": 2, "averageNs": 27365.0, "durationRatio": 0.0}, {"rank": 3, "name": "BroadcastTo_Tiling", "totalTimeNs": 54070, "totalCount": 1, "averageNs": 54070.0, "durationRatio": 0.0}, {"rank": 3, "name": "aclnnMul", "totalTimeNs": 50310, "totalCount": 2, "averageNs": 25155.0, "durationRatio": 0.0}, {"rank": 3, "name": "Index_Tiling", "totalTimeNs": 47990, "totalCount": 4, "averageNs": 11997.5, "durationRatio": 0.0}, {"rank": 3, "name": "Sort_Tiling", "totalTimeNs": 43260, "totalCount": 4, "averageNs": 10815.0, "durationRatio": 0.0}, {"rank": 3, "name": "Mul_Tiling", "totalTimeNs": 39090, "totalCount": 2, "averageNs": 19545.0, "durationRatio": 0.0}, {"rank": 3, "name": "aclnnBitwiseAndTensor", "totalTimeNs": 34360, "totalCount": 2, "averageNs": 17180.0, "durationRatio": 0.0}, {"rank": 3, "name": "aclnnEmbedding", "totalTimeNs": 29800, "totalCount": 2, "averageNs": 14900.0, "durationRatio": 0.0}, {"rank": 3, "name": "aclrtGetResInCurrentThread", "totalTimeNs": 29340, "totalCount": 2, "averageNs": 14670.0, "durationRatio": 0.0}, {"rank": 3, "name": "Log_Tiling", "totalTimeNs": 25320, "totalCount": 2, "averageNs": 12660.0, "durationRatio": 0.0}, {"rank": 3, "name": "aclnnBitwiseNot", "totalTimeNs": 24630, "totalCount": 2, "averageNs": 12315.0, "durationRatio": 0.0}, {"rank": 3, "name": "ArgMaxV2_Tiling", "totalTimeNs": 23060, "totalCount": 2, "averageNs": 11530.0, "durationRatio": 0.0}, {"rank": 3, "name": "ViewCopy_Tiling", "totalTimeNs": 14910, "totalCount": 1, "averageNs": 14910.0, "durationRatio": 0.0}, {"rank": 3, "name": "Cumsum_Tiling", "totalTimeNs": 12710, "totalCount": 2, "averageNs": 6355.0, "durationRatio": 0.0}], "row_count": 491, "truncated": false, "columns": ["rank", "name", "totalTimeNs", "totalCount", "averageNs", "durationRatio"], "summary": null, "csv_export": null}
AI: CANN API 数据出现关键分叉（Rank1 的 `aclrtSynchronizeEvent` 总量仅 19.6ms，其余 Rank 286–330ms）。继续查 slow_rank 明细与 hccl 按 Rank 统计。[{'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/workspace/user_data/analysis/slow_rank/cluster_analysis_output/cluster_analysis.db', 'query': 'SELECT * FROM SlowOpStats LIMIT 30'}, 'id': 'call_00_5IxzU49RflLAbuhwlrhr9191', 'type': 'tool_call'}, {'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/tmp/cluster_analysis/hccl_sum/cluster_analysis_output/cluster_analysis.db', 'query': "SELECT sql FROM sqlite_master WHERE type='table'"}, 'id': 'call_01_lIZP2oinh6fuqRIQRKqi0297', 'type': 'tool_call'}]
Tool: [{"type": "text", "text": "{\"rows\": [{\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_0_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760018606020, \"Count\": 4, \"MeanNs\": 6722.914, \"StdNs\": 4465.384870553564, \"MinNs\": 24.94, \"Q1Ns\": 6697.285, \"MedianNs\": 8943.608499999998, \"Q3Ns\": 8969.2375, \"MaxNs\": 8979.499, \"SumNs\": 26891.656}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_1_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760031202400, \"Count\": 4, \"MeanNs\": 9359.849250000001, \"StdNs\": 6223.579890895667, \"MinNs\": 24.48, \"Q1Ns\": 9358.02675, \"MedianNs\": 12470.1845, \"Q3Ns\": 12472.007, \"MaxNs\": 12474.548, \"SumNs\": 37439.397000000004}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_2_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760035102460, \"Count\": 4, \"MeanNs\": 2790.3015, \"StdNs\": 1844.2042289517901, \"MinNs\": 24.0, \"Q1Ns\": 2786.9700000000003, \"MedianNs\": 3710.416, \"Q3Ns\": 3713.7475, \"MaxNs\": 3716.374, \"SumNs\": 11161.206}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_3_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760045788820, \"Count\": 4, \"MeanNs\": 7934.15725, \"StdNs\": 5273.398511500743, \"MinNs\": 24.06, \"Q1Ns\": 7932.72, \"MedianNs\": 10569.5555, \"Q3Ns\": 10570.992750000001, \"MaxNs\": 10573.458, \"SumNs\": 31736.629}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_4_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760048527540, \"Count\": 4, \"MeanNs\": 1918.25675, \"StdNs\": 1262.600524707472, \"MinNs\": 24.36, \"Q1Ns\": 1915.7399999999998, \"MedianNs\": 2548.0555, \"Q3Ns\": 2550.57225, \"MaxNs\": 2552.556, \"SumNs\": 7673.027}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_5_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760052343540, \"Count\": 4, \"MeanNs\": 2780.7209999999995, \"StdNs\": 1837.3621301757582, \"MinNs\": 24.68, \"Q1Ns\": 2779.355, \"MedianNs\": 3697.765, \"Q3Ns\": 3699.131, \"MaxNs\": 3702.674, \"SumNs\": 11122.883999999998}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_6_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760054104960, \"Count\": 4, \"MeanNs\": 1182.5975, \"StdNs\": 772.3434809381554, \"MinNs\": 24.1, \"Q1Ns\": 1179.58, \"MedianNs\": 1565.806, \"Q3Ns\": 1568.8235, \"MaxNs\": 1574.678, \"SumNs\": 4730.39}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_7_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760059532300, \"Count\": 4, \"MeanNs\": 3992.7644999999998, \"StdNs\": 2645.509935289036, \"MinNs\": 24.5, \"Q1Ns\": 3991.5245, \"MedianNs\": 5314.963, \"Q3Ns\": 5316.203, \"MaxNs\": 5316.632, \"SumNs\": 15971.057999999999}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_8_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760060210320, \"Count\": 4, \"MeanNs\": 373.20500000000004, \"StdNs\": 232.5300551900048, \"MinNs\": 24.44, \"Q1Ns\": 370.685, \"MedianNs\": 487.44500000000005, \"Q3Ns\": 489.96500000000003, \"MaxNs\": 493.49, \"SumNs\": 1492.8200000000002}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_9_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760061237700, \"Count\": 4, \"MeanNs\": 689.46725, \"StdNs\": 443.67288135695577, \"MinNs\": 24.0, \"Q1Ns\": 684.7485, \"MedianNs\": 908.3345, \"Q3Ns\": 913.05325, \"MaxNs\": 917.2, \"SumNs\": 2757.869}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_10_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760061857320, \"Count\": 4, \"MeanNs\": 328.95775000000003, \"StdNs\": 202.90584697731933, \"MinNs\": 24.6, \"Q1Ns\": 328.53, \"MedianNs\": 430.0615, \"Q3Ns\": 430.48925, \"MaxNs\": 431.108, \"SumNs\": 1315.8310000000001}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_11_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760063958120, \"Count\": 4, \"MeanNs\": 1494.06925, \"StdNs\": 979.8208892212139, \"MinNs\": 24.34, \"Q1Ns\": 1492.5685, \"MedianNs\": 1982.959, \"Q3Ns\": 1984.45975, \"MaxNs\": 1986.019, \"SumNs\": 5976.277}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_12_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760064659360, \"Count\": 4, \"MeanNs\": 388.79025, \"StdNs\": 242.96946902903252, \"MinNs\": 24.34, \"Q1Ns\": 387.775, \"MedianNs\": 509.56550000000004, \"Q3Ns\": 510.58075, \"MaxNs\": 511.69, \"SumNs\": 1555.161}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_13_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760067871500, \"Count\": 4, \"MeanNs\": 2330.248, \"StdNs\": 1537.4860377824575, \"MinNs\": 24.02, \"Q1Ns\": 2328.74, \"MedianNs\": 3098.255, \"Q3Ns\": 3099.763, \"MaxNs\": 3100.462, \"SumNs\": 9320.992}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_14_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760070653980, \"Count\": 4, \"MeanNs\": 1948.3125, \"StdNs\": 1282.8484516441008, \"MinNs\": 24.04, \"Q1Ns\": 1947.7982499999998, \"MedianNs\": 2589.4049999999997, \"Q3Ns\": 2589.91925, \"MaxNs\": 2590.4, \"SumNs\": 7793.25}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_15_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760071727800, \"Count\": 4, \"MeanNs\": 726.484, \"StdNs\": 468.08917676941286, \"MinNs\": 24.36, \"Q1Ns\": 723.9000000000001, \"MedianNs\": 959.4195, \"Q3Ns\": 962.0035, \"MaxNs\": 962.737, \"SumNs\": 2905.936}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_16_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760072399020, \"Count\": 4, \"MeanNs\": 366.10949999999997, \"StdNs\": 227.73974535347725, \"MinNs\": 24.52, \"Q1Ns\": 363.895, \"MedianNs\": 478.42449999999997, \"Q3Ns\": 480.639, \"MaxNs\": 483.069, \"SumNs\": 1464.4379999999999}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_17_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760075541200, \"Count\": 4, \"MeanNs\": 2275.5860000000002, \"StdNs\": 1500.859645503869, \"MinNs\": 24.3, \"Q1Ns\": 2273.085, \"MedianNs\": 3024.4615, \"Q3Ns\": 3026.9625, \"MaxNs\": 3029.121, \"SumNs\": 9102.344000000001}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_18_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760076864360, \"Count\": 4, \"MeanNs\": 853.0287500000001, \"StdNs\": 552.6910695215879, \"MinNs\": 24.0, \"Q1Ns\": 850.815, \"MedianNs\": 1127.901, \"Q3Ns\": 1130.1147500000002, \"MaxNs\": 1132.313, \"SumNs\": 3412.1150000000002}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_19_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760079819280, \"Count\": 4, \"MeanNs\": 2137.245, \"StdNs\": 1408.166819247871, \"MinNs\": 25.0, \"Q1Ns\": 2133.973, \"MedianNs\": 2839.8999999999996, \"Q3Ns\": 2843.1719999999996, \"MaxNs\": 2844.18, \"SumNs\": 8548.98}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_20_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760080417840, \"Count\": 4, \"MeanNs\": 307.61725, \"StdNs\": 189.05683822134017, \"MinNs\": 24.04, \"Q1Ns\": 306.115, \"MedianNs\": 401.57399999999996, \"Q3Ns\": 403.07624999999996, \"MaxNs\": 403.281, \"SumNs\": 1230.469}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_21_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760083575280, \"Count\": 4, \"MeanNs\": 2289.4612500000003, \"StdNs\": 1510.4441924611392, \"MinNs\": 23.8, \"Q1Ns\": 2286.11875, \"MedianNs\": 3043.2825000000003, \"Q3Ns\": 3046.625, \"MaxNs\": 3047.48, \"SumNs\": 9157.845000000001}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_22_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760084765580, \"Count\": 4, \"MeanNs\": 755.84, \"StdNs\": 487.67051007826996, \"MinNs\": 24.34, \"Q1Ns\": 753.97, \"MedianNs\": 998.56, \"Q3Ns\": 1000.4300000000001, \"MaxNs\": 1001.9, \"SumNs\": 3023.36}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_23_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760088178680, \"Count\": 4, \"MeanNs\": 2479.82375, \"StdNs\": 1637.0837634106927, \"MinNs\": 24.2, \"Q1Ns\": 2477.67875, \"MedianNs\": 3297.5175, \"Q3Ns\": 3299.6625000000004, \"MaxNs\": 3300.06, \"SumNs\": 9919.295}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_24_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760088714680, \"Count\": 4, \"MeanNs\": 265.1055, \"StdNs\": 160.52416540093478, \"MinNs\": 24.32, \"Q1Ns\": 264.84125, \"MedianNs\": 345.0875, \"Q3Ns\": 345.35175000000004, \"MaxNs\": 345.927, \"SumNs\": 1060.422}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_25_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760089817480, \"Count\": 4, \"MeanNs\": 748.65475, \"StdNs\": 483.090178431436, \"MinNs\": 24.02, \"Q1Ns\": 748.24925, \"MedianNs\": 989.7995000000001, \"Q3Ns\": 990.205, \"MaxNs\": 991.0, \"SumNs\": 2994.619}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_26_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760090395800, \"Count\": 4, \"MeanNs\": 293.45675, \"StdNs\": 179.6003584468862, \"MinNs\": 24.06, \"Q1Ns\": 292.71, \"MedianNs\": 382.61400000000003, \"Q3Ns\": 383.36075, \"MaxNs\": 384.539, \"SumNs\": 1173.827}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_27_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760092432220, \"Count\": 4, \"MeanNs\": 1449.0527499999998, \"StdNs\": 949.83571092457, \"MinNs\": 24.3, \"Q1Ns\": 1448.0249999999999, \"MedianNs\": 1923.4389999999999, \"Q3Ns\": 1924.46675, \"MaxNs\": 1925.033, \"SumNs\": 5796.210999999999}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_28_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760093264900, \"Count\": 4, \"MeanNs\": 487.01925000000006, \"StdNs\": 308.46637058343435, \"MinNs\": 24.32, \"Q1Ns\": 486.65975000000003, \"MedianNs\": 641.0685000000001, \"Q3Ns\": 641.428, \"MaxNs\": 641.62, \"SumNs\": 1948.0770000000002}, {\"SlowRank\": \"1\", \"OpName\": \"hcom_allReduce__503_29_1\", \"GroupName\": \"group_name_3\", \"Timestamp\": 1767864760095650840, \"Count\": 4, \"MeanNs\": 1709.8184999999999, \"StdNs\": 1123.9328808208847, \"MinNs\": 23.92, \"Q1Ns\": 1708.75, \"MedianNs\": 2271.1440000000002, \"Q3Ns\": 2272.2124999999996, \"MaxNs\": 2273.066, \"SumNs\": 6839.273999999999}], \"row_count\": 30, \"truncated\": false, \"columns\": [\"SlowRank\", \"OpName\", \"GroupName\", \"Timestamp\", \"Count\", \"MeanNs\", \"StdNs\", \"MinNs\", \"Q1Ns\", \"MedianNs\", \"Q3Ns\", \"MaxNs\", \"SumNs\"], \"summary\": null, \"csv_export\": null}", "id": "lc_81916fce-6e00-4412-9fab-891710c3482e"}]
Tool: [{"type": "text", "text": "{\"rows\": [{\"sql\": \"CREATE TABLE \\\"HcclAllRankStats\\\" (\\n\\\"OpType\\\" TEXT,\\n  \\\"Count\\\" INTEGER,\\n  \\\"MeanNs\\\" REAL,\\n  \\\"StdNs\\\" REAL,\\n  \\\"MinNs\\\" REAL,\\n  \\\"Q1Ns\\\" REAL,\\n  \\\"MedianNs\\\" REAL,\\n  \\\"Q3Ns\\\" REAL,\\n  \\\"MaxNs\\\" REAL,\\n  \\\"SumNs\\\" REAL\\n)\"}, {\"sql\": \"CREATE TABLE \\\"HcclPerRankStats\\\" (\\n\\\"OpType\\\" TEXT,\\n  \\\"Count\\\" INTEGER,\\n  \\\"MeanNs\\\" REAL,\\n  \\\"StdNs\\\" REAL,\\n  \\\"MinNs\\\" REAL,\\n  \\\"Q1Ns\\\" REAL,\\n  \\\"MedianNs\\\" REAL,\\n  \\\"Q3Ns\\\" REAL,\\n  \\\"MaxNs\\\" REAL,\\n  \\\"SumNs\\\" REAL,\\n  \\\"Rank\\\" INTEGER\\n)\"}, {\"sql\": \"CREATE TABLE \\\"HcclTopOpStats\\\" (\\n\\\"OpName\\\" TEXT,\\n  \\\"Count\\\" INTEGER,\\n  \\\"MeanNs\\\" REAL,\\n  \\\"StdNs\\\" REAL,\\n  \\\"MinNs\\\" REAL,\\n  \\\"Q1Ns\\\" REAL,\\n  \\\"MedianNs\\\" REAL,\\n  \\\"Q3Ns\\\" REAL,\\n  \\\"MaxNs\\\" REAL,\\n  \\\"SumNs\\\" REAL,\\n  \\\"MinRank\\\" INTEGER,\\n  \\\"MaxRank\\\" INTEGER\\n)\"}, {\"sql\": \"CREATE TABLE \\\"HcclGroupNameMap\\\" (\\n\\\"GroupName\\\" TEXT,\\n  \\\"GroupId\\\" TEXT,\\n  \\\"Ranks\\\" TEXT\\n)\"}], \"row_count\": 4, \"truncated\": false, \"columns\": [\"sql\"], \"summary\": null, \"csv_export\": null}", "id": "lc_10f25a48-275f-483d-89bb-90622fb231ef"}]
AI: [{'name': 'msprof-mcp_execute_sql', 'args': {'db_path': '/tmp/cluster_analysis/hccl_sum/cluster_analysis_output/cluster_analysis.db', 'query': 'SELECT OpType, Count, MeanNs, SumNs, Rank FROM HcclPerRankStats ORDER BY Rank, SumNs DESC'}, 'id': 'call_00_4supIk6PpNZPwCZfZLOa7711', 'type': 'tool_call'}]
Tool: [{"type": "text", "text": "{\"rows\": [{\"OpType\": \"hcom_allReduce_\", \"Count\": 258, \"MeanNs\": 1650770.3100775194, \"SumNs\": 425898740.0, \"Rank\": 0}, {\"OpType\": \"hcom_allGather_\", \"Count\": 17, \"MeanNs\": 1294378.8235294118, \"SumNs\": 22004440.0, \"Rank\": 0}, {\"OpType\": \"hcom_allReduce_\", \"Count\": 258, \"MeanNs\": 776005.8914728683, \"SumNs\": 200209520.0, \"Rank\": 1}, {\"OpType\": \"hcom_allGather_\", \"Count\": 17, \"MeanNs\": 211929.41176470587, \"SumNs\": 3602800.0, \"Rank\": 1}, {\"OpType\": \"hcom_allReduce_\", \"Count\": 258, \"MeanNs\": 895574.7984496125, \"SumNs\": 231058298.0, \"Rank\": 2}, {\"OpType\": \"hcom_allGather_\", \"Count\": 17, \"MeanNs\": 982907.7647058824, \"SumNs\": 16709432.0, \"Rank\": 2}, {\"OpType\": \"hcom_allReduce_\", \"Count\": 258, \"MeanNs\": 1645715.1550387596, \"SumNs\": 424594510.0, \"Rank\": 3}, {\"OpType\": \"hcom_allGather_\", \"Count\": 17, \"MeanNs\": 1199256.4117647058, \"SumNs\": 20387359.0, \"Rank\": 3}], \"row_count\": 8, \"truncated\": false, \"columns\": [\"OpType\", \"Count\", \"MeanNs\", \"SumNs\", \"Rank\"], \"summary\": null, \"csv_export\": null}", "id": "lc_cf6883a6-9a37-414a-8448-06fba27f24d7"}]
AI: 核心证据已清晰。现在用 skill 自带的对比脚本做 Rank 级微观比对（慢卡 vs 基准卡）。[{'name': 'ls', 'args': {'path': '/workspace/.msagent/skills/ascend-cluster-fast-slow-rank-detector/scripts'}, 'id': 'call_00_xMnHHGQCVqzeMEL5BFm95013', 'type': 'tool_call'}]
Tool: ['/workspace/.msagent/skills/ascend-cluster-fast-slow-rank-detector/scripts/__pycache__/', '/workspace/.msagent/skills/ascend-cluster-fast-slow-rank-detector/scripts/compare_api_stats.py', '/workspace/.msagent/skills/ascend-cluster-fast-slow-rank-detector/scripts/compare_op_stats.py', '/workspace/.msagent/skills/ascend-cluster-fast-slow-rank-detector/scripts/rank_data_finder.py']
AI: [{'name': 'execute', 'args': {'command': 'cd /tmp && python /workspace/.msagent/skills/ascend-cluster-fast-slow-rank-detector/scripts/compare_api_stats.py /workspace/user_data 1 0 --top 15 2>&1 | head -60', 'timeout': 300}, 'id': 'call_00_ET_2zj45jGfWuTDRTC8EKTL4973', 'type': 'tool_call'}]
Tool: === 慢卡 vs 快卡 API 统计对比 ===
慢卡 Rank 1，快卡 Rank 0，数据来源: text
差异最大的前 15 个 API（按 |diff_total_time_us| 排序）：

1. aclrtSynchronizeEvent
   慢卡: total_time_us=19558, count=17
   快卡: total_time_us=329918, count=17
   diff_total_time_us=-310360, ratio(slow/fast)=0.05928115315012974, avg_time_diff_us=-18256.47

2. aclnnInnerFusedInferAttentionScore
   慢卡: total_time_us=130070, count=1088
   快卡: total_time_us=83720, count=1088
   diff_total_time_us=46350, ratio(slow/fast)=1.5536245902430423, avg_time_diff_us=42.60

3. hcom_allGather_
   慢卡: total_time_us=55670, count=17
   快卡: total_time_us=79987, count=17
   diff_total_time_us=-24317, ratio(slow/fast)=0.6959895882820666, avg_time_diff_us=-1430.40

4. aclrtRecordEvent
   慢卡: total_time_us=62734, count=1819
   快卡: total_time_us=39734, count=1819
   diff_total_time_us=23001, ratio(slow/fast)=1.5788650444242938, avg_time_diff_us=12.64

5. hcom_allReduce_
   慢卡: total_time_us=70109, count=258
   快卡: total_time_us=89118, count=258
   diff_total_time_us=-19008, ratio(slow/fast)=0.7867043800579838, avg_time_diff_us=-73.68

6. launch
   慢卡: total_time_us=276833, count=4168
   快卡: total_time_us=259490, count=4168
   diff_total_time_us=17343, ratio(slow/fast)=1.0668366848905024, avg_time_diff_us=4.16

7. aclnnInnerFusedInferAttentionScoreGetWorkspaceSize
   慢卡: total_time_us=31586, count=1088
   快卡: total_time_us=28843, count=1088
   diff_total_time_us=2743, ratio(slow/fast)=1.0951135626891702, avg_time_diff_us=2.52

8. aclnnSoftmaxGetWorkspaceSize
   慢卡: total_time_us=6126, count=2
   快卡: total_time_us=3515, count=3
   diff_total_time_us=2610, ratio(slow/fast)=1.7425880304728525, avg_time_diff_us=1891.07

9. aclnnInplaceCopyGetWorkspaceSize
   慢卡: total_time_us=27534, count=1034
   快卡: total_time_us=30142, count=1034
   diff_total_time_us=-2608, ratio(slow/fast)=0.9134863984791909, avg_time_diff_us=-2.52

10. Launch_Ffts
   慢卡: total_time_us=8903, count=275
   快卡: total_time_us=11187, count=275
   diff_total_time_us=-2284, ratio(slow/fast)=0.7958031319190624, avg_time_diff_us=-8.31

11. aclrtDestroyEvent
   慢卡: total_time_us=1446, count=555
   快卡: total_time_us=3640, count=556
   diff_total_time_us=-2194, ratio(slow/fast)=0.39729538187691593, avg_time_diff_us=-3.94

12. aclnnIndexGetWorkspaceSize

[Command succeeded with exit code 0]
AI: API 对比确认 Rank1 的下发/同步特征。再对比 Rank2 vs Rank0，并跑算子对比确认计算均衡性。[{'name': 'execute', 'args': {'command': 'python /workspace/.msagent/skills/ascend-cluster-fast-slow-rank-detector/scripts/compare_api_stats.py /workspace/user_data 2 0 --top 10 2>&1 | head -45', 'timeout': 300}, 'id': 'call_00_KeWRdsOtWPt7J8M42TkT5362', 'type': 'tool_call'}, {'name': 'execute', 'args': {'command': 'python /workspace/.msagent/skills/ascend-cluster-fast-slow-rank-detector/scripts/compare_op_stats.py /workspace/user_data 1 0 --top 8 2>&1 | head -40', 'timeout': 300}, 'id': 'call_01_8PVVtCzpX5JNat96b02f5591', 'type': 'tool_call'}]
Tool: === 慢卡 vs 快卡 API 统计对比 ===
慢卡 Rank 2，快卡 Rank 0，数据来源: text
差异最大的前 10 个 API（按 |diff_total_time_us| 排序）：

1. aclrtSynchronizeEvent
   慢卡: total_time_us=285745, count=17
   快卡: total_time_us=329918, count=17
   diff_total_time_us=-44173, ratio(slow/fast)=0.8661086044937732, avg_time_diff_us=-2598.42

2. hcom_allGather_
   慢卡: total_time_us=102764, count=17
   快卡: total_time_us=79987, count=17
   diff_total_time_us=22778, ratio(slow/fast)=1.28476786169718, avg_time_diff_us=1339.86

3. hcom_allReduce_
   慢卡: total_time_us=70266, count=258
   快卡: total_time_us=89118, count=258
   diff_total_time_us=-18852, ratio(slow/fast)=0.7884608198791329, avg_time_diff_us=-73.07

4. launch
   慢卡: total_time_us=272947, count=4168
   快卡: total_time_us=259490, count=4168
   diff_total_time_us=13457, ratio(slow/fast)=1.0518583714822272, avg_time_diff_us=3.23

5. aclnnInnerFusedInferAttentionScore
   慢卡: total_time_us=88328, count=1088
   快卡: total_time_us=83720, count=1088
   diff_total_time_us=4608, ratio(slow/fast)=1.055038583207239, avg_time_diff_us=4.24

6. aclnnLtTensor
   慢卡: total_time_us=6832, count=34
   快卡: total_time_us=2913, count=34
   diff_total_time_us=3918, ratio(slow/fast)=2.3448717332656015, avg_time_diff_us=115.24

7. aclnnSubsGetWorkspaceSize
   慢卡: total_time_us=3853, count=2
   快卡: total_time_us=1498, count=2
   diff_total_time_us=2355, ratio(slow/fast)=2.57160783057341, avg_time_diff_us=1177.32

8. aclrtStreamGetId
   慢卡: total_time_us=2432, count=2696
   快卡: total_time_us=4090, count=4760
   diff_total_time_us=-1659, ratio(slow/fast)=0.5944934346113696, avg_time_diff_us=0.04

9. aclnnEqScalar

[Command succeeded with exit code 0]
Tool: === 慢卡 vs 快卡 算子统计对比 ===
慢卡 Rank 1，快卡 Rank 0，数据来源: text
差异最大的前 8 个算子（按 |diff_total_time_us| 排序）：

1. MatMulV2_ND_ND_FP16_FP16_false_true_all_98499
   慢卡: total_time_us=77199, count=1920
   快卡: total_time_us=77740, count=1920
   diff_total_time_us=-541, ratio(slow/fast)=0.9930467833880203, avg_time_diff_us=-0.28

2. MatMulV2_ND_ND_FP16_FP16_false_true_all_98513
   慢卡: total_time_us=134930, count=1920
   快卡: total_time_us=135395, count=1920
   diff_total_time_us=-465, ratio(slow/fast)=0.9965684028353257, avg_time_diff_us=-0.24

3. FusedInferAttentionScore_3b093497fc536d61a77a7a3293a524da_5000000000010200203
   慢卡: total_time_us=18560, count=960
   快卡: total_time_us=18140, count=960
   diff_total_time_us=420, ratio(slow/fast)=1.0231636605397467, avg_time_diff_us=0.44

4. RmsNorm_normal_all_bf16_high_performance_2
   慢卡: total_time_us=15555, count=1920
   快卡: total_time_us=15792, count=1920
   diff_total_time_us=-237, ratio(slow/fast)=0.9850088082908116, avg_time_diff_us=-0.12

5. SwiGlu_3_high_performance_27
   慢卡: total_time_us=4960, count=960
   快卡: total_time_us=5160, count=960
   diff_total_time_us=-200, ratio(slow/fast)=0.9612158516543149, avg_time_diff_us=-0.21

6. MatMulV2
   慢卡: total_time_us=33678, count=529
   快卡: total_time_us=33799, count=529
   diff_total_time_us=-121, ratio(slow/fast)=0.9964264749740818, avg_time_diff_us=-0.23

7. FusedInferAttentionScore
   慢卡: total_time_us=2236, count=128
   快卡: total_time_us=2153, count=128
   diff_total_time_us=83, ratio(slow/fast)=1.038546562389701, avg_time_diff_us=0.65

8. ApplyRotaryPosEmb_b629c03f5922badb3407bf2e66751e02_high_performance_1

[Command succeeded with exit code 0]
AI: 微观证据闭环：计算算子完全一致，CANN API 与 HCCL 差异集中在 Rank1/2。最后用 `free_analysis` 验证 Free 时间成因，并对慢卡 Rank1 目录跑 advisor。[{'name': 'execute', 'args': {'command': 'msprof-analyze cluster -m free_analysis -d /workspace/user_data -o /tmp/cluster_analysis/free_analysis --force --agent 2>&1 | tail -8', 'timeout': 600}, 'id': 'call_00_Yme1IYPdmWF5NX6AiHXH8162', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:43:15][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.
{"status": "success", "code": 0, "message": {}, "suggestion": "", "log_path": "/tmp/msprof_analyze_1787128995.log"}

[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'find /tmp/cluster_analysis/free_analysis -type f | head -20'}, 'id': 'call_00_ET_lMQg91P5cydWCYkaR4oe9519', 'type': 'tool_call'}]
Tool: <no output>
[Command succeeded with exit code 0]
AI: [{'name': 'execute', 'args': {'command': 'tail -20 /tmp/msprof_analyze_1787128995.log'}, 'id': 'call_00_yTlZYgMYxmeTL0Lvw8oI0029', 'type': 'tool_call'}]
Tool: [2026-08-19 08:43:15][INFO][cluster_analysis.py:71] Start allocate profiling data.
[2026-08-19 08:43:33][INFO][cluster_analysis.py:77] Finish allocate profiling data: 4 ranks, data type: db, profiling type: pytorch
[2026-08-19 08:43:33][INFO][analysis_facade.py:98] Recipe FreeAnalysis analysis is starting to launch.
[2026-08-19 08:43:33][INFO][context.py:35] context concurrent initialized.
[2026-08-19 08:43:33][INFO][free_analysis.py:51] Free Analysis init.
[2026-08-19 08:43:35][ERROR][context.py:106] A process in the process pool was terminated abruptly while the future was running or pending.
[2026-08-19 08:43:35][INFO][free_analysis.py:67] No free time data found.
[2026-08-19 08:43:36][INFO][analysis_facade.py:103] Recipe FreeAnalysis analysis finished.

[Command succeeded with exit code 0]
AI: free_analysis 内部进程异常，未能输出（记录为证据缺口）。对慢卡 Rank1 目录执行 advisor 获取针对性建议。[{'name': 'execute', 'args': {'command': 'msprof-analyze advisor all -d /workspace/user_data/df536040f370_654_20260108093239559_ascend_pt -o /tmp/advisor_rank1 --force --agent 2>&1 | tail -15', 'timeout': 900}, 'id': 'call_00_pHZg9VUab7oAtCCcF4Ti8983', 'type': 'tool_call'}]
Tool: =================================================================
                   >>>>>   MindStudio   <<<<<
    THE END-TO-END TOOLCHAIN TO UNLEASH HUAWEI ASCEND COMPUTE
=================================================================

[2026-08-19 08:43:43][WARNING][entrance.py:217] Security Warning: Do not run this tool as root. Running with elevated privileges may compromise system security. Use a regular user account.

Building dataset for timeline analysis:   0%|          | 0/11 [00:00<?, ?it/s]
Building dataset for timeline analysis:   9%|▉         | 1/11 [00:00<00:05,  1.92it/s]
Building dataset for timeline analysis:  36%|███▋      | 4/11 [00:00<00:00,  7.41it/s]
Building dataset for timeline analysis:  64%|██████▎   | 7/11 [00:00<00:00,  9.83it/s]
Building dataset for timeline analysis:  91%|█████████ | 10/11 [00:02<00:00,  3.39it/s]
Building dataset for timeline analysis: 100%|██████████| 11/11 [00:02<00:00,  3.61it/s]
                                                                                       

Building dataset for timeline analysis:   0%|          | 0/6 [00:00<?, ?it/s]
Building dataset for timeline analysis:  17%|█▋        | 1/6 [00:00<00:02,  1.95it/s]
                                                                                     

Scanning timeline for affinity apis:   0%|                                   | 0/34 [00:00<?, ?it/s]
Scanning timeline for affinity apis:  18%|████▊                      | 6/34 [00:00<00:00, 52.36it/s]
Scanning timeline for affinity apis:  35%|█████████▏                | 12/34 [00:00<00:00, 54.67it/s]
Scanning timeline for affinity apis:  53%|█████████████▊            | 18/34 [00:00<00:00, 52.44it/s]
Scanning timeline for affinity apis:  71%|██████████████████▎       | 24/34 [00:00<00:00, 53.01it/s]
Scanning timeline for affinity apis:  88%|██████████████████████▉   | 30/34 [00:00<00:00, 53.36it/s]
                                                                                                    

Scanning timeline for affinity apis:   0%|                                    | 0/9 [00:00<?, ?it/s]
                                                                                                    
{"status": "success", "code": 0, "message": {"问题综述": {"headers": ["category", "description", "suggestion", "problem count", "total_time(us)", "time ratio", "income(us)", "income ratio"], "data": [["环境变量分析", "描述并给出最优的环境变量配置建议", "1. 请设置最优的环境变量", "", "", "", "", ""], ["整网耗时分析", "模型E2E的时间是1422.767ms。\n    -- 计算时长是332.579ms\n    -- 未被掩盖的通信时长是203.812ms\n    -- 空闲时长是886.376ms\n \n", "", "", "", "", "", ""], ["字节对齐分析", "17个通信算子的数据大小未对齐，这会降低通信性能。", "1. 请调整数据大小，对齐通信算子的数据量，具体方法请联系相关HCCL研发。", "", "", "", "", ""], ["包分析", "过小的通信数据包可能会导致host传递瓶颈。\n在SDMA通信中，通信数据量的100.00%小于16.0MB，总时间为2.9185ms。\n", "1. 数据并行建议, 如果异常通信集中在数据并行域，1.增加批量大小；2.增加梯度累积。\n2. 检查内存优化策略, 如果内存优化策略为Zero3，建议在内存条件允许的情况下将其设置为Zero2/Zero1。\n3. 适配亲和优化器或融合算子, 使用亲和优化器或融合算子可以减少通信算子的数量。", "", "", "", "", ""], ["Cube算子性能分析", "提供一些AICORE算子的参考瓶颈", "1. 请根据亲和性、bound类型或优化空间尝试分析筛选出来的算子", "", "", "", "", ""], ["Vector算子性能分析", "提供一些AICORE算子的参考瓶颈", "1. 请根据亲和性、bound类型或优化空间尝试分析筛选出来的算子", "", "", "", "", ""], ["动态shape算子", "找到所有是动态shape的算子", "1. 在python脚本入口加入以下代码关闭在线编译：\n'torch_npu.npu.set_compile_mode(jit_compile=False) \n torch_npu.npu.config.allow_internal_format = False' \n", 1, "", "", "", ""], ["可融合算子分析", "共检测到94个有融合价值的算子序列，总计端到端耗时9775.139ms， 其中npu时间共981.28ms，host瓶颈耗时占比0.9，mte耗时占比0.009.", "1. 发现大量存在host下发瓶颈的算子序列，可能原因是代码中使用了NPU非亲和操作。请先采集开启调用栈数据根据算子索引确定代码位置评估算法实现的亲和性，如无问题可进行后续可融合性评估\n2. 基于可融合算子序列数据，联系开发人员评估算法层面上算子序列是否可融合.", "", "", "", "", ""]]}, "整网耗时分析": {"headers": ["Performance Index", "Duration(ms)", "Duration Ratio"], "data": [["计算时长", "332.579", "23.38%"], ["    -- Flash Attention", "0.000", "0.00%"], ["    -- Conv", "0.000", "0.00%"], ["    -- Matmul", "245.807", "17.28%"], ["    -- Vector", "85.193", "5.99%"], ["    -- SDMA(Tensor Move)", "0.000", "0.00%"], ["    -- 其它Cube", "2.236", "0.16%"], ["未被掩盖的通信时长", "203.812", "14.33%"], ["    -- 等待时长", "0.017", "0.00%"], ["    -- 传输时长", "203.795", "14.32%"], ["空闲时长", "886.376", "62.30%"], ["    -- SDMA", "0.000", "0.00%"], ["    -- 空闲时长", "886.376", "62.30%"], ["E2E时长", "1422.767", "100.00%"]]}}, "suggestion": "", "log_path": "/tmp/msprof_analyze_1787129023.log"}

[Command succeeded with exit code 0]
AI: 证据链已完整。整理最终结论。

---

# 集群快慢卡分析结论

## 1. 数据概况

- **数据**：4 Rank（Rank 0–3），DB 类型（`ascend_pytorch_profiler_{0..3}.db`），目录 `/workspace/user_data`
- **已执行分析**：`cluster_time_summary`、`slow_rank`、`slow_link`、`compute_op_sum`、`hccl_sum`、`cann_api_sum`（输出 `/tmp/cluster_analysis/` 与 `/workspace/user_data/analysis/slow_rank/`），并对慢卡 Rank1 执行 `advisor all`
- **执行失败项**：`free_analysis` 进程异常退出、`slow_link` 无输出（未发现异常链路），这两项不作为证据

## 2. 结论

**存在快慢卡现象。真正的慢卡是 Rank 1 与 Rank 2**（Rank1 最严重），属于 **Host 下发（调度）瓶颈型慢卡**，即典型"伪快卡"模式：表面看 Rank0/3 通信耗时更长，实际是它们在等 Rank1/2 晚到的集合通信。

### 宏观证据（cluster_time_summary，单位 ms，step ≈ 1414 ms）

| Rank | Compute | Comm(未掩盖) | **Free(占比)** | Comm Wait | Comm Transmit |
|---|---|---|---|---|---|
| 0 | 333.8 (23.6%) | 447.9 | 625.0 (**44.2%**) | 441.1 | 6.8 |
| **1** | 332.6 (23.5%) | **203.8** | **869.5 (61.5%)** | **197.0** | 6.8 |
| **2** | 335.4 (23.7%) | **247.8** | **823.6 (58.2%)** | **241.0** | 6.8 |
| 3 | 334.2 (23.6%) | 445.0 | 628.4 (44.4%) | 438.2 | 6.8 |

关键判读：**各卡传输时间完全一致（6.8ms），计算时间几乎一致（~333ms）**，差异全部集中在 Free 与通信等待的"互补迁移"上：
- Rank1 比 Rank0 **Free 多 244.5ms**、**Comm 少 244.1ms**（差值几乎完全对消）
- Rank2 比 Rank0 **Free 多 198.6ms**、**Comm 少 200.1ms**

即 Rank1/2 每步有 ~200–244ms 的 NPU 空闲，是 CPU 下发滞后导致其"晚到"集合通信点——它们到达时其它卡已在等待，所以自身通信等待极短（197/241ms vs 438/441ms），通信"看起来快"实为"伪快"。

### 交叉验证证据

1. **slow_rank 算法**：`SlowRank` 表 slowAffectCount：Rank1=137、Rank2=129，直接命中 Rank1/2。
2. **hccl_sum（Device 侧 hcom_allReduce_ 总耗时）**：Rank0=425.9ms、Rank1=**200.2ms**、Rank2=231.1ms、Rank3=424.6ms。同一集合通信，Rank1 在 barrier 内耗时不足 Rank0/3 一半 → 晚到者等待少。
3. **cann_api_sum（Host 侧）**：
   - `aclrtSynchronizeEvent`：Rank1 总量仅 **19.6ms**（17 次，平均 1.15ms/次），Rank0/2/3 为 285.7–329.9ms（平均 16.8–19.4ms/次）→ Rank1 同步点几乎无等待，NPU 队列被饿空
   - `aclnnInnerFusedInferAttentionScore`：Rank1=130.1ms vs Rank0=83.7ms（**1.55×**），FA 下发开销偏大
   - `launch`：Rank1=276.8ms vs Rank0=259.5ms（+6.7%）
4. **compute_op_sum / compare_op_stats**：MatMul、RmsNorm、FA 等算子 **Count 与 Mean 各 Rank 完全一致**（如 MatMul 1920 次均 ~70.5μs，diff ratio 0.99–1.02）→ 排除计算型慢卡与负载不均。
5. **advisor（Rank1 目录）**：空闲占比 **62.30%**，并明确检出"大量存在 **host 下发瓶颈**的算子序列"（94 个可融合序列）；另有 SDMA 通信 **100% 数据 <16MB**（小包）、**17 个通信算子字节未对齐**、1 个动态 shape 算子。

> ⚠️ 一致性与缺口说明：advisor 对 Rank1 的 wait/transmit 拆分（wait≈0.02ms / transmit≈203.8ms）与 cluster_time_summary（wait=197ms / transmit=6.8ms）存在统计口径差异，但不影响"Rank1 通信总时长显著短、空闲显著长"这一主结论；`free_analysis` 无法运行，Free 成因以 API/advisor 证据佐证。

## 3. 建议（按优先级）

1. **[P0] 排查 Rank1/Rank2 的 Host 下发瓶颈**：检查下发线程 CPU 绑核/亲和性、是否有其他进程抢占 CPU、launch 间隙与同步点（重点看 `aclrtSynchronizeEvent` 与 FA 下发 1.55× 开销），确认 Rank1/2 所在节点 CPU 负载是否偏高；可对比主机侧 `top`/`npu-smi`。
2. **[P1] 通信小包与对齐问题**：SDMA 100% 数据量 <16MB → 若为 ZeRO3 切分过细则增大 batch/梯度累积或改 ZeRO2/1；对 17 个未对齐通信算子调整数据量到字节对齐。
3. **[P2] 消除动态 shape 与融合机会**：1 个动态 shape 算子 → 入口加 `torch_npu.npu.set_compile_mode(jit_compile=False)`；评估 94 个可融合算子序列的融合收益（当前 E2E 中 host 瓶颈占比高）。

**验证方法**：对 Rank1/2 完成绑核/下发优化后重新 Profiling，观察 `cluster_time_summary` 中 Rank1/2 的 Free 占比是否从 61.5%/58.2% 回落至 ~44%，且四卡 comm wait 拉齐到同一量级；单点验证 `aclrtSynchronizeEvent` 从 19.6ms 恢复至与其它 Rank 一致。

