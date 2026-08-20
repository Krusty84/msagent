## 每Token KVCache占用量

针对每一种注意力类型做计算：
- Full Attention (GQA)：`(num_kv_heads × head_dim × 2 × dtype_bytes)` bytes/token/layer
- MLA (absorption mode)：`(kv_lora_rank + qk_rope_head_dim) × dtype_bytes` bytes/token/layer
- Linear Attention：会话固定循环状态（无per‑token开销），state = `num_heads × head_dim × head_dim × dtype_bytes`
- Sparse‑Attention Indexer Key Cache：`index_dim × dtype_bytes` bytes/token/layer（通常为单头k配合多头q）
- 输出相对MHA基准的压缩比

**单位规范（关键，极易出错）：** 本章全程只使用一套换算约定，并且必须显式写明。标准约定：**KiB = 1024 bytes，MiB = 1024²，GiB = 1024³，GB = 10⁹**。1024倍率会在「单层占用 × 层数 × Token数量」计算中累积，和10⁹的GB混用会产生7‑8%的隐性误差。校验规则：
- 若写“X KiB/token”，则“1M tokens × X KiB” = `1048576 × X × 1024` bytes，除以`1e9`换算为GB（**禁止除以2³⁰**）。
- 校验示例：134.25 KiB/token，1048576 tokens：`1048576 × 134.25 × 1024 / 1e9 = 144.15 GB` ✓。
- 如果写对比行例如“若用 MHA：1M × 60 × 64 × 128 × 2 × 2 = N GB”，N此时仍为**字节**，必须显式做除法，禁止省略单位。
- 在小节末尾增加一行“单位说明”，写明本小节采用的换算约定。

## 参数量计算

每层、每模块分开算，最后汇总。

- Embedding: `vocab_size × hidden_size`
- LM Head: `vocab_size × hidden_size` if `tie_word_embeddings == false`, else 0 (shared)
- Full attention (GQA): `hidden × n_heads · head_dim` (q) + `hidden × n_kv · head_dim` × 2 (k+v) + `n_heads · head_dim × hidden` (o)
- MLA Q path: `q_lora_rank × (hidden_size + hidden_size) + num_heads × qk_head_dim × q_lora_rank` (a\_proj + b\_proj)
- MLA KV path: `kv_lora_rank + qk_rope_head_dim` for a\_proj, then `num_heads × (qk_nope_head_dim + v_head_dim)` for b\_proj
- Sparse-Attention Indexer (MSA / DSA): `hidden × n_idx_heads · idx_dim` (q) + `hidden × idx_dim` (k, single-head) + per-head RMSNorm
- Per-head QK Norm: `(n_heads + n_kv) × head_dim` per layer (when `qk_norm_type=per_head`)
- Dense MLP (SwiGLU): `3 × hidden_size × intermediate_size`
- MoE routed experts: `n_routed_experts × 3 × hidden_size × moe_intermediate_size` (gate\_up + down = 3×)
- MoE shared expert: `n_shared × 3 × hidden_size × shared_intermediate_size`
- Linear attention: count all in\_proj weights + out\_proj + gate projections + recurrent state init
- Active params per token = ALL non-MoE-routed weights + (top\_k / num\_experts) of routed expert weights + all shared experts. **Decide upfront whether lm\_head counts as "active"** — both Anthropic-style (active = forward FLOPs proportional) and inference-style (active = weights actually fetched per token) include it; if the official figure excludes it, note the reconciliation gap explicitly.

## 3.4 不同量化方式与并行模式下的单卡权重显存估算 (REQUIRED for any MoE model)

按顺序输出以下 4 个子小节：

1. **量化与切分约定** — A table fixing the quantization conventions (BF16 / W8A8 / W4A8 default; if model card pushes a specific scheme like NVFP4, add it):
   - **BF16** (2 B/param) — embedding, lm_head, all RMSNorm/LayerNorm, vision tower, multimodal projector. These are NEVER quantized — small parameter count, large precision impact.
   - **W8A8** (1.0 B/param) — all GEMM weights: attention projections (q/k/v/o), indexer projections, gate, dense MLP, routed experts, shared expert.
   - **W4A8** (0.5 B/param) — same coverage as W8A8.
   - Quote the parallelism convention: e.g. "DP·N / TP=1 / EP=N — pure expert parallel; routed experts split N-way; everything else replicated."

2. **权重组件分类** — A table listing every weight group with: 规模 / 切分方式 (复制 vs EP 切分) / 量化策略 / BF16 总量. Aggregate into three buckets:
   - **K (BF16, replicated)** — emb + lm_head + norms + vision; small, fixed per card
   - **Q_rep (quantized, replicated)** — attn + indexer + gate + shared + dense MLP; medium, scales with quant byte
   - **Q_experts (quantized, EP-sharded)** — routed experts; dominant term

   End with the explicit formula:
   ```
   W_card(quant, EP) = K_BF16 · 2 + Q_rep · b_quant + (Q_experts / EP) · b_quant
   ```

3. **单卡权重显存矩阵 (核心结果)** — A table with one row per (parallelism, quant) combination. Default to the four EP values **{8, 16, 32, 64}** × {W8A8, W4A8} = 8 rows. Columns: 并行配置 / 量化 / BF16部分(K) / 量化复制(Q_rep) / 每卡专家分片 / **权重合计/卡** (boldface) / 权重占比(64GB HBM) / 权重占比(96GB HBM). Highlight in red any entry that exceeds the HBM budget (e.g. ">64GB → 超 ⚠"). If the model card specifies a different parallelism set, use that instead.

4. **趋势与决策要点** — A short table + decision block calling out non-intuitive findings:
   - "EP 翻倍 ≠ 显存减半" — the K + Q_rep baseline is fixed; quantify the actual scaling
   - "W4A8 vs W8A8 节省的绝对量随 EP 缩小" — high EP makes weight quant matter less in absolute GB
   - HBM-cliff warnings (e.g. EP=8/W8A8 may not fit 64GB cards)
   - End with a "选型建议" block giving 4-5 concrete recommendations: 硬件型号 → 配置 → 权重显存 → 留余给 KV+激活
