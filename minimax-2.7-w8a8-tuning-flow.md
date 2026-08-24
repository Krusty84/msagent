# MiniMax-2.7 W8A8 量化调优 Agent 流程

## 1. 概述

由编排层 skill `quantization-accuracy-tuning-orchestrator` 统一调度，端到端完成 MiniMax-2.7 的 W8A8 量化与精度调优。

```text
参数对齐 → 路由决策 → 环境准备 → 模型准备 → 量化配置调优 → 结果输出
```

## 2. 输入与前置条件

| 参数 | 说明 |
|------|------|
| 模型路径 | 浮点权重目录，如 `./models/MiniMax-2.7` |
| 设备 | 单卡或多卡（如 `npu:0` / `npu:0,1`） |
| 精度目标 | 绝对目标（`≥83%`）或相对目标（`损失≤1%`），未提供绝对值时需先测 FP 基线 |
| 数据集 | 如 gpqa、gsm8k 等 |

`model_type` 由 agent 按模型名自动推导，无需用户指定。

## 3. 调优流程

### 步骤1：路由决策（EP 适配）

| 条件 | 去向 |
|------|------|
| 卡数 ≥ 2 | 量化前委派 `msmodelslim-ep-parallel-adaptation` 做 MoE 检查 + EP 适配 |
| 单卡，或用户明确不用多卡 | 普通单卡流程 |

MiniMax-2.7 为 MoE 模型：多卡时适配通过后回传 `requires_ep=true`，后续每轮量化与评测均固定多卡，中途不退单卡。

### 步骤2：环境与模型准备

- 环境：msmodelslim、NPU 驱动、vLLM、AISBench 就绪。
- 模型：权重、config、tokenizer 完整，transformers 适配。

### 步骤3：量化配置调优（核心循环）

#### 3.1 询问出口标准（子集 + 全集）←→ 用户

进入循环前先确定**两个出口标准**并询问用户：

1. **子集出口标准**：用户给绝对精度值 → 直接作为 target；未给 → 跑子集 FP 基线，`target = baseline - tolerance`；
2. **全集出口标准**：用户给绝对精度值 → 直接作为 target；未给 → 跑全集 FP 基线。

两者是独立评测配置，各自 FP 基线不能互相替代；跑基线会额外占用卡数，须向用户确认可用卡。

#### 3.2 压缩数据集（默认）

调优循环默认使用压缩数据集快速迭代，来源三选一：用户自备 / 委派 `aisbench-dataset-compression-herding` 生成 coreset（仅 aime2025、gpqa）/ 退回全集。

流程：**子集调优 → 全集验证 → 不通过改全集调优**（出口标准见 3.1）。

#### 3.3 调优循环（standing_high_with_experience）

每轮依次执行：

1. `history_clear` 清空历史；
2. `quant-tuning-evaluation-generator`（subagent）生成 Evaluation YAML；
3. `quant-tuning-practice-generator`（subagent）生成 Practice YAML：
   - 参考 `quantization-expert-experience-tuning-rules`（L1+L2+L3）获取回退候选意见，作为 exclude 初值；
   - 调 `tune-practice-cfg`（skill）执行 `msmodelslim analyze` 敏感层分析 → `validate_practice_yaml.py` 校验；
   - 生成 Practice YAML，exclude 以经验库意见结合敏感层分析确定。
4. `accuracy_lookup` 查精度缓存，命中则跳过量化/评测；
5. 未命中：`quant-tuning-quantizer`（subagent）执行 `msmodelslim quant`，`quant-tuning-evaluator`（subagent）执行 `run_evaluation.py`，`accuracy_append` 写缓存；
6. `history_append` 记录当轮；
7. 按退出条件判断继续或结束。

**MiniMax-2.7 经验库要点**：

| 关键经验 | 说明 |
|----------|------|
| 专家命名 | `*block_sparse_moe.experts*`，非标准 `*mlp.experts*` |
| 推荐路径 | `quarot(export_extra_info: True)` + `linear_quant`；W8A8 可用 int（per_token/per_channel）或 mxfp8（per_block/minmax）双路径 |
| include 范围 | 仅 `*block_sparse_moe.experts*` + `*self_attn*`，其余层不量化 |

参考已落地配置：`lab_practice/minimax_m2/minimax_m27_w8a8_mxfp8.yaml`、`minimax_m27_w8a8.yaml`。

二分约束：

- `gate_proj`/`up_proj` 在 vLLM 中融合为 `gate_up_proj`，**必须同退同量化**，截断 exclude 不得落在配对中间；
- 上界=全部敏感层回退（复用 FP 基线精度），Round 1 为 0 层回退，后续取上下界中位数；
- **单轮达标不是退出条件**：达标仅标记上界，继续二分至收敛；
- 经验库只提供 exclude 初值，二分上下界与收敛判定不受其影响。

#### 3.4 退出条件

二分收敛（上下界不可再分，输出上界）或达到最大迭代次数（输出历史最优达标配置）。

### 步骤4：结果输出

- 输出最优量化权重与评测报告；
- 写调优历史、回写 practice 仓库（`finalize_practice_repo.py`）；
- 磁盘保留 ≤ 2 份完整权重。

## 4. 关键命令

```bash
# 敏感层分析
msmodelslim analyze linear --model_type MiniMax-M2.7 --model_path ${MODEL_PATH} --device npu

# 量化（每轮 save_path 形如 round_N/quantized）
msmodelslim quant --model_type MiniMax-M2.7 \
    --model_path ${MODEL_PATH} --save_path ${SAVE_PATH}/round_1/quantized \
    --device npu:0 --config_path ${PRACTICE_YAML} --trust_remote_code True

# 评测
python skills/quant-tuning-evaluate/scripts/run_evaluation.py \
    --quant-model-path ${SAVE_PATH}/round_1/quantized --evaluate-id round-1 \
    --evaluate-config-path ${EVALUATE_YAML} --save-path ${SAVE_PATH} \
    --device npu --device-indices 0
```

## 5. MiniMax-2.7 要点速查

| 要点 | 内容 |
|------|------|
| 模型结构 | MoE，专家命名 `block_sparse_moe.experts` |
| 量化方案 | W8A8 int：`quarot` + `linear_quant`（per_token/per_channel、minmax）；W8A8 mxfp8：`quarot` + `linear_quant`（per_block、mxfp8、minmax） |
| 量化范围 | include 仅 `*block_sparse_moe.experts*` + `*self_attn*` |
| EP 并行 | 多卡自动适配；加入后全程多卡不退单卡 |
| 调优策略 | `standing_high_with_experience`：结构化回退经验 + 摸高二分搜索 |
| 数据集 | 默认压缩子集快速迭代，全集最终验收 |