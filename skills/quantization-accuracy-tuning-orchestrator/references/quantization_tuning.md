# 量化配置调优（阶段）

## 阶段说明

**量化配置调优阶段**是**端到端自动量化与调优流程**编排的第4阶段。这个阶段将循环迭代生成多组量化配置，进行测评，并从中选择最优策略用作结果。

## 执行依赖项

### Agent

| Plugin | Agent name | 功能用途 |
| --- | --- | --- |
| `modelslim-agent` | `quant-tuning-evaluation-generator` | 生成测评配置 |
| `modelslim-agent` | `quant-tuning-practice-generator` | 生成量化配置 |
| `modelslim-agent` | `quant-tuning-quantizer` | 依据量化配置进行量化 |
| `modelslim-agent` | `quant-tuning-evaluator` | 对量化后的模型进行测评 |

### MCP Tools

| Plugin | MCP Server | MCP Tool name | 功能用途 |
| --- | --- | --- | --- |
| `modelslim-agent` | `modelslim` | `history_clear` | 清空当前调优历史，每次调优任务开始时用于初始化 |
| `modelslim-agent` | `modelslim` | `history_append` | 记录一条调优历史，用于记录每次循环迭代的调优过程 |
| `modelslim-agent` | `modelslim` | `accuracy_append` | 计算结束后，将practice + evaluate配置的评测结果写入精度缓存，避免重复计算 |
| `modelslim-agent` | `modelslim` | `accuracy_lookup` | 计算开始前，在精度缓存中根据practice + evaluate配置查询评测结果，避免重复计算 |

## 详细步骤

完整的调优循环流程图如下。在执行该流程前和流程中，须遵守后续小节列出的约束规则。

```plaintext
             ┌───────────────┐
             │ Agent:        │
             │ evaluation-   │
             │ generator     │
             │ (生成测评配置) │
             └───────┬───────┘
                     ▼
             (>>> 循环开始 <<<) ◄────────────┐
                     ▼                      │
            ┌────────────────────┐          │
            │ 输出:              │          │
            │ "第X次调优循环"     │          │
            └────────┬───────────┘          │
                     ▼                      │
            ┌─────────────────┐             │
            │ MCP Tool:       │             │
            │ history_clear   │             │
            │ (初始化历史)     │             │
            └────────┬────────┘             │
                     ▼                      │
            ┌─────────────────┐             │
            │ Agent:          │             │
            │ practice-       │             │
            │ generator       │             │
            │ (生成量化配置)   │             │
            └────────┬────────┘             │
                     ▼                      │
            ┌─────────────────┐             │
            │ MCP Tool:       │             │
            │ accuracy_lookup │             │
            │ (查询精度缓存)   │             │
            └────────┬────────┘             │
               ┌─ 缓存命中? ─┐               │
               │            │               │
         Yes   │        No  ▼               │
(跳过量化/评估) │    ┌───────────────┐       │
               │    │ Agent:        │       │
               │    │ quant-tuning- │       │
               │    │ quantizer     │       │
               │    │ (执行量化)     │       │
               │    └────────┬──────┘       │
               │             ▼              │
               │    ┌───────────────┐       │
               │    │ Agent:        │       │
               │    │ quant-tuning- │       │
               │    │ evaluator     │       │
               │    │ (模型测评)     │       │
               │    └────────┬──────┘       │
               │             ▼              │
               │    ┌─────────────────┐     │
               │    │ MCP Tool:       │     │
               │    │ accuracy_append │     │
               │    │ (写入精度缓存)   │     │
               │    └────────┬────────┘     │
               └─────┬───────┘              │
                     ▼                      │
            ┌────────────────┐              │
            │ MCP Tool:      │              │
            │ history_append │              │
            │ (记录调优历史)  │              │
            └────────┬───────┘              │
                     ▼                      │
            ┌─────────────────┐             │
            │ 检查退出条件:    │             │
            │ 1. 精度达标?     │             │
            │ 2. 达到最大次数? │             │
            └────────┬────────┘             │
         ┌───── 满足退出条件? ──────┐        │
         │                         │        │
      No ▼                     Yes ▼        │
(继续循环)                      [ 任务结束 ] │
         └──────────────────────────────────┘
```

## 调优约束规则
 	 
以下规则对上方流程图中的各个环节施加额外约束，执行调优时必须一并遵守。

### FP baseline 获取（循环前强制前置）

若用户未提供 FP baseline：
1. 生成浮点模型的评测配置（不含 quantization 相关参数）
2. 对浮点模型执行评测，记录 baseline 精度
3. 将 baseline 值写入 evaluate_config.yaml 的 target 字段（target = FP baseline, tolerance = 用户容差）
4. 然后才进入循环

### standing_high 摸高二分搜索

采用 standing_high 策略时，调优循环不是"达标即停"，而是二分搜索最少回退层数：

1. **Round 1**：0层回退（下界）
2. **Round 2**：全部敏感层回退（上界）
3. **后续轮**：取上下界中位数，保持 gate_proj/up_proj 配对完整性（见下方约束）
4. **终止条件**：上界与下界不可再二分（如差距 ≤ 配对粒度）
5. **最终结果**：上界（最少回退且达标的配置）为最优

退出条件（判断优先级）：
- 二分收敛（上界与下界不可再分）→ 输出上界为最优
- 达到最大迭代次数 → 输出历史最优达标配置
- **"某轮达标"不是退出条件**，达标只标记上界

### 二分搜索约束（exclude 列表截断规则）

生成 Practice YAML 的 exclude 列表时，必须遵守以下规则：
- gate_proj 和 up_proj 在 vllm 中融合为 gate_up_proj，**必须同退同量化**
- 截断 exclude 列表时，不能在 gate_proj/up_proj 配对中间截断
- 若中位数截断点落在配对中间，向上取整到配对末尾

## 拉起subagent时传入的格式

### Agent: quant-tuning-evaluation-generator

传入参数：
- 模型名称：量化后的模型标识符
- 服务地址：推理服务 host（默认 localhost）
- 服务端口：推理服务 port（默认 8000）
- 设备类型：推理后端设备（默认 ascend）
- 设备数量：并行推理的卡数（默认 1）
- 目标数据集：要评测的数据集列表
- 精度目标：每个数据集的目标精度百分比
- 精度容差：允许的精度波动范围

### Agent: quant-tuning-practice-generator

传入参数：
- model_type：模型类型名
- model_path：模型路径
- save_path：工作目录，Practice YAML 写入此目录下
- device：分析设备（如 npu、npu:0、gpu:0,1）
- strategy：调优策略（"standing_high" 或 "standing_high_with_experience"）
- max_iterations：最大迭代轮次
- prev_result：上轮评测结果（首轮为 None）
- anchor_practice：当前已知最优且达标的 Practice YAML 路径（锚点）

### Agent: quant-tuning-quantizer

传入参数：
- config_path：Practice YAML 路径，JSON 字符串格式
- model_path：原始模型路径
- save_path：量化产物保存路径
- device：设备类型，如 `npu:0`
- trust_remote_code：是否信任远程代码（可选）

### Agent: quant-tuning-evaluator

传入参数：
- config_path：Evaluation YAML 路径，JSON 字符串格式
- device：设备类型，如 `npu`
- device_indices：设备索引列表，如 `[0,1]`
