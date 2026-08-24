# DeepSeek-V3 / R1 / V3.1 / V3.2 专属经验

> 层级：L3。结构家族背景同 L2 的「MoE」「MLA 低秩投影」。本节只记该家族独有、不可迁移的坑。

## 1. MLA 低秩投影排除（该家族高频、跨版本稳定）

- 排除清单高频出现：`*kv_b_proj`、`*wq_b`、`*wk`、`*weights_proj`（V3.2 还有 `*q_a_proj`/`*kv_a_proj*`）。
- 这是 MLA 的逐头/低秩投影，量化敏感，多份实践一致排除；但**同一家族内不同版本命名略有差异**，务必按目标模型实际 `named_modules` 确认，不要照抄旧版本。
- source：`lab_practice/deepseek_v3_2/deepseek_w8a8.yaml`、`deepseek_w4a8.yaml`、`deepseek_w8a8_quarot.yaml`；置信度：高。

## 2. `fa3_quant` 逐头注意力量化（MLA 独有）

- DeepSeek-V3/R1/V3.1 系使用 `fa3_quant` 处理器做 **Q/K/V 逐头（per-head）INT8/FP8 量化**，适应不同注意力头的分布差异。
- 配置要点：`fa_q` 用 `per_token`，`fa_k`/`fa_v` 用 `per_head`（或 `per_token`），dtype 可为 `fp8_e4m3` 或 `int8`。
- **尾部敏感层排除**：实践常排除 `model.layers.0/1/2.*`（首层）以及尾部若干层（如 R1 排除 46~61，V3.1 排除 3~14 + 46~61），避免 fa3 量化误差干扰。
- source：`deepseekr1_w4a8c8_per_channel.yaml`、`deepseekv3_w4a8c8_per_channel.yaml`；置信度：中（层号随版本/训练差异变化，须实测）。

## 3. 尾部 `model.layers.61.*` 敏感层提级

- W4A8 场景下，多份实践将 `model.layers.61.mlp.experts*` 单列提级到 W8A8（其余 experts 走 W4A8），并相应在 W4A8 include 中排除 `model.layers.61.*`。
- 这是「专家整体 W4 + 指定高敏感层专家 W8」的典型落地；层号 61 是该家族某规模的实测结论，**移植到其他规模/版本需按敏感层分析重新确认**。
- source：`deepseek_w4a8.yaml`、`deepseekr1_w4a8c8_per_channel.yaml`、`deepseekv3_w4a8c8_per_channel.yaml`；置信度：中。

## 4. 离群值抑制组合（该家族典型搭配）

- W4A8 常见：`quarot` + `flex_awq_ssz`（enable `up-down`）+ `flex_smooth_quant`（enable `norm-linear`/`ov`/`up-down`）。
- W8A8 常见：`quarot` + `flex_smooth_quant`（enable `norm-linear`/`ov`）。
- source：`deepseek_w4a8.yaml`、`deepseek_w8a8_quarot.yaml`；置信度：中。

## 5. `quarot` 的 block_size

- V3.2 家族实践 `quarot` 多用默认（未显式 block_size），V4 家族显式 `block_size: 32`。迁移时留意 QuaRot 旋转块尺寸对精度的影响。
- source：见 `deepseek-v4.md`；置信度：低。

## 6. 校准集

- V3.2 实践用 `mix_calib.jsonl` 或 `qwen3_cot_w4a4.json`；校准集文案见 L1 §4。
- source：`deepseek_w4a8.yaml`（dataset `qwen3_cot_w4a4.json`）；置信度：低。