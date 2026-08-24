# L2 结构家族通用坑（structure-family-pitfalls）

> **定位与阅读规则**
> - 本文件**仅对某类结构成立**（MoE / MLA / 混合 attention / 自定义 modeling / VLM / DiT / DSA-SWA-GatedDeltaNet / Dense FFN）。
> - 先按**触发信号**判断新模型属于哪几类（可以多属），再**叠加 L1 读本文件对应小节**。
> - 每条经验标注证据来源与置信度；结构家族内的「回退」只是手段之一，同样涵盖离群值抑制、量化方法选型、结构映射等手段。

---

## 触发信号 → 结构家族速查

| 触发信号（从 `named_modules` / config 判断） | 归属家族 | 本节 |
|---|---|---|
| 存在 `*.experts.*` / `*.mlp.experts.*` / routed+shared experts | MoE | §1 |
| 存在 `router` / `gate` / `*.mlp.gate` / `*.router.gate` | MoE（路由层） | §1 |
| 存在 `kv_b_proj` / `q_a_proj` / `kv_a_proj` / `wk` / `weights_proj` / `wq_b` | MLA 低秩投影 | §2 |
| 存在 `linear_attn` / `gated_delta_net` / `dsa` / `swa` 结构 | 混合 attention / 特殊结构 | §3 §5 |
| 自定义 `modeling_*.py`（非标准 transformers 结构命名） | 自定义 modeling | §4 |
| 含 `vision_tower` / `merger` / `multi_modal_projector` 等跨模态结构 | VLM | §6 |
| DiT / 扩散生成主干 + `mod`/调制模块 | DiT/扩散 | §7 |
| 纯 dense FFN（`mlp.gate_proj/up_proj/down_proj`，无 experts） | Dense FFN | §8 |

---

## 1. MoE 家族

### 1.1 核心区分

必须区分四类：**路由层（gate/router）/ routed experts / shared_experts / 普通 attention 与 FFN**。不要把「属于 expert」当作整体回退理由。

### 1.2 通用原则（横跨三格式）

| 模块 | 默认处理 | 说明 |
|---|---|---|
| `gate` / `router` | 排除量化 / 保持高精度 | 路由决策敏感，是**结构性排除**，非普通线性层逐层回退 |
| `shared_experts` | 通常保持高精度 | 与 routed experts 计算路径不同，多份实践排除出低比特区间 |
| routed experts 普通 `up_proj/down_proj` | 默认量化 | 只有敏感层/精度结果支持时才回退 |
| MLA 低秩投影（`kv_b_proj` 等） | 条件性排除 | 仅当结构存在（见 §2） |

- 证据来源：多份 `lab_practice`（qwen3_moe、deepseek_v3、glm_5 等）；置信度：高。

### 1.3 按格式的低比特落点

| 格式 | 低比特落点 | attention / 一般 FFN |
|---|---|---|
| W8A8 | 无（experts 也走 W8A8） | W8A8 |
| W4A8 | `*mlp.experts*`（int4 权重 + ssz） | 保持 W8A8 |
| W4A4 | `*mlp.experts.*`（int4×int4） | 提级 W8A8 |

- 结构映射来源：`expert_experience.yaml` 中 w4a8 只有 `MoE: w4a8_dynamic`，attention 仍 `w8a8_default`。
- 证据来源：`qwen3-30b-w4a8-v1.yaml`、`deepseek_w4a8.yaml`、`glm_5_1_w4a8c8.yaml`；置信度：高。

### 1.4 常见敏感层与提级手法

- **敏感层专家提级**：多份实践把「某几层 experts」从 W4A8 单列提级回 W8A8（如 `model.layers.41~47.mlp.experts*`），其余 experts 落 W4A8 —— 即「专家整体 W4 + 指定高敏感层专家 W8」是通用手法。
- **首几层保护**：`flex_awq_ssz` 中排除 `model.layers.0/1/2.*`，避免低比特权重早期传播误差。
- **`o_proj`（self_attn 输出投影）**：精度不达标时作为结构化回退候选，`exclude` 增加 `*.o_proj`（单层投影保持浮点），**非整层回退**。
- **`*mlp.down_proj*` / `*shared_experts.down_proj*`**：输出投影回退候选（与 L1 通用结论同源）。

- 证据来源：`qwen3-30b-w4a8-v1.yaml`、`deepseek_w4a8.yaml`、`glm_5_1_w4a8c8.yaml`；置信度：中（层号随模型不同）。

---

## 2. MLA 低秩投影家族

### 2.1 触发信号

出现 `kv_b_proj` / `q_a_proj` / `kv_a_proj` / `wk` / `weights_proj` / `wq_b` / `wo_a` / `wo_b` 等低秩/分解投影。

### 2.2 规则

| 模块 | 处理 | 说明 |
|---|---|---|
| `*kv_b_proj`、`*wk`、`*weights_proj` | 高频条件性排除 | MLA 低秩投影，多份实践排除 |
| `*q_a_proj` / `*kv_a_proj` | 是否量化、量化到哪档**必须按结构逐项确认** | 不可只记 `kv_b_proj` 一句话 |
| `*wo_a`/`*wo_b`/`*compressor.*`/`*indexer.weights_proj` | 条件性排除 | DeepSeek-V4 类索引/压缩投影 |

- 证据来源：`deepseek_w8a8_quarot.yaml`、`deepseek_v4_pro_w4a8.yaml`、多份 kimi/glm 实践；置信度：高（排除方向）、中（具体层号/档位）。

---

## 3. 混合 attention 家族（MLA + GatedDeltaNet + 线性 attention）

### 3.1 触发信号

同时存在 `self_attn`（MLA）与 `linear_attn` / `gated_delta_net` 等混合结构；常见命名 `linear_attn.in_proj_qkvz` 等。

### 3.2 规则

- 混合 attention 中，线性 attention 的投影（如 `linear_attn.in_proj_qkvz`）可与 MLA 分开量化（Qwen3-Next 实践即 `include: ["*linear_attn.in_proj_qkvz*"]` 单列）。
- 混合结构中 MLA 低秩投影仍按 §2 排除。
- 证据来源：`qwen3-next-80b-a3b-w8a8.yaml`；置信度：中。

---

## 4. 自定义 modeling 家族

### 4.1 触发信号

非标准 transformers 结构命名（`*.ffn.shared_experts.*`、`*.block_sparse_moe.*`、`*.mlp.expert_bias` 等），或模型目录内自定义 `modeling_*.py`。

### 4.2 规则

- **结构命名可能偏离常规**：如 DeepSeek-V4 的 FFN 用 `ffn` 而非 `mlp`、shared experts 为 `ffn.shared_experts`；MiniMax 用 `block_sparse_moe.experts`；Hy3 有 `mlp.expert_bias`、`router.gate`。
- 处理前**必须先取得真实 `named_modules`**，不要把 `mlp`/`self_attn` 等常规前缀硬套。
- 证据来源：`deepseek_v4_pro_w4a8.yaml`、`minimax_m27_w8a8_mxfp8.yaml`、`hy3_w8a8.yaml`；置信度：高。

---

## 5. DSA / SWA / GatedDeltaNet 特殊结构

### 5.1 规则

- `expert_experience.yaml` 将这三类结构映射为 **`bf16`**（保持高精度）。默认进入量化范围外，若要量化需**另确认处理器/推理引擎支持**。
- 这是结构保持高精度的**结构映射结论**，不是量化后测评结论。
- 证据来源：`expert_experience.yaml`；置信度：高（映射存在）、中（量化可行性未验证）。

---

## 6. VLM 家族

### 6.1 规则

- **初版仅量化 LLM 部分**：视觉编码器（`vision_tower`）、跨模态投影/融合层（`merger` / `multi_modal_projector` / `patch_merge_mlp`）先不量化，常见排除。
- 注意 VLM 的 apiversion 可能为 `multimodal_vlm_modelslim_v1`，与纯文本 `modelslim_v1` 区分。
- 证据来源：`internvl*`、`qwen3_vl*`、`kimi_k2_6*` 等实践；置信度：中。

---

## 7. DiT / 扩散生成家族

### 7.1 规则

- 生成 DiT 主干外的 `mod`/调制模块、文本/图像调制首层常见**条件性排除**。
- 扩散模型低比特可用 SVDQuant 类方案（离群值迁移 + SVD 低秩残差 + 残差量化）。
- 证据来源：`wan2_1`/`wan2_2`、`flux1`、`hunyuan_video` 实践；置信度：中。

---

## 8. Dense FFN 家族（非 MoE）

### 8.1 通用原则

- 普通 attention（`q/k/v/o_proj`）与 FFN 主体**默认继续量化**，只有实践配置/敏感层/精度结果给出证据才回退。
- **不因模块名直接判定回退**（`gate_proj`/`down_proj`/`o_proj`），先确认父模块与层范围。
- `o_proj` 不能泛化为「所有 attention 都应回退」。

### 8.2 按格式

| 格式 | 关键经验 |
|---|---|
| W8A8 | `mlp.down_proj` 优先回退候选（层范围按模型确认）；无其他默认回退 |
| W4A8 | dense 无 experts，低比特可能全 FFN 或按敏感度拆分，**不照搬 MoE experts 低比特模式**，无明确证据时不整体回退 FFN |
| W4A4 | 权重+激活均低比特，回退更保守；更常见「首几块提级 W8A8」而非整体逐层回退 |

### 8.3 W4A4 结构化拆分（观测）

| 结构 | 处理 |
|---|---|
| 前若干主干块 | 提级 W8A8（首几块高精度档位，避免误差早期传播） |
| 自注意力 | 提级 W8A8（高精度） |
| 文本/图像调制首层 | 排除 |
| 低秩投影 | 排除 |

- 证据来源：`qwen3-32b-dense-w4a4.yaml`；置信度：W8A8 高、W4A4 中（纯文本 dense 实践少）。

---

## 9. 结构家族通用处理顺序

1. 固化基线 YAML；取完整 `named_modules`，把结构映射到家族（可多属）。
2. 先覆盖「明确保持高精度」的特殊结构：DSA/SWA/GatedDeltaNet、VLM/扩散的 `merger`/`mod` 排除。
3. 处理 MoE `gate`/`router`、`shared_experts` 高精度档位。
4. 按格式确定低比特落点（W4A8/W4A4 的 experts）与敏感层专家提级。
5. 处理 MLA 低秩投影与 `mlp.down_proj` / `o_proj` 等结构化回退候选。
6. 最后才基于敏感层分析处理普通 attention/FFN 主体，不做整体盲退。

每次只引入可追踪的结构化变更，记录层号、来源 YAML、置信度；不同结构的层号与模块前缀**不得直接合并**。