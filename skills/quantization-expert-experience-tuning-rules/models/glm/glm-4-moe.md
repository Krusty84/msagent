# GLM-4-MoE 专属经验

> 层级：L3。结构家族背景同 L2「MoE」。本节记该模型独有坑。

## 1. 末层 `model.layers.92.*` 全排

- W8A8 场景：`flex_smooth_quant` 与 group 内各 `linear_quant` 均排除 `model.layers.92.*`（末层整体不量化）。
- 这是「尾部/末层保护」的模型专属落地，层号 92 是该规模（GLM-4.7）实测结论，不可迁移到其他规模。
- source：`lab_practice/glm4_moe/glm4_7_moe-w8a8-v1.yaml`；置信度：中。

## 2. W8A8 结构拆分

- `flex_smooth_quant` 仅 enable `norm-linear`；`*self_attn*` 与 `*mlp.experts*` 均走 W8A8 动态（均排除末层 92）。
- `part_file_size: 8`（区别于多数别家默认 4）。
- source：`glm4_7_moe-w8a8-v1.yaml`；置信度：中。