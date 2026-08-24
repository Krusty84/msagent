# GLM-5.1 专属经验

> 层级：L3。结构家族背景同 L2「MoE」「MLA」。本节记该模型独有坑。

## 1. W4A4 走 MXFP4 路径

- GLM-5.1 的 W4A4 使用 **mxfp4**（per_block + minmax，act 加 `ext.axes: -1`），而非 int4；attention/一般 FFN 用 mxfp8 提级。
- 结构拆分：`*self_attn*`（排除 `*kv_b_proj`/`*wk`/`*weights_proj`）→ mxfp8；`*mlp*`（排除 `*gate`/`*mlp.experts.*`）→ mxfp8；`*mlp.experts.*` → mxfp4。
- source：`lab_practice/glm_5/glm_5_1_w4a4c8_mxfp4.yaml`；置信度：中。

## 2. W4A8 int 路径（多层尾部/首层排除）

- W4A8 用 `quarot(export_extra_info: True)` + `flex_awq_ssz`（enable `up-down`）+ `flex_smooth_quant`（enable `norm-linear`/`ov`/`up-down`）+ `fa3_quant`。
- `flex_awq_ssz` 排除 `model.layers.0/1/2.*` 与 `*mlp.shared_experts.*`（首层保护 + shared 高精度）。
- `fa3_quant` 排除 `model.layers.0~4.*` 与 `model.layers.74~78.*`（首/尾敏感层）。
- `linear_quant` 各段排除 `model.layers.78.*`；MLA 低秩投影 `*kv_b_proj`/`*wk`/`*weights_proj` 排除。
- source：`glm_5_1_w4a8c8.yaml`；置信度：中。

## 3. 校准集与分片

- 校准集 `mix_calib.jsonl` 或 `qwen3_cot_w4a4.json`；`part_file_size: 4`。
- source：`glm_5_1_w4a8c8.yaml`、`glm_5_1_w4a4c8_mxfp4.yaml`；置信度：低。