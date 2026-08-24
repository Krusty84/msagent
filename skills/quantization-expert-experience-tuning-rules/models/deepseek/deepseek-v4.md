# DeepSeek-V4-Pro / Flash 专属经验

> 层级：L3。结构家族背景同 L2 的「MoE」「MLA」「自定义 modeling」。本节记该家族独有坑。

## 1. 结构命名偏移（该家族最易踩坑）

- V4 的 FFN 命名用 **`ffn` 而非 `mlp`**：`*.ffn.shared_experts.*`、`*ffn*`；shared experts 写作 `ffn.shared_experts`。
- 若照搬 L2/MoE 的 `*mlp.*` 前缀会导致**匹配不到任何层**。处理前必须取真实 `named_modules`。
- source：`lab_practice/deepseek_v4/deepseek_v4_pro_w4a8.yaml`、`deepseek_v4_flash_w8a8.yaml`；置信度：高。

## 2. `indexer` / `compressor` 投影排除（V4 独有）

- attention 相关低秩/索引压缩投影需排除：`*wo_a`、`*wo_b`、`*compressor.wgate`、`*compressor.wkv`、`*indexer.weights_proj`、`*indexer.compressor.wgate`、`*indexer.compressor.wkv`。
- 这是 V4 引入的 latent 空间索引/压缩侧投影，量化敏感。该清单为 V4 独有，不可迁移到无 indexer 的模型。
- source：`deepseek_v4_pro_w4a8.yaml`、`deepseek_v4_flash_w8a8.yaml`；置信度：中。

## 3. `quarot` block_size=32

- V4 实践 `quarot` 显式 `block_size: 32`（V3.2 家族多为默认）。旋转块尺寸是 QuaRot 精度关键超参之一。
- source：`deepseek_v4_pro_w4a8.yaml`、`deepseek_v4_flash_w8a8.yaml`；置信度：中。

## 4. 格式与结构映射

- W8A8（Flash）：`quarot(32)` + `flex_smooth_quant(norm-linear)`，attn 用 `w8a8_dynamic`，`ffn*` 用 `w8a8_dynamic` 但排除 `*gate`。
- W4A8（Pro）：W4 低比特落在 `*ffn*`（排除 `*gate`、`*shared_experts*`），attn 保持 W8A8；`shared_experts` 单列 W8A8 保持高精度。
  - `flex_awq_ssz`（enable `up-down`）排除 `*.ffn.shared_experts.*`。
  - `flex_smooth_quant`（enable `norm-linear`）排除 `*ffn_norm*`。
- source：`deepseek_v4_pro_w4a8.yaml`、`deepseek_v4_flash_w8a8.yaml`；置信度：中。

## 5. 校准集

- 两实践均用 `dataset: mix_calib.jsonl`。
- source：`deepseek_v4_pro_w4a8.yaml`；置信度：低。