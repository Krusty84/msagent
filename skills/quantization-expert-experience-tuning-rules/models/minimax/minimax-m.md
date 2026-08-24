# MiniMax-M2.7 / M3 专属经验

> 层级：L3。结构家族背景同 L2「MoE」「自定义 modeling」。本节记该模型独有坑。

## 1. `block_sparse_moe.experts` 命名（M2.7 独有）

- M2.7 的专家命名用 `*block_sparse_moe.experts*`（块稀疏 MoE），非标准 `*mlp.experts*`；若照搬 L2 常规前缀会匹配不到层。
- source：`lab_practice/minimax_m2/minimax_m27_w8a8_mxfp8.yaml`；置信度：高。

## 2. W8A8 mxfp8 路径（M2.7）

- `quarot(export_extra_info: True)` + `linear_quant`（mxfp8 per_block + minmax），include 仅 `*block_sparse_moe.experts*` 与 `*self_attn*`，其余不量化。
- source：`minimax_m27_w8a8_mxfp8.yaml`；置信度：中。

## 3. W8A8 int 路径（M3）

- `iter_smooth`（alpha 0.5、scale_min 1e-5、symmetric True、enable `norm-linear`）+ 全局 `w8a8_dynamic`。
- 大范围排除：`*norm*`/`*embed_tokens*`/`*lm_head*`/`*vision_tower*`/`*multi_modal_projector*`/`*patch_merge_mlp*`/`*mlp.down_proj*`/`*shared_experts.down_proj*`/`*mlp.gate`/`*indexer*`/`*index_*`。
- 说明 M3 是 VLM 且含 indexer/跨模态结构，回退面较大，属该家族「先保 LLM 主体、大量外围排除」的实践。
- source：`minimax_m3/minimax_m3_w8a8.yaml`；置信度：中。