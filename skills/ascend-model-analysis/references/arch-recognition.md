# 架构识别

分析 config 与建模代码时，逐条对照下表，命中即记录对应特征。

| 特征 | Config 键 | 代码 / 权重名线索 |
|---------|------------|-------------------|
| MLA | `kv_lora_rank`, `q_lora_rank` | `kv_a_proj`, `q_a_proj`, `q_b_proj`, `kv_b_proj` |
| Full Attention (GQA) | `num_key_value_heads` < `num_attention_heads` | `q_proj`, `k_proj`, `v_proj` |
| Linear Attention (GLA) | `linear_attention_dim`, `gate_lr` | `SimpleGLA`, `in_proj_qkv`, `g_proj`, `GroupRMSNorm` |
| Linear Attention (Delta) | `use_gated_delta_rule` | `GatedDeltaNet`, `in_proj_qkv`, `CausalConv1d`, `A_log`, `dt_bias` |
| MoE | `n_routed_experts`, `num_experts_per_tok` | `SparseMoeBlock`, `block_sparse_moe.experts.N.{w1,w2,w3}`, `gate_up_proj` packed |
| MSA (MiniMax Sparse Attn) | `sparse_attention_config`, `sparse_topk_blocks`, `sparse_block_size`, `sparse_index_dim` | `index_q_proj`, `index_k_proj`, `index_q_norm`, `index_k_norm` |
| DSA (DeepSeek/GLM-style) | `index_topk`, `index_n_heads` | `Indexer`, `wq_b`, `wk`, `k_norm`, `Einsum` |
| MTP | `num_nextn_predict_layers`, `num_mtp_modules` | `NextNPredictLayer` (note: weights may not be in checkpoint yet) |
| Multimodal | Vision config nested in model config | `VisionEncoder`, `ForConditionalGeneration`, `multi_modal_projector`, `patch_merge_mlp` |
| MRoPE / 3D RoPE | `mrope_section`, `rope_mode: "3d"` | `MRoPE`, multimodal position encoding |
| QK Norm per-head | `use_qk_norm: true`, `qk_norm_type: per_head` | `q_norm`, `k_norm` weight shape `[n_heads, head_dim]` |
| Partial RoPE | `partial_rotary_factor < 1.0`, `rotary_dim` | RoPE applied to first `rotary_dim` slots only |
| SwiGLU-OAI | `hidden_act: swigluoai`, `swiglu_alpha`, `swiglu_limit` | `x · sigmoid(α·x) · clamp(limit) ⊙ up` |
| Gemma-style RMSNorm | `use_gemma_norm: true` | `(1 + w) · RMSNorm(x)` |