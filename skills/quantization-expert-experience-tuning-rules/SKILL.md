---
name: quantization-expert-experience-tuning-rules
description: |
  量化结构化回退经验库：按「模型结构 × 量化格式」两维矩阵，
  输出需要排除量化 / 保持高精度 / 提级的层与模块清单（即「哪些层需要回退」），并给出证据来源与置信度。
  本 Skill 是专家经验库中的回退经验条目，只回答「回退哪些层」，不修改 YAML、不执行量化、不做 EP 检查 / 服务化 / 评测。
license: Apache-2.0
metadata:
  version: 0.1.0
  domain: quantization
  framework: msmodelslim
  protocol: reference
  skill_class: tool
  aliases:
    - quantization-fallback-rules
    - expert-fallback-experience
    - structural-fallback
  trigger_intents:
    - 结构化回退
    - 敏感层回退
    - 哪些层需要回退
    - 量化回退规则
  keywords:
    - 专家经验库
    - 结构化回退
    - mlp.down_proj 回退
    - gate/router 排除
    - shared_experts 保持高精度
    - 量化回退规则
---

# 量化结构化回退经验库

## 职责边界

本 Skill 只回答一个问题：**给定模型结构与量化格式，哪些层需要回退**，输出有证据来源的回退候选层清单。

- **回退**：排除量化、恢复 BF16/FP16、或将某模块设为更高精度档位（如 W4A8 中 experts 之外保持 W8A8）。
- **不回退 / 保持量化**：无足够证据支持时保持量化，不等于对所有模型绝对安全。

回退的具体执行（改 YAML、跑量化、EP 检查、服务化、全集/子集测评）由量化/EP 调优 Skill 承接，不在本 Skill 范围内。

## 场景矩阵

决策先锚定两维：[模型结构] × [量化格式]，再查对应规则文件。

| 结构类型 | 归属 | 规则文件 |
|---|---|---|
| Dense LLM（GQA/MHA）、Dense MLA、VLM、DiT/扩散生成、DSA/SWA/GatedDeltaNet | 非 MoE | `fall_back/dense_model_rules.md` |
| MoE LLM、MoE-VLM（含 routed/shared experts） | MoE | `fall_back/moe_model_rules.md` |

规则文件内部按三种量化格式纵向展开，便于对照同一模块在不同格式下的回退差异。

## 量化格式（回退视角）

量化格式决定「低比特落在哪些模块」，是定位回退目标的输入维度。完整定义见 `fall_back/quant_format_mapping.md`，要点：

| 格式 | 权重 / 激活 | 系别 / 粒度 | 回退关注点 |
|---|---|---|---|
| W8A8 | int8 / int8 | int 系，weight per_channel / act per_token | 高敏感模块排除/提级 |
| W4A8 | int4 / int8 | int 系，weight per_channel / act per_token | 低比特集中在 FFN/MoE experts |
| W4A4 | int4 / int4 | int 系，weight per_channel / act per_token | 权重与激活均为低比特，回退更保守 |

## 输入

尽量提供：

- 模型结构类型（Dense LLM 的 GQA/MHA/MLA、MoE、VLM、DiT/扩散、DSA/SWA/GatedDeltaNet 等）和完整模块名样例；
- `quant_type` / 量化格式：`w8a8`、`w4a8`、`w4a4`；
- 是否使用 EP、专家容器命名、routed/shared experts 和 router/gate 命名；
- 当前生效 YAML 的 `include`/`exclude`、回退项和量化配置；
- 浮点基线、量化精度、敏感层分析或异常日志；
- 可引用的 `lab_practice` YAML 路径。

## 输出

按以下顺序给出回退结论：

1. **场景定位**：结构类型 + 量化格式，命中矩阵哪一格。
2. **优先回退候选**：模块模式、层范围、回退方式、适用条件和证据来源。
3. **默认保持量化**：没有敏感证据时不建议回退的模块。
4. **已保持高精度结构**：专家经验或实践明确映射为 BF16 / 排除量化的结构。
5. **建议顺序与风险**：回退候选的优先级、适用范围与需实测确认项。
6. **YAML 变更记录**：变更前后 `include`/`exclude` 或高精度档位差异。

不得只根据模块名称直接宣称「回退必然提升精度」。

## 最小工作流

1. 确认模型包含的目标结构，从真实 `named_modules`/YAML 中取得完整名称。
2. 将模块映射到 MoE、FFN、attention 或特殊结构，避免同名模块误匹配。
3. 按量化格式锁定 `fall_back/quant_format_mapping.md` 的格式定义，确定低比特落点。
4. 依结构进入 `fall_back/dense_model_rules.md` 或 `fall_back/moe_model_rules.md`，优先生成结构化候选（非逐层盲退）。
5. 按 `fall_back/fallback_workflow.md` 完成回退候选的验证、固化或回滚。
6. 保存 YAML 差异、结果、证据来源和置信度。

## 核心规则速查

| 类别 | 默认处理 | 说明 |
|---|---|---|
| `mlp.down_proj` | 优先回退候选 | 多份实践出现，但层范围不统一，须按模型确认 |
| `o_proj`（self_attn 输出投影） | 结构化回退候选 | MoE 精度不达标时 `exclude` 增加 `*.o_proj`，单层投影保持浮点，非整层回退 |
| MoE `gate`/`router` | 排除量化 / 保持高精度 | 路由决策敏感；不是普通线性层的逐层回退 |
| MoE `experts` | W4A8/W4A4 下为低比特落点 | 低比特权重集中在 experts；敏感实例才提级回退 |
| `shared_experts` | 通常保持高精度 | 多份实践排除出低比特区间，不按 routed experts 处理 |
| MLA 低秩投影（`kv_b_proj`/`q_a_proj`/`kv_a_proj`/`wk`/`weights_proj`） | 条件性排除 | 仅在结构存在时使用 |
| 普通 attention/FFN 主体 | 默认继续量化 | 无敏感性证据时不整体回退 |
| DSA / SWA / GatedDeltaNet | 保持高精度 | `expert_experience.yaml` 映射为 `bf16` |
| VLM 跨模态融合（`merger`/投影） | 初版不量化，常见排除 | 调优指南：VLM 初版只量化 LLM 部分 |
| 生成 DiT 主干外的 `mod`/调制模块 | 条件性排除 | 扩散生成模型常见排除 |

## 参考资料

- `fall_back/quant_format_mapping.md` — 量化格式定义与结构映射（定位低比特落点）
- `fall_back/dense_model_rules.md` — 非 MoE 结构回退经验（按三格式展开）
- `fall_back/moe_model_rules.md` — MoE 结构回退经验（按三格式展开）
- `fall_back/fallback_workflow.md` — 回退决策树、候选顺序、锚点/回滚、证据与置信度
- 来源：`msmodelslim/docs/zh/user_guide/process_quantization_precision_tuning.md`
- 来源：`msmodelslim/lab_practice/**/*.yaml`
- 来源：`msmodelslim/msmodelslim/core/tune_strategy/common/config_builder/expert_experience/expert_experience.yaml`
- 协同执行：`msmodelslim-ep-parallel-adaptation`（EP 适配）；调优主流程由 `quantization-accuracy-tuning-orchestrator` 承接