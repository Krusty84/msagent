# 参数量 / KVCache / 显存矩阵公式

> **章节归属**：本文档内容全部归入报告 **Ch3 KVCache 与参数量**。
> - 参数量计算 / 每 token 激活参数 → Ch3 参数量分项表、汇总行
> - KVCache 公式与压缩比 → Ch3 "每 token KV 占用与压缩比"
> - 量化 × 并行显存矩阵 → Ch3 末尾表格（MoE 时 8 行）
> - 单位规范 → Ch3 末尾的"单位说明"行

## 参数量计算

每层、每模块分开算，最后汇总。

- **Embedding / LM Head**：`vocab_size × hidden_size`；LM Head 在 `tie_word_embeddings == true` 时为 0
- **Full Attention (GQA)**：`hidden×n_heads·head_dim`(q) + `hidden×n_kv·head_dim`×2(k+v) + `n_heads·head_dim×hidden`(o)
- **MLA Q 路径**：`q_lora_rank×(hidden+hidden)` + `num_heads×qk_head_dim×q_lora_rank`
- **MLA KV 路径**：a_proj 为 `kv_lora_rank+qk_rope_head_dim`，b_proj 为 `num_heads×(qk_nope_head_dim+v_head_dim)`
- **Sparse-Attention Indexer**：`hidden×n_idx_heads·idx_dim`(q) + `hidden×idx_dim`(k) + per-head RMSNorm
- **Dense MLP (SwiGLU)**：`3×hidden×intermediate_size`
- **MoE routed experts**：`n_routed_experts×3×hidden×moe_intermediate_size`
- **MoE shared expert**：`n_shared×3×hidden×shared_intermediate_size`

### 每 token 激活参数

> = 非 MoE-routed 权重 + routed 的 `top_k / num_experts` + 全部 shared experts

**lm_head 是否计入激活必须在 Ch3 显式声明**（与官方口径不一致时注明对账）。

## KVCache（每 token 每层）

- **Full Attention (GQA)**：`num_kv_heads × head_dim × 2 × dtype_bytes`
- **MLA (absorption)**：`(kv_lora_rank + qk_rope_head_dim) × dtype_bytes`
- **Linear Attention**：会话级固定 state `num_heads × head_dim × head_dim × dtype_bytes`，无每 token 成本
- **Indexer Key Cache**：`index_dim × dtype_bytes`
- **CSA / HCA 混合**：KV 池压缩后 `effective_KV_per_token = (kv_lora_rank × compress_rate) × dtype_bytes`

**同时给出相对 MHA baseline 的压缩比**：`compression = MHA_per_token / model_per_token`。

## 单位规范（易错，全文档统一）

- `KiB = 1024 B`、`MiB = 1024² B`、`GiB = 1024³ B`
- `GB = 10⁹ B`（注意是十进制）
- 写 "X KiB/token" 时，"1M tokens" = `1,048,576` 个
- 换算 GB 时除以 `1e9`（不是 `2³⁰`）
- 校验例：`1,048,576 × 134.25 × 1024 / 1e9 = 144.15 GB`
- 任何 `1M × X = Y GB` 行在未显式除以前单位是字节
- KVCache 章节底部加一行"单位说明"

## 量化 × 并行单卡权重显存（MoE 必需）

### 三桶划分

- **K (BF16, 复制)**：emb + lm_head + norms + vision，永不量化，每卡固定
- **Q_rep (量化, 复制)**：attn + indexer + gate + shared + dense MLP
- **Q_experts (量化, EP 切分)**：routed experts，主导项

### 量化约定

| 精度 | 字节/参数 |
| --- | --- |
| BF16 | 2.0 |
| W8A8 | 1.0 |
| W4A8 | 0.5 |

### 核心公式

```
W_card(quant, EP) = K_BF16·2 + Q_rep·b_quant + (Q_experts/EP)·b_quant
```

### 默认扫描

`EP ∈ {8,16,32,64} × {W8A8, W4A8}` = 8 行，输出每卡权重合计及对 64GB/96GB HBM 占比，超预算高亮。

### 必点结论

- "EP 翻倍 ≠ 显存减半"（K + Q_rep 固定）
- "高 EP 下 W4A8 绝对收益缩小"（Q_experts/EP 主导时收益上限 ≈ `K + Q_rep`）
- 结尾给 4-5 条硬件选型建议
