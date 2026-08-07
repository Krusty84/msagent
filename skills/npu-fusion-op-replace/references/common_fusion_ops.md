# 常用融合算子详细参考

本文件是 SKILL.md §1.1 表格的展开。**写替换代码前必读对应小节**——这些算子都有容易踩错的地方。

## 目录

1. [RMSNorm → npu_rms_norm](#1-rmsnorm--npu_rms_norm)
2. [SwiGLU → npu_swiglu](#2-swiglu--npu_swiglu)
3. [Rotary Position Embedding → npu_rotary_mul](#3-rotary-position-embedding--npu_rotary_mul)
4. [Attention → npu_fusion_attention](#4-attention--npu_fusion_attention)
5. [Grouped Matmul（MoE）](#5-grouped-matmulmoe)

---

## 1. RMSNorm → npu_rms_norm

最简单、收益稳定的替换。直接替换手写 RMSNorm 的 forward。

文档：https://gitcode.com/Ascend/op-plugin/blob/26.1.0/docs/zh/custom_APIs/torch_npu/（beta）torch_npu-npu_rms_norm.md

### 识别 pattern

手写 RMSNorm 通常长这样（各种变体都算）：

```python
def forward(self, x):
    output = self._norm(x.float()).type_as(x)
    return output * self.weight
```

或展开形式：

```python
input_dtype = hidden_states.dtype
hidden_states = hidden_states.to(torch.float32)
variance = hidden_states.pow(2).mean(-1, keepdim=True)
hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
output = (self.weight * hidden_states).to(input_dtype)
```

### 替换后

```python
def forward(self, x):
    if _HAS_TORCH_NPU and str(x.device).startswith("npu"):
        return torch_npu.npu_rms_norm(x, self.weight, epsilon=self.eps)[0]
    output = self._norm(x.float()).type_as(x)
    return output * self.weight
```

### 踩坑点

- **返回值是 tuple，取 `[0]`** 才是结果张量。直接用返回值会拿到 tuple 而非 tensor。
- FSDP 场景可能返回 FP32 hidden states 但 weight 留在 BF16。`aclnnRmsNorm` 支持 FP32 gamma，对 FP32/BF16 输入都成立，可显式 `.float()`：

```python
output = torch_npu.npu_rms_norm(hidden_states, self.weight.float(), epsilon=self.variance_epsilon)[0]
```

---

## 2. SwiGLU → npu_swiglu

将 `SiLU + 门控乘法` 替换为单个融合算子。

文档：https://gitcode.com/Ascend/op-plugin/blob/master/docs/zh/custom_APIs/torch_npu/（beta）torch_npu-npu_swiglu.md

### 识别 pattern

```python
# 原始：a1 * silu(a2)
intermediate = a1 * F.silu(a2)
```

### 替换后

```python
# NPU 优化：concat 顺序 [a2, a1] 使得 SiLU(a2) * a1
gate_up = torch.cat([a2, a1], dim=-1)
intermediate = torch_npu.npu_swiglu(gate_up, dim=-1)
```

### 踩坑点（最关键）

**concat 顺序必须正确。** `npu_swiglu(x, dim=-1)` 的语义是：对前半做 SiLU，乘以后半。所以：

- 原始代码 `a1 * silu(a2)` → 需 concat `[a2, a1]`，让前半是 a2（被 SiLU），后半是 a1。
- 如果原始代码是 `silu(a1) * a2` → concat `[a1, a2]`。

判断方法：**谁被 silu 包，谁放前半。** 反了语义就错了，但不会报错，要靠精度验证抓出来。

---

## 3. Rotary Position Embedding → npu_rotary_mul

替换手写的旋转位置编码计算。

文档：https://gitcode.com/Ascend/op-plugin/blob/master/docs/zh/custom_APIs/torch_npu/torch_npu-npu_rotary_mul.md

### 识别 pattern

```python
def apply_rotary_pos_emb(t, freqs):
    cos, sin = freqs
    rot_dim = cos.shape[-1]
    t_, t_pass_ = t[..., :rot_dim], t[..., rot_dim:]
    t_ = (t_.float() * cos) + (_rotate_half(t_.float()) * sin)
    return torch.cat((t_, t_pass_), dim=-1).type_as(t)
```

### 替换后

```python
def apply_rotary_pos_emb(t, freqs):
    cos, sin = freqs
    if _HAS_TORCH_NPU and str(t.device).startswith("npu"):
        rot_dim = cos.shape[-1]
        t_, t_pass_ = t[..., :rot_dim], t[..., rot_dim:]
        cos = cos.expand_as(t_)
        sin = sin.expand_as(t_)
        output = torch_npu.npu_rotary_mul(t_, cos, sin).type_as(t)
        if t_pass_.shape[-1] > 0:
            return torch.cat((output, t_pass_), dim=-1)
        return output
    # fallback to original
    rot_dim = cos.shape[-1]
    t_, t_pass_ = t[..., :rot_dim], t[..., rot_dim:]
    t_ = (t_.float() * cos) + (_rotate_half(t_.float()) * sin)
    return torch.cat((t_, t_pass_), dim=-1).type_as(t)
```

### 踩坑点

- **cos/sin 必须 `expand_as(t_)`** 到与输入相同 shape，否则 broadcast 不对。
- **处理 t_pass_**（超出旋转维度的部分）：npu_rotary_mul 只处理 rot_dim 维度，超出部分要 concat 回来。判断 `t_pass_.shape[-1] > 0`。
- `npu_rotary_mul` 内部实现是 `x * cos + rotate_half(x) * sin`，其中 `rotate_half` 是 `[-x[D//2:], x[:D//2]]`。确认手写实现的 `_rotate_half` 语义一致，否则语义不等价。

---

## 4. Attention → npu_fusion_attention（最复杂，收益最大）

替换手写的 `Q@K^T → mask → softmax → @V` 为单个融合算子。

文档：https://gitcode.com/Ascend/op-plugin/blob/master/docs/zh/custom_APIs/torch_npu/torch_npu-npu_fusion_attention.md

### 识别 pattern

任何手写 `scale * Q @ K^T → mask → softmax → @V` 的代码，都是候选。

### 替换后

```python
# query/key/value shape: (B, S, H, D) — 即 BSND 格式
scale = 1.0 / math.sqrt(head_dim)

# 构建 causal mask（bool，True = mask out）
seq_len_q = query.shape[1]
seq_len_k = key.shape[1]
causal_mask = torch.triu(
    torch.ones(seq_len_q, seq_len_k, dtype=torch.bool, device=query.device),
    diagonal=seq_len_k - seq_len_q + 1,
)
# 合并 padding mask（如果有）
if attention_mask is not None:
    # attention_mask 是 float 加法 mask (0.0=attend, -inf=mask)，转为 bool
    pad_mask = (attention_mask.squeeze(1).squeeze(1) < -1.0)
    causal_mask = causal_mask.unsqueeze(0) | pad_mask.unsqueeze(-2)
    atten_mask = causal_mask.unsqueeze(1)  # (B, 1, S_q, S_kv)
else:
    atten_mask = causal_mask.unsqueeze(0).unsqueeze(0)  # (1, 1, S_q, S_kv)

npu_out = torch_npu.npu_fusion_attention(
    query, key, value,
    num_heads,
    input_layout="BSND",
    pse=None,
    atten_mask=atten_mask,
    scale=scale,
    pre_tockens=65536,
    next_tockens=0,
    keep_prob=1.0 if not training else 1.0 - dropout_p,
)[0]
context_layer = npu_out.flatten(2, 3).contiguous()  # (B, S, H*D)
```

### 踩坑点（最多，务必逐条核对）

- **必须使用显式 causal mask。** 仅靠 `pre_tockens/next_tockens=0` 不足以正确实现因果注意力。实测去掉显式 mask 后 logits 余弦相似度从 0.999 暴跌至 0.5~0.8——精度验证能抓出来，但别等验证，直接加。
- **mask 语义**：`atten_mask` 中 `True` 表示"屏蔽"（不参与注意力），`False` 表示"参与"。**与 PyTorch 的 float additive mask 相反**（float mask 里 0.0=attend, -inf=mask）。从 float mask 转 bool 时注意取反：`(attention_mask < -1.0)` 得到 bool mask。
- **padding mask 合并**：Transformer 层传入的 `attention_mask` 通常是 float 格式，需转为 bool 后与 causal mask 做 **OR** 合并（`|`），不是相加。
- **input_layout="BSND"**：B=batch, S=seq_len, N=num_heads, D=head_dim。确认 query/key/value 从 `_split_heads` 出来的 shape 是 `(B, S, H, D)` 而非 `(B, H, S, D)`。layout 填错 shape 解释就错了。
- **返回值是 tuple，取 `[0]`**。
- **decode 阶段（seq_len_q=1）**：`diagonal = S_kv - 1 + 1 = S_kv`，`torch.triu(..., diagonal=S_kv)` 全零，即不屏蔽任何 token，行为正确。不用特别处理。

### v2 变体（`npu_fusion_attention_v2`）

较新版本的 torch_npu 提供 `npu_fusion_attention_v2`，API 与 v1 有差异。如果环境里有 v2，参考实际签名（用 `scripts/torch_npu_api_ref.py show npu_fusion_attention_v2` 查；本地文档没有就查在线文档），主要区别：

- 参数名用 `shape_order`（如 `"BNSD"`）而非 v1 的 `input_layout`；`BNSD` = B,N(heads),S,D。
- 用 `sparse_mode` 控制 mask 类型（如 `sparse_mode=4` 为因果 mask），可传一个压缩的 `[2048,2048]` bool triu mask 而非完整 atten_mask。
- 部分模型（如带 attention sink 的 gpt_oss）有 `sink=` 参数，把 sinks 传进去。
- 返回值仍是 tuple，取 `[0]`。

**选 v1 还是 v2**：以当前环境实际可用的 API 为准，查 `torch_npu_api_ref.py` 或在线文档，别凭记忆。两者语义目标一致（融合 attention），参数和 mask 处理方式不同，照实际签名调用。

---

## 5. Grouped Matmul（MoE）

文档：https://gitcode.com/Ascend/op-plugin/blob/master/docs/zh/custom_APIs/torch_npu/torch_npu-npu_grouped_matmul.md

### 识别 pattern

Look for MoE expert loops or batched expert matmuls——这种"按专家循环做 matmul"的模式：

```text
select tokens for expert e
tokens @ W1[e]
activation
hidden @ W2[e]
multiply routing probability
index_add output
```

### 确认事项（替换前必须核实）

- expert 归属（EP / FSDP / TP 哪种并行下）
- checkpoint weight layout
- bias 处理
- 空 expert（某些专家本轮没分到 token）
- token-count dtype
- routing-weight 应用方式

### 替换后

Use `moe_grouped_gemm` to choose a fused expert module. 典型本地路径：

```text
npu_moe_token_permute(hidden, expert_ids)
tokens_per_expert = histogram(expert_ids)
Ops.gmm or npu_grouped_matmul(tokens, W1, tokens_per_expert)
activation
Ops.gmm or npu_grouped_matmul(hidden, W2, tokens_per_expert)
npu_moe_token_unpermute(output, row_map, routing_weights)
```

### 踩坑点

- MoE 替换涉及并行策略和权重布局，比单个算子替换复杂得多。**确认事项没核实清楚前不要动手**，否则容易在 expert 归属或 routing weight 上出错。
- 空 expert 的 `tokens_per_expert=0` 要确认 grouped_matmul 能正确处理（不同版本行为可能不同），查 `scripts/torch_npu_api_ref.py show npu_grouped_matmul` 的约束说明。
