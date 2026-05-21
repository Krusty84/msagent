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
