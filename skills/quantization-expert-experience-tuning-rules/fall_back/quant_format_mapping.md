# 量化格式定义与结构映射

来源：
- `msmodelslim/msmodelslim/core/tune_strategy/common/config_builder/expert_experience/expert_experience.yaml`（int 系：W8A8 / W4A8）
- `msmodelslim/lab_practice/**`（W4A4 实践 YAML）

注意：本文件只定义量化格式与结构映射，用于确定回退时「低比特落在哪些模块」。**不覆盖离群值抑制算法、量化策略（权重/激活 method）选型**——这些由量化调优 Skill 处理，不属于本 Skill 的回答范围。

## 三种格式速览

| 格式 | 权重 dtype | 激活 dtype | 系别 / 粒度 |
|---|---|---|---|
| W8A8 | int8 | int8 | int 系，weight per_channel / act per_token |
| W4A8 | int4 | int8 | int 系，weight per_channel / act per_token |
| W4A4 | int4 | int4 | int 系，weight per_channel / act per_token |

三种均为 int 系：权重 `per_channel`、激活 `per_token`；W4A8 的低比特落在权重（int4），W4A4 的权重与激活均为低比特（int4），回退更保守。

## `expert_experience.yaml` 结构映射（int 系）

| 结构 | W8A8 | W4A8 |
|---|---|---|
| MHA / GQA / MLA（attention） | `w8a8_default` | `w8a8_default`（注意：attention 不落 int4） |
| FFN | `w8a8_dynamic` | 未单独映射 |
| MoE | `w8a8_dynamic` | `w4a8_dynamic` |
| DSA / SWA / GatedDeltaNet | `bf16` | `bf16` |

W4A8 的 attention 仍映射为 `w8a8_default`，说明低比特 int4 只应落到 FFN/MoE experts，attention 保持 W8A8。W4A4 未在 `expert_experience.yaml` 正式映射，规则置信度以「多份实践一致」为准，标注「待验证」，不套用 W4A8 结论。

## 与回退的关系

- 「结构映射为 `bf16`」= 保持高精度规则（不进入量化处理），即该结构不回退。
- 「`structure_configs.include/exclude`」决定模块范围；范围重叠或命名错误会使经验规则失效。
- 未在支持列表 / 无实践覆盖的格式只给「待验证」提示，不伪造回退规则。