# 非 MoE 结构经验规则

适用于不存在 routed expert 的结构：Dense LLM（GQA/MHA）、Dense MLA、VLM（视觉-语言理解）、DiT/扩散生成模型，以及 DSA/SWA/GatedDeltaNet 等特殊结构。不能套用 MoE 的 `gate/router`/`experts` 规则。按三种量化格式纵向展开。

## 通用原则（横跨三格式）

1. 普通 attention（`q/k/v/o_proj`）与 FFN 主体默认继续量化，只有实践配置、敏感层分析或精度结果提供证据时才回退。
2. 不因模块名称（`gate_proj`、`down_proj`、`o_proj`）直接判定回退；必须先确认父模块结构与层范围。
3. 按调优指南口径，VLM 初版仅量化 LLM 部分，视觉编码器、跨模态投影/融合层先不量化。
4. DSA、SWA、GatedDeltaNet 由 `expert_experience.yaml` 映射为 `bf16`，默认保持高精度；要量化需另确认处理器/引擎/实测支撑。
5. `mlp.down_proj` 是多份实践的高频回退候选，但层号不统一，必须按目标结构复制/对齐层范围。

## W8A8（int8）

### 默认保持量化

- `q_proj/k_proj/v_proj/o_proj`：无敏感性证据时继续量化。`o_proj` 不能泛化为「所有 attention 都应回退」。
- `mlp.gate_proj/up_proj`：继续量化。
- norm / embedding / lm_head：不自动回退，仅配置与实测明确要求时单独处理。

### 优先回退候选

| 模块 | 回退方式 | 证据来源（实践 YAML） |
|---|---|---|
| `mlp.down_proj` | 排除量化 / 指定层恢复高精度 | 多系 dense LLM 实践：既有通配排除（`*mlp.down_proj*`），也有逐层列举（`layers.<i>.mlp.down_proj`） |
| 首/末层 MLP | 条件性排除 | 层号以具体实践为准，不作通用固定值 |

调优指南明确：`mlp.down_proj` 通常是量化敏感度最高的层之一，应优先考虑回退；但层范围须对齐目标结构，逐一列举与通配排除是两种等价表达。

## W4A8（int4 权重）

### 关键差异

W4A8 是权重压到 `int4` 的格式。对 dense 结构，低比特权重是否全模型生效需看实践；`expert_experience.yaml` 未给出 dense FFN 的 `w4a8` 映射，故 dense 场景 W4A8 属「待验证/需实践支撑」，不要照搬 MoE experts 的低比特模式。

### 默认保持量化 / 回退

- 低比特权重落点需精确到模块：dense 无 experts，低比特可能全 FFN 或按敏感度拆分。
- 无明确敏感证据时不整体回退 FFN。
- 遇精度异常，以 `mlp.down_proj`、首末层为优先候选，证据粒度同 W8A8。

## W4A4（int4 权重 + int4 激活）

权重与激活均为低比特，低比特损失比 W4A8 更大，回退应更保守、优先结构化拆封，而非整体逐层回退。

### 观测到的结构化拆分

| 结构 | 处理 | 说明 |
|---|---|---|
| 前若干主干块 | 提级 W8A8 | 首几块用高精度档位，其余走 W4A4，避免低比特误差早期传播 |
| 自注意力 | 提级 W8A8（高精度） | 保持 attention 为高精度，低比特只落局部 |
| 文本/图像调制首层 | 排除 | 文本 MLP 末层、图像/文本调制首层排除 |
| 低秩投影（`kv_b_proj`/`wk`/`weights_proj`） | 排除 | MLA/混合结构实践 |

### 规则

- W4A4 下「首几块提级 W8A8」比整体逐层回退更常见。
- 注意力保持高精度，低比特 int4 只落局部（主干后半），而非全模型 W4A4。
- 纯文本 dense 的 W4A4 实践较少，无直接证据时回到 W8A8 int 通用原则并标注「待验证」。

## DSA / SWA / GatedDeltaNet

`expert_experience.yaml` 将这三类映射为 `bf16`，即结构保持高精度。若目标格式把其量化，需先确认处理器与推理引擎是否支持对应算子与精度。

## 建议处理顺序

1. 先覆盖明确保持高精度的特殊结构（DSA/SWA/GatedDeltaNet）或 VLM/扩散中的 `merger`/`mod` 排除。
2. 处理 `mlp.down_proj`（按具体层范围）。
3. 最后才基于敏感层分析处理普通 attention/FFN 主体，不做整体盲回退。

每次只引入可追踪的结构化变更，并把层号、来源 YAML、置信度一并记录。