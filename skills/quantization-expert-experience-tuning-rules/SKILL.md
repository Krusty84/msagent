---
name: quantization-expert-experience-tuning-rules
description: |
  量化专家经验调优库：按「L1 跨网络通用 → L2 结构家族 → L3 模型专属」三级递进，
  输出量化精度调优的完整手段（离群值抑制 / 量化方法·粒度·对称性选型 / 校准集调整 / 敏感层回退 / 模型专属策略），
  每条附证据来源与置信度。
  本 Skill 是专家经验库，只回答「怎么调、哪些层需要回退」，不修改 YAML、不执行量化、不做 EP 检查 / 服务化 / 评测。
license: Apache-2.0
metadata:
  version: 0.2.0
  domain: quantization
  framework: msmodelslim
  protocol: reference
  skill_class: tool
  aliases:
    - quantization-tuning-rules
    - expert-tuning-experience
    - quantization-fallback-rules
    - expert-fallback-experience
  trigger_intents:
    - 量化调优
    - 量化专家经验
    - 敏感层回退
    - 哪些层需要回退
    - 量化精度调优
    - 离群值抑制
  keywords:
    - 专家经验库
    - 量化调优
    - 离群值抑制
    - 量化方法选型
    - 校准集
    - 敏感层回退
    - mlp.down_proj 回退
    - gate/router 排除
    - shared_experts 保持高精度
    - MLA 低秩投影排除
    - fa3_quant
---

# 量化专家经验调优库

## 职责边界

本 Skill 回答「**给定量化目标与模型结构，如何调优量化精度**」，输出有据可依的调优结论：

- **完整手段链**：离群值抑制算法、量化方法/粒度/对称性选型、校准集调整、敏感层回退、模型专属策略。
- **回退**：排除量化、恢复 BF16/FP16、或把某模块提级到更高精度档位（如 W4A8 中 experts 之外保持 W8A8），只是手段之一，且是**最后手段**。
- **不回退 / 保持量化**：无足够证据支持时保持量化，不等于对所有模型绝对安全。

调优的具体执行（改 YAML、跑 `msmodelslim quant`、EP 检查、服务化、全集/子集测评）由量化/EP 调优 Skill 承接，不在本 Skill 范围内。

## 三级知识结构

按「通用 → 家族 → 专属」递进，**新模型先通读 L1，再按触发信号读 L2 对应小节，最后按 vendor 查 L3 个案**。

| 层级 | 定位 | 文件 | 阅读时机 |
|---|---|---|---|
| **L1 跨网络通用** | 与具体结构无关的通用调优手段与坑 | `cross-model-pitfalls.md` | 任何新模型都先通读 |
| **L2 结构家族** | 仅对某类结构成立（MoE / MLA / 混合 attention / 自定义 modeling / VLM / DiT / DSA-SWA-GatedDeltaNet / Dense FFN） | `structure-family-pitfalls.md` | 按触发信号判定，可多属，叠加 L1 |
| **L3 模型专属** | 各 `<vendor>/<model>.md` 个案，只放该模型/家族独有、不可迁移的坑 | `models/`（索引见 `models/README.md`） | 命中已收录模型时叠加 |

## 输入

尽量提供：

- 模型结构类型与完整模块名样例（`named_modules`）；
- 量化格式：`w8a8` / `w4a8` / `w4a4`（含 int / mxfp 系）；
- 是否 MoE / EP、routed/shared experts 与 gate/router 命名；
- 当前生效 YAML 的 `include`/`exclude`、回退项与量化配置；
- 浮点基线、量化精度、敏感层分析或异常日志；
- 可引用的 `lab_practice` YAML 路径。

## 输出

按以下顺序给出调优结论：

1. **场景定位**：结构家族 + 量化格式，命中 L1/L2/L3 哪几处。
2. **首选调优手段**（非回退）：离群值抑制、量化方法/粒度/对称性、校准集。
3. **优先回退候选**：模块模式、层范围、回退方式与证据来源。
4. **默认保持量化 / 已保持高精度结构**：有明确映射的 BF16 / 排除量化结构。
5. **模型专属策略**：L3 命中项。
6. **建议顺序与风险**、**YAML 变更记录**与证据置信度。

不得只根据模块名称直接宣称「回退必然提升精度」。

## 最小工作流

1. 通读 L1 `cross-model-pitfalls.md`，确认精度调优递进顺序与手段链。
2. 从真实 `named_modules`/YAML 判断结构家族，按 L2 触发信号叠加对应小节。
3. 命中有 L3 个案的模型，叠加 `models/<vendor>/<model>.md`。
4. 先给出非回退手段（离群值抑制 → 量化方法/粒度 → 校准集），不足再给敏感层回退候选。
5. 结论附 `source` 与置信度；执行由量化/EP 调优 Skill 承接。

## 核心规则速查

| 类别 | 默认处理 | 说明 |
|---|---|---|
| 激活离群值 | 优先 Iterative Smooth；复杂分布叠加 Flex Smooth Quant；INT4 用 Flex AWQ SSZ；可叠加 QuaRot | 先于回退，属关键步骤 |
| 权重低比特方法 | INT8 用 minmax；INT4 用 ssz，不足再 autoround/gptq | 权重 per_channel + 对称 |
| 激活粒度 | 性能 per_tensor；精度 per_token；平衡 pd_mix | 激活通常非对称 |
| 校准集 | 10–50 条、场景匹配、删异常、加 badcase | 第 4 步 |
| `mlp.down_proj` | 优先回退候选 | 层范围须按模型确认 |
| `o_proj`（self_attn 输出投影） | 结构化回退候选 | `exclude` 增加 `*.o_proj`，单层投影保持浮点 |
| MoE `gate`/`router` | 排除量化 / 保持高精度 | 路由决策敏感 |
| MoE `experts` | W4A8/W4A4 下为低比特落点 | 敏感实例才提级回退 |
| `shared_experts` | 通常保持高精度 | 不按 routed experts 处理 |
| MLA 低秩投影（`kv_b_proj`/`q_a_proj`/`wk`/`weights_proj` 等） | 条件性排除 | 结构存在时使用 |
| DSA / SWA / GatedDeltaNet | 保持高精度（bf16） | `expert_experience.yaml` 映射 |
| VLM 跨模态融合（`merger`/投影/视觉塔） | 初版不量化，常见排除 | 仅量化 LLM 部分 |

## 参考资料

- `cross-model-pitfalls.md` — L1 跨网络通用调优手段与坑（离群值抑制/方法/粒度/校准集/回退）
- `structure-family-pitfalls.md` — L2 结构家族经验（MoE/MLA/混合 attention/VLM/DiT/DSA/自定义 modeling/Dense FFN）
- `models/`（`models/README.md` 为索引）— L3 模型专属经验（deepseek/glm/minimax）
- 来源：`msmodelslim/docs/zh/user_guide/process_quantization_precision_tuning.md`
- 来源：`msmodelslim/docs/zh/knowledge_base/quantization_algorithms/README.md`
- 来源：`msmodelslim/lab_practice/**/*.yaml`
- 来源：`msmodelslim/msmodelslim/core/tune_strategy/common/config_builder/expert_experience/expert_experience.yaml`
- 协同执行：`msmodelslim-ep-parallel-adaptation`（EP 适配）；调优主流程由 `quantization-accuracy-tuning-orchestrator` 承接