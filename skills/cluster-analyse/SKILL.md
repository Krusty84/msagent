---
name: cluster-analyse
description: 专门用于 Ascend 集群 Profiling 性能数据的综合分析专家技能。当用户提供【集群性能数据目录/路径】并要求分析【集群耗时拆解】、【计算/通信/内存占比】、【卡间耗时差异】、【通信矩阵/带宽】、【慢卡识别】、【Host下发问题】或【集群性能瓶颈定位】时，必须触发此技能。该技能自动调用 msprof-analyze 的 cluster 能力，根据用户指定的分析模式（-m 参数）执行对应分析，输出各类分析数据，帮助定位慢卡、慢节点、慢链路等问题。
---

# 集群综合分析

## 1. 技能目标
在 Ascend 多卡/集群场景下，利用 msprof-analyze 的 `-m` 参数指定分析能力，对集群训练数据进行综合分析。用户只需说明要分析什么，系统自动选择对应的 `-m` 参数执行分析。

## 2. 分析模式选择 (-m 参数)

| 分析能力 | 介绍 |
| --- | --- |
| cluster_time_summary | 提供集群训练过程中迭代耗时的拆解，帮助用户找到性能瓶颈。 |
| cluster_time_compare_summary | 提供AI运行过程中集群维度的性能数据对比能力，帮助用户找到性能瓶颈。 |
| module_statistic | 针对PyTorch模型自动解析模型层级结构的分析能力，帮助用户精准定位性能瓶颈。 |
| calibrate_npu_gpu | 自动对比NPU和GPU的性能数据，帮助用户进行跨平台的性能校准和瓶颈分析。 |
| compute_op_sum | device侧运行的计算类算子汇总。 |
| freq_analysis | 识别aicore是否存在空闲（频率为800MHz）、异常（频率不为1800MHz或800MHz）的情况并给出分析结果。 |
| ep_load_balance | moe负载信息汇总分析。 |
| computational_op_masking | 提供集群训练过程中不同算子耗时的掩盖计算，帮助用户找到性能瓶颈。 |
| communication_group_map | 集群场景通信域与并行策略呈现。 |
| communication_time_sum | 集群场景通信时间和带宽汇总分析。 |
| communication_matrix_sum | 集群场景通信矩阵汇总分析。 |
| hccl_sum | 通信类算子信息汇总。 |
| pp_chart | pp流水图数据分析，针对pp并行下各个阶段的耗时分析与可视化能力。 |
| slow_rank | 根据当前的快慢卡统计算法，展示各个rank得出的快慢卡影响次数，识别慢卡出现的原因。 |
| communication_bottleneck | 对于长耗时通信算子，识别快慢卡，并推测造成通信等待的Host/Device侧操作。 |
| cann_api_sum | CANN层API的汇总。 |
| mstx_sum | MSTX自定义打点汇总。 |
| free_analysis | 提供对Device侧大块空闲时间的自动分析能力，能够识别空闲时间产生的原因，帮助用户定位性能问题。 |
| export_summary | 导出集群中各卡的API统计信息和Kernel详情信息，生成api_statistic.csv和kernel_details.csv文件。 |
| mstx2commop | 将通过MSTX内置通信打点的通信信息转换成通信算子表格式。 |
| p2p_pairing | P2P算子生成全局关联索引，输出的关联索引会作为一个新的字段`opConnectionId`附在`COMMUNICATION_OP`的表中。 |
| all | 同时解析通信矩阵communication_matrix和通信耗时数据communication_time。 |

## 3. 诊断先验知识库 (Expert Rules)
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

## 4. 硬性约束 (MUST DO)

1. **必须使用 msprof-analyze 工具**：使用 `msprof-analyze` 命令进行集群分析。
2. **分析能力选择**：根据提示词选择分析模式，如果根据提示词无法确定使用哪个分析能力，请让用户明确使用哪一个分析能力，不能自己尝试。
3. **禁止孤立分析单卡**：在集群场景下，严禁仅分析单个 Rank 数据而不进行多卡对比，最好只基于最后输出的文件进行分析。命令行失败后不能继续读取各个rank的数据。
4. **必须分析时间占比**：输出报告必须包含各 Rank 的计算、通信、内存拷贝、空闲时间占比分析（适用于 cluster_time_summary 模式）。
5. **时间单位统一规范**：所有原始数据（单位为微秒），报告中必须自动换算为毫秒（ms）展示，并明确标注单位。

## 5. 标准操作流程 (SOP)

1. **确认分析模式**：
   - 根据用户需求确定 `-m` 参数值（参见第2节映射表）。
   - 若用户要求"完整分析"或"全部"，使用 `all` 模式。

2. **执行分析命令**：
   - 执行命令：`msprof-analyze -m <mode> -d <cluster_data> [-o <output_path>] [--force] --agent`
   - 默认情况下，不需要使用`-o`参数，除非用户明确指定输出路径。

3. **读取输出结果**：
   - 根据命令运行返回的结果，读取生成的文件。
   - 如果返回信息有 error，或者 JSON 里 message 为空，不用往下运行。提示让用户自己执行命令（去掉 --agent 参数），查看日志定位原因。

4. **数据解读与瓶颈定位**：
   - 对照【先验知识库】综合判断瓶颈类型
   - 如果从数据中无法做出明确结论，不要给出不准确的结论。

5. **输出报告**：按以下结构输出最终回复：
   - **分析概要**：使用的分析模式、执行的完整命令以及输出结果路径等信息。
   - **详细数据**：表格形式展示分析结果
   - **瓶颈定位**：瓶颈类型、关键证据（引用具体数值和占比）
   - **优化建议**：针对性建议

## 6. 命令调用手册

**核心命令模板：**
```bash
# 【集群综合分析】
msprof-analyze -m <mode> -d <cluster_data_path> [-o <output_path>] [--force] --agent
```

**参数说明：**

| 参数名 | 说明 | 适用模式 |
| --- | --- | --- |
| `-m <mode>` | 分析能力选项，取值见第2节映射表 | 所有 |
| `-d <path>` | 性能数据汇集目录 | 所有 |
| `-o <path>` | 自定义输出路径 | 所有 |
| `--force` | 强制执行（跳过用户属主/文件大小/权限检查） | 所有 |
| `--rank_list` | 指定Rank ID（逗号分隔），默认all | cann_api_sum, compute_op_sum, hccl_sum, mstx_sum |
| `--step_id` | 指定Step ID | cann_api_sum, compute_op_sum, hccl_sum, mstx_sum |
| `--top_num` | TopN数量，默认15 | hccl_sum |
| `--exclude_op_name` | 结果不包含op_name | compute_op_sum |
| `--bp <path>` | 标杆集群数据路径 | cluster_time_compare_summary |


## 8. 高频故障与规避

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
