# MoE 结构经验规则

适用于含 routed/shared experts 的 MoE 结构（MoE LLM、MoE-VLM）。核心是区分「路由层 / routed experts / shared experts / 普通 attention 与 FFN」，再按量化格式决定低比特落点与回退。按三种量化格式纵向展开。

## 通用原则（横跨三格式）

1. `gate`/`router`：路由决策敏感，优先排除量化/保持高精度；是结构性排除，不是普通线性层逐层回退。
2. `shared_experts`：与 routed experts 计算路径不同，多份实践将其排除出低比特区间（保持 W8A8 或 BF16）；无明确证据时输出「待验证」，不强制量化。
3. routed experts 的普通 `up_proj/down_proj`：默认量化，只有敏感层/精度结果支持时才回退，不因「属于 expert」整体回退。
4. MLA/低秩投影 `kv_b_proj`、`q_a_proj`、`kv_a_proj`、`wk`、`weights_proj`：条件性排除，仅当结构存在时使用。
5. 回退只改量化范围，不改变 EP 专家分片；落地后仍需 EP 适配 Skill 检查每卡只加载本地专家、mapping 只访问本地专家、各 rank 覆盖完整专家范围。

## W8A8（int8）

### 典型结构拆分

| 模块 | 默认处理 | 说明 |
|---|---|---|
| `*self_attn*` | 量化（`w8a8_default`，per_tensor act） | MLA 低秩投影常排除（见下） |
| `*mlp*`（非 experts） | 量化（`w8a8_dynamic`） | `*gate` 排除 |
| `*mlp.experts*` | 量化（`w8a8_dynamic`） | W8A8 下专家普遍量化 |
| `gate`/`router` | 排除 | 各 MoE 实践普遍排除 `*mlp.gate`/`*router*`/`*.router.gate`/`*mlp.router*` |
| `shared_experts` | 保持高精度 | 多份实践 `*mlp.shared_experts.*`（及 `*shared_experts*`/`*ffn.shared_experts*`）排除 |

### 观测到的排除模式

- MLA/low-rank：`*kv_b_proj`、`*wk`、`*weights_proj` 在多份 MoE/混合结构实践高频排除。
- `*mlp.down_proj*`、`*shared_experts.down_proj*` 作为输出投影回退候选。
- `o_proj`（self_attn 输出投影）：MoE 模型 W8A8 精度的常见敏感层，精度不达标时作为**结构化回退候选**——在 `exclude` 中增加 `*.o_proj`（单层投影保持浮点），而非回退整个 attention/decoder layer。
- 多 token 预测头（`mtp*`）在带 MTP 结构实践中的排除。

## W4A8（int4 权重）

### 低比特落点：experts

W4A8 是 MoE 的主战场：`int4` 权重只落到 `*mlp.experts*`，attention 与一般 FFN 保持 W8A8。

| 步骤 | 内容 |
|---|---|
| 1 | `*self_attn*` → `w8a8_default`（或 `w8a8`），排除 MLA/low-rank |
| 2 | `*mlp*`（非 experts）→ `w8a8_dynamic`，排除 `*gate`、`*mlp.experts.*` |
| 3 | `*mlp.experts*` → `w4a8_dynamic`（权重 int4 + ssz） |

### 敏感层 / shared experts 处理

- `shared_experts` 保持高精度：多份实践把 `*mlp.shared_experts.*` 排除出 W4 区间，此模式跨结构高度稳定。
- 敏感层专家提级：多份实践把「某几层的 experts」从 W4A8 单列提级为 W8A8（如 `model.layers.<i>.mlp.experts*`），其余 experts 落 W4A8。即「专家整体 W4 + 指定高敏感层专家 W8」是通用回退手法。
- `o_proj`：与 W8A8 同源，精度不达标时作为结构化回退候选（`exclude` 增加 `*.o_proj`），单层投影保持浮点，不回退整个 attention/decoder layer。
- 首几层 MLP：`flex_awq_ssz` 中排除 `model.layers.0/1/2.*`，避免低比特权重早期传播误差。

## W4A4（int4 权重 + int4 激活）

权重与激活均为低比特，低比特损失比 W4A8 更大，experts 之外更应保守。来源为 `lab_practice` 实践，多含 MLA/混合 MoE 结构。

### 观测到的结构拆分

| 结构 | 处理 | 说明 |
|---|---|---|
| `*self_attn*` | 提级 W8A8（高精度） | 混合 MoE 实践 |
| `*mlp*`（非 experts） | 提级 W8A8 | 混合 MoE 实践 |
| `*mlp.experts.*` | W4A4（低比特落点） | 混合 MoE / MLA 实践 |
| `*kv_b_proj`、`*wk`、`*weights_proj` | 排除 | MLA 低秩投影 |
| `*mlp.router*`、`*o_proj*` | 排除 | 混合注意力 MoE 实践 |

### 规则

- 与 W4A8 一致：experts 才是低比特（int4×int4）落点，attention/一般 FFN 提级 W8A8。
- 路由层 `router`、MLA 低秩投影与 `o_proj` 常排除，与 W4A8 经验同源。
- 低秩投影（`kv_a_proj`/`q_a_proj` 等）是否量化、量化到哪档，必须按结构逐项确认，不可只记 `kv_b_proj` 一句话。

## MoE 建议处理顺序

1. 固化基线 YAML；确认 routed/shared/attention/norm 的完整 `named_modules`。
2. 排除 `gate`/`router`（路由层保持高精度）。
3. 确定 `shared_experts` 高精度档位（有证据则保持，无则待验证）。
4. 按格式决定 experts 是否为低比特落点：
   - W8A8：experts 也走 W8A8；
   - W4A8/W4A4：experts 落低比特，attention/一般 FFN 保持高精度；
   - 指定高敏感层专家提级 W8A8。
5. 处理 MLA/低秩投影与 `mlp.down_proj` 等结构化回退。
6. 由 EP 适配 Skill 检查专家分片；最终接受以全集精度与浮点基线为准。