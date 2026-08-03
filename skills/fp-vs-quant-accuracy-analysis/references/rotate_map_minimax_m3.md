# MiniMax-M3 旋转作用范围 (rotate_map)

来源：`msmodelslim/msmodelslim/model/minimax_m3/model_adapter.py` 的 `get_rotate_map`

## rotate_map 定义

旋转矩阵 `R` 来自 `QuaRotInterface.get_rotate_command`，shape `[hidden_size, hidden_size]`，正交 Hadamard 矩阵。

### pre_run（前置旋转）

| 模块 | 方向 | 说明 |
|------|------|------|
| `model.language_model.embed_tokens` | right | 嵌入层输出右乘 R |
| `model.multi_modal_projector.merge_linear_2` | left | 视觉投影输出左乘 R^T |

### 主旋转（per layer）

对每个 `layer_idx` in `[0, num_hidden_layers)`：

#### Attention 部分

| 模块 | 方向 |
|------|------|
| `model.language_model.layers.{idx}.self_attn.q_proj` | right |
| `model.language_model.layers.{idx}.self_attn.k_proj` | right |
| `model.language_model.layers.{idx}.self_attn.v_proj` | right |
| `model.language_model.layers.{idx}.self_attn.o_proj` | left |

#### Sparse Attention 层（仅 sparse 层）

| 模块 | 方向 |
|------|------|
| `model.language_model.layers.{idx}.self_attn.indexer.q_proj` | right |
| `model.language_model.layers.{idx}.self_attn.indexer.k_proj` | right |

#### MoE 层（仅 sparse MLP 层）

| 模块 | 方向 |
|------|------|
| `model.language_model.layers.{idx}.mlp.experts.{i}.gate_proj` | right |
| `model.language_model.layers.{idx}.mlp.experts.{i}.up_proj` | right |
| `model.language_model.layers.{idx}.mlp.experts.{i}.down_proj` | left |
| `model.language_model.layers.{idx}.mlp.shared_experts.gate_proj` | right |
| `model.language_model.layers.{idx}.mlp.shared_experts.up_proj` | right |
| `model.language_model.layers.{idx}.mlp.shared_experts.down_proj` | left |
| `model.language_model.layers.{idx}.mlp.gate` | right |

#### Dense 层（仅 dense MLP 层）

| 模块 | 方向 |
|------|------|
| `model.language_model.layers.{idx}.mlp.gate_proj` | right |
| `model.language_model.layers.{idx}.mlp.up_proj` | right |
| `model.language_model.layers.{idx}.mlp.down_proj` | left |

### lm_head

| 模块 | 方向 |
|------|------|
| `lm_head` | right |

## 逆变换公式

R 正交，`R^T = R^{-1}`。

| 正向变换 | 逆变换（激活） |
|---------|--------------|
| right: `W' = W @ R`, `x' = x @ R` | `x = x' @ R^T` |
| left: `W' = R^T @ W`, `x' = R^T @ x` | `x = R @ x'` |

## 匹配规则

【新格式】按"旋转空间归属"分类（基于数学推导，统一用 `side=right, mat=R^T`）：

| 分类 | 含义 | 逆变换 |
|------|------|--------|
| `right_input` | right 旋转权重模块，input 在右旋空间（前一个右旋的 output） | 对 input 做 `x = x' @ R^T` |
| `right_output` | pre_run 的 right 旋转模块（如 embed_tokens），output 在右旋空间 | 对 output 做 `x = x' @ R^T` |
| `left_output` | left 旋转权重模块（包括 pre_run left），output 在右旋空间（`x @ W^T @ R`） | 对 output 做 `x = x' @ R^T` |

**对应到 MiniMax-M3**：

| 模块后缀 | 分类 | 说明 |
|---------|------|------|
| `.q_proj` / `.k_proj` / `.v_proj` / `.gate_proj` / `.up_proj` / `.gate` | right_input | right 旋转权重，input 在右旋空间 |
| `lm_head` | right_input | right 旋转权重，input 在右旋空间 |
| `embed_tokens` | right_output | pre_run right，输出在右旋空间（旋转空间的源头） |
| `.o_proj` / `.down_proj` | left_output | left 旋转权重，output 在右旋空间 |
| `merge_linear_2` | left_output | pre_run left，输出在右旋空间 |

**关键**：left 旋转模块的 output 也在右旋空间（`y = x @ W^T @ R`），所以逆变换统一用 `x = y @ R^T`（side=right），不是用 R 左乘。
