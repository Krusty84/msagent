# 分析报告：Keye-VL-2.0-30B-A3B

## 模型标识
- **模型路径**：`/home/xiayingxin/weights/Keye-VL-2.0-30B-A3B`
- **`model_type`**：`KeyeVL2`
- **`architectures`**：`["KeyeVL2MoeForConditionalGeneration"]`

## 实现来源解析
- **结果**：`model-local`
- **依据**：
  - `model_type = "KeyeVL2"` 未注册在 `transformers` 库中（transformers 4.57.3 不含 `KeyeVL2` 条目）
  - `auto_map` 指向模型目录内本地文件：
    - `AutoConfig` → `configuration_keye_vl_2.KeyeVL2Config`
    - `AutoModel` → `modeling_keye_topk_mask_30ba3b.KeyeVL2MoeForConditionalGeneration`
    - `AutoModelForCausalLM` → `modeling_keye_topk_mask_30ba3b.KeyeVL2MoeForConditionalGeneration`
  - 模型目录内存有 `modeling_*.py`、`configuration_*.py` 完整建模代码
  - **实现衍生关系**：该模型从 `transformers.models.qwen3_vl_moe` 派生而来，继承 `Qwen3VLMoePreTrainedModel`，复用 `Qwen3VLMoeTextSparseMoeBlock`、`Qwen3VLMoeTextMLP` 等核心组件，并在其基础上增加了自研 Top-K Mask 稀疏注意力（`KeyeTopKMaskAttention` + `SALightningIndexer`）

## 模型特征与规格
| 参数 | 值 |
|------|------|
| Hidden size | 2048 |
| 层数 | 48 |
| Attention heads / KV heads | 32 / 4 |
| Head dim | 128 |
| Intermediate size（稠密 MLP） | 6144 |
| MoE intermediate size | 768 |
| 参数量 | ~31.1B（30B-A3B：总 30B，激活 ~3B） |
| 权重重类型 | bfloat16 |
| 权重总大小 | ~62.24 GB（13 个 safetensors 分片） |
| Max position embeddings | 262144 |
| 是否仅分析 VLM 文本部分 | **是**（仅适配文本 decoder 主干） |

## 模型类型、结构差异与连接关系
- **模型类型**：多模态理解模型（VLM）
- **架构组成**：
  ```
  KeyeVL2MoeForConditionalGeneration
  ├── visual (KeyeVL2VisionModel)          ← 视觉编码器（27 层 ViT, patch_size=14, image_size=384）
  ├── mlp_AR (Projector)                    ← 视觉-语言投影器（2 层 MLP）
  └── model (Qwen3VLMoeTopkMaskTextModel)  ← 文本解码器主干（48 层 MoE + Top-K Mask Attention）
      ├── embed_tokens (nn.Embedding)
      ├── rotary_emb (KeyeVL2RotaryEmbedding, 3D MRoPE)
      ├── layers (48 × Qwen3VLMoeTopkMaskTextDecoderLayer)
      │   ├── self_attn (KeyeTopKMaskAttention)   ← 自定义稀疏注意力含 SALightningIndexer
      │   └── mlp (Qwen3VLMoeTextSparseMoeBlock | Qwen3VLMoeTextMLP)
      └── norm (Qwen3RMSNorm)
  └── lm_head (nn.Linear)
  ```
- **相对常见 Qwen2 的特殊结构**：
  1. **Top-K Mask 稀疏注意力**（`KeyeTopKMaskAttention`）：在标准 GQA 基础上增加 `SALightningIndexer`，prefill 阶段通过 indexer 选择 top-k 个 KV 位置构建稀疏 mask，decode 阶段复用缓存的 indexer K/W 进行快速 top-k 筛选。替换原有的 Flash Attention 路径。
  2. **MoE + 全 MoE 布局**：`decoder_sparse_step=1` + `mlp_only_layers=[]`，**48 层全部为 MoE 层**（每层使用 `Qwen3VLMoeTextSparseMoeBlock`，128 experts/8 per tok），无稠密 MLP 层。
  3. **3D MRoPE**：使用 temporal/height/width 三维旋转位置编码（`mrope_section: [16, 24, 24]`），支持图像与视频的多模态位置编码。
  4. **DeepStack**：视觉特征可注入到 decoder 前若干层的 hidden states 中（论文 DeepStack 方法）。
- **特殊结构连接关系**：
  - 视觉编码器输出 → `mlp_AR` Projector 映射 → `inputs_embeds` masked_scatter 替换到 token embedding 中 → 送入文本 decoder 主干统一处理
  - `SALightningIndexer` 为注意力模块的内嵌组件，不影响层遍历路径
  - DeepStack 在 decoder forward 循环中，每层后选择性加回视觉嵌入
- **对适配流程的影响**：
  - 适配只需关注文本 decoder 主干（`model` 部分），视觉编码器与投影器无需修改
  - 注意力模块命名不同于标准 `LlamaAttention`，为自定义 `KeyeTopKMaskAttention`
  - 全部 48 层均为 MoE 层，需处理融合专家权重的 unpack

## 逐层量化建议
- **是否建议逐层量化**：建议
- **触发原因**：模型总权重 62.24 GB（bf16），全量加载需要约 62 GB CPU 内存 + 同等 GPU 显存，在普通开发环境（128 GB RAM + 单卡）下构成内存压力。MoE 128 experts × 48 层 = 6144 个专家权重块，逐层加载可大幅降低峰值内存。
- **说明**：此为高阶可选项，应在基础适配与四步验证完成后再进入 `msmodelslim-layer-wise-quantization`。基础适配阶段可先使用 `low_cpu_mem_usage=True` 加载。

## MoE 评估
- **是否含 MoE**：是
- **布局类型**：**MoE 融合**
- **疑似融合的键/模块**：
  - `model.layers.{n}.mlp.experts.gate_up_proj` — shape **`[128, 2048, 1536]`**（3D 融合张量）
  - `model.layers.{n}.mlp.experts.down_proj` — shape **`[128, 768, 2048]`**（3D 融合张量）
- **专家权重形态**：打包张量（3D 专家权重）
  - `gate_up_proj`：维度 0 = 128 experts，维度 1 = 2048 (hidden_size)，维度 2 = 1536 (768×2, gate 与 up 融合)
  - `down_proj`：维度 0 = 128 experts，维度 1 = 768 (moe_intermediate_size)，维度 2 = 2048 (hidden_size)
  - `gate.weight`（router）：shape `[128, 2048]`（2D 正常）
- **是否需要 unpack**：**是**
  - 所有 48 层均为 MoE 层，gate_up_proj 与 down_proj 均为 3D 融合参数
  - 量化时需要先 unpack 为独立的 128 个 expert 线性层，然后按 expert 逐个量化
  - 使用 `Qwen3VLMoeTextExperts` 类（继承自 transformers），其 `forward` 已支持通过 `self.gate_up_proj[expert_idx]` 索引访问单个 expert

## 适配影响要点
- **Decoder 遍历路径**：
  ```
  KeyeVL2MoeForConditionalGeneration.model (Qwen3VLMoeTopkMaskTextModel)
    → self.layers (ModuleList of 48 × Qwen3VLMoeTopkMaskTextDecoderLayer)
      → layer.self_attn (KeyeTopKMaskAttention)     ← 注意力层
      → layer.mlp (Qwen3VLMoeTextSparseMoeBlock)    ← MoE 专家层（全部 48 层）
    → self.norm (Qwen3RMSNorm)
  ```
- **Attention 模块命名**：`self_attn` → `KeyeTopKMaskAttention`
  - Q/K/V/O 投影命名：`q_proj`, `k_proj`, `v_proj`, `o_proj`（标准）
  - Q/K 额外层归一化：`q_norm`, `k_norm`（`Qwen3RMSNorm`）
  - **额外子结构**：`sa_indexer`（`SALightningIndexer`，含 `q_proj`, `k_proj`, `q_norm`, `k_norm`, `rotary_emb`）
- **MLP / MoE 模块命名**：`mlp` → `Qwen3VLMoeTextSparseMoeBlock`
  - Router：`mlp.gate` → `nn.Linear(2048, 128, bias=False)`
  - Experts：`mlp.experts` → `Qwen3VLMoeTextExperts`（融合 3D 参数）
    - `experts.gate_up_proj`：[128, 2048, 1536]
    - `experts.down_proj`：[128, 768, 2048]
  - 激活函数：silu（gate 部分使用 silu 激活）
- **`visit/forward` 严格对齐点**：
  - `model.layers.{idx}` → 48 层遍历（0-indexed）
  - 每层内：`input_layernorm` → `self_attn` → residual → `post_attention_layernorm` → `mlp` → residual
  - MoE 层 forward 返回 tuple `(hidden_states, router_logits)`，需取 `[0]`
  - `_no_split_modules`：`["Qwen3VLMoeTopkMaskTextDecoderLayer", "KeyeVL2VisionEncoderLayer"]`

## 量化与 MTP 风险评估

### 量化风险
- **模型是否已量化**：**否**
- **量化判定依据**：`config.json` 中 `"dtype": "bfloat16"`，权重文件存储为 bf16（非量化格式），无量化相关参数（如 `quantization_config`）
- **是否已提供反量化脚本**：不适用（模型非量化）
- **风险评估**：**低**。模型为原生 bf16 精度，可直接进行量化感知训练/PTQ

### MTP（Multi-Token Prediction）风险
- **是否存在 MTP 结构**：**否**
- **搜索范围**：对全部 modeling 文件（`modeling_keye_topk_mask_30ba3b.py`, `modeling_keye_vl_2.py`, `configuration_keye_vl_2.py`）全文搜索 `MTP`/`mtp`/`MultiTokenPrediction` 等关键词，无匹配
- **风险评估**：无

## 风险与后续动作
- **风险等级**：**低**
  - ✅ 实现来源明确（model-local，代码完整可读）
  - ✅ 模型为原生 bf16，非量化，无需反量化
  - ✅ 无 MTP 结构
  - ⚠️ MoE 融合权重（48 层全部为 MoE，需 unpack 后逐 expert 量化）
  - ⚠️ 注意力为自定义 Top-K Mask Attention（非标准 attention 路径）
  - ⚠️ 权重总大小 62 GB，内存需求较高

- **阻塞项**：
  - 无硬阻塞项

- **建议下一步**：进入 **`model-adapt-core`**
  - 创建适配器：`msmodelslim/model/keye_vl/`
  - 适配器应专注于文本 decoder 部分（`model` 子模块），不处理 `visual`/`mlp_AR`
  - 遍历路径：`model.layers.{0..47}` → `self_attn`（`KeyeTopKMaskAttention`） + `mlp`（MoE）
  - MoE 融合权重处理：在 `_quantize` 时通过 `experts.gate_up_proj[expert_idx]` / `experts.down_proj[expert_idx]` 索引单个 expert 进行量化
  - 优先使用 `low_cpu_mem_usage=True` 加载权重
  - 基础适配与四步验证完成后，如有内存压力可进入 `msmodelslim-layer-wise-quantization`
