# 架构识别表

从 config 键 + 模型实现代码/权重名两个来源交叉判断（关键事实至少两个来源佐证）。

> **章节归属**：
> - 表格 1「基础注意力」/ 表格 2「稀疏注意力」/ 表格 4「特殊结构」→ Ch1 层类型汇总、Ch2 算子流归类
> - 表格 3「MoE 路由」→ Ch1 层类型汇总、Ch2 MoE 通路

## 基础注意力

| 特性 | Config 键 | 代码 / 权重名线索 |
| --- | --- | --- |
| MLA | `kv_lora_rank`、`q_lora_rank` | `kv_a_proj`、`q_a_proj`、`q_b_proj`、`kv_b_proj` |
| Full Attention (GQA) | `num_key_value_heads` < `num_attention_heads` | `q_proj`、`k_proj`、`v_proj` |
| Linear Attention (GLA) | `linear_attention_dim`、`gate_lr` | `SimpleGLA`、`in_proj_qkv`、`g_proj`、`GroupRMSNorm` |
| Linear Attention (Delta) | `use_gated_delta_rule` | `GatedDeltaNet`、`CausalConv1d`、`A_log`、`dt_bias` |

## 稀疏注意力

| 特性 | Config 键 | 代码 / 权重名线索 |
| --- | --- | --- |
| MSA (MiniMax Sparse Attn) | `sparse_topk_blocks`、`sparse_block_size`、`sparse_index_dim` | `index_q_proj`、`index_k_proj`、`index_q_norm` |
| DSA (DeepSeek/GLM 风格) | `index_topk`、`index_n_heads` | `Indexer`、`wq_b`、`wk`、`k_norm`、`Einsum` |
| CSA / HCA (DeepSeek-V4 混合注意力) | `compress_rate_csa`、`compress_rate_hca`、`index_topk` | `LightningIndexer`、`compressor`、`compressed_kv` |

## MoE 路由

| 特性 | Config 键 | 代码 / 权重名线索 |
| --- | --- | --- |
| 标准 MoE | `n_routed_experts`、`num_experts_per_tok` | `SparseMoeBlock`、`block_sparse_moe.experts.N.{w1,w2,w3}` |
| MoE + 共享专家 | `n_shared_experts` | `shared_experts` |
| 分组受限 TopK | `n_group`、`topk_group` | `GroupLimitedTopK` |
| Hash MoE 引导层 | `num_hash_layers`、`tid2eid` | `hash_moe`、静态 token-id → expert-id 查表 |

## 特殊结构

| 特性 | Config 键 | 代码 / 权重名线索 |
| --- | --- | --- |
| MTP | `num_nextn_predict_layers` | `NextNPredictLayer`（权重可能不在 checkpoint） |
| 多模态 | config 嵌套 vision 配置 | `VisionEncoder`、`multi_modal_projector`、`patch_merge_mlp` |
| MRoPE / 3D RoPE | `mrope_section`、`rope_mode: "3d"` | `MRoPE` |
| Per-head QK Norm | `qk_norm_type: per_head` | `q_norm`、`k_norm` shape `[n_heads, head_dim]` |
| Partial RoPE | `partial_rotary_factor < 1.0`、`rotary_dim` | RoPE 只作用前 `rotary_dim` 个通道 |
| SwiGLU-OAI | `hidden_act: swigluoai` | `x · sigmoid(α·x) · clamp(limit) ⊙ up` |
| Gemma 风格 RMSNorm | `use_gemma_norm: true` | `(1 + w) · RMSNorm(x)` |
| 流形约束超连接 (mHC) | `hc_mult`、`hc_sinkhorn_iters` | `HyperConnection`、`attn_hc`、`ffn_hc` |
