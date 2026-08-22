# {模型名称} 训推算子差异完整报告

> **模型**: {model_id} (layers={num_layers}, hidden={hidden_size}, heads={num_heads}, GQA={num_kv_heads}, {activation_type})
> **训练路径**: {train_backend} + {bridge_info} → `{actor_strategy_config}`
> **推理路径**: {infer_backend} {infer_version} + {infer_plugin} {plugin_version} → `{rollout_name_config}`
> **脚本**: `{script_path}`
> **报告时间**: {date} | **数据采集**: 完整 RL 脚本运行时采集（msprof level1），非静态扫描

---

## 算子证据来源

| 路径 | 数据源 | DB 文件 | 算子数 |
|------|--------|--------|--------|
| **训练 (Megatron)** | e2e profiler | `profiler_output/e2e/.../ascend_pytorch_profiler_0.db` | {train_op_count} |
| **推理 (vLLM)** | rollout discrete profiler | `profiler_output/agent_loop_rollout_replica_0/.../ascend_pytorch_profiler_0.db` | {infer_op_count} |

> **说明**: 以上算子均来自完整 RL 训练脚本运行时采集的 CANN 级 profiling 数据（同一 step 内采集，训练路径用 e2e profiler，推理路径用 discrete=True 分离采集）。非静态源码扫描，非独立离线运行。

---

## 差异等级图例

| 标记 | 颜色 | 含义 | 精度风险 |
|:----:|:----:|------|:--------:|
| 🔴 | <span style="color:red">红</span> | **高差异** — 融合 vs 非融合，一方单 kernel 另一方多 kernel（如 Attention） | 高 |
| 🟠 | <span style="color:orange">橙</span> | **中差异** — 同类操作但融合程度不同，或不同后端实现同一数学运算 | 中 |
| 🟡 | <span style="color:gold">黄</span> | **低差异** — 实现方式不同但数学语义等价，或仅单侧存在且有等价替代 | 低 |
| 🟢 | <span style="color:green">绿</span> | **无差异** — 相同算子，或训练/推理单侧独占且无等价替代 | 极低 |

---

## 一、训练路径算子（Megatron Actor+Ref 前向传播）

| 算子 | 调用次数 | 用途分类 |
|------|----------|----------|
| {train_op_1} | {cnt_1} | {category_1} |
| {train_op_2} | {cnt_2} | {category_2} |
| ... | ... | ... |

---

## 二、推理路径算子（vLLM Rollout 生成）

| 算子 | 调用次数 | 用途分类 |
|------|----------|----------|
| {infer_op_1} | {cnt_1} | {category_1} |
| {infer_op_2} | {cnt_2} | {category_2} |
| ... | ... | ... |

---

## 三、核心差异：训练 vs 推理算子对比表

| 维度 | 训练 (Megatron) | 推理 (vLLM) | 差异类型 | 等级 |
|------|-----------------|-------------|----------|:----:|
| **注意力** | `{train_attn_op}` ({train_attn_count}×) | **`{infer_attn_op}`** ({infer_attn_count}×) | 算子不同 | 🔴 |
| **残差+归一化** | `Add` + `RmsNorm` | **`{infer_norm_fused_op}`** | 融合程度不同 | 🟠 |
| **激活函数** | `{train_act_op}` | **`{infer_act_op}`** | 融合程度不同 | 🟠 |
| **RoPE** | `Sin` + `Cos` | **`{infer_rope_op}`** | 后端实现不同 | 🟠 |
| **线性投影** | `MatMulV2` | `MatMulV2` | 相同 | 🟢 |
| **KV Cache** | 无 | `{infer_cache_op}` | 推理独有 | 🟡 |
| **反向传播** | `{train_bw_ops}` | 无 | 训练独有 | 🟡 |
| **采样** | 无 | `{infer_sample_ops}` | 推理独有 | 🟡 |

---

## 四、代码调用栈详情

### 4.1 🔴 注意力算子差异

<details>
<summary>🔴 训练: {train_attn_op} → 推理: {infer_attn_op} — 调用栈详情</summary>

**训练路径（Megatron）**:
```
verl/workers/actor/megatron_actor.py → compute_log_prob()
  → megatron/core/transformer/transformer_layer.py → TransformerLayer.forward()
    → megatron/core/transformer/attention.py → SelfAttention.forward()
      → megatron/core/transformer/dot_product_attention.py → FlashAttention
        → CANN: FlashAttentionScore
```

**推理路径（vLLM）**:
```
verl/workers/rollout/vllm_rollout/vllm_rollout_spmd.py → generate_sequences()
  → vllm/worker/model_runner.py → ModelRunner.execute_model()
    → vllm_ascend/attention/ascend_attention.py → AscendAttentionBackendImpl
      → CANN: FusedInferAttentionScore
```

</details>

### 4.2 🟠 残差+归一化融合差异

<details>
<summary>🟠 训练: Add + RmsNorm → 推理: AddRmsNormBias — 调用栈详情</summary>

**训练路径**:
```
megatron/core/transformer/transformer_layer.py → TransformerLayer.forward()
  → CANN: Add (残差连接)
  → megatron/core/transformer/torch_norm.py → WrappedTorchNorm
    → CANN: RmsNorm
```

**推理路径**:
```
vllm_ascend/layers/layernorm.py → AscendRMSNorm.forward()
  → CANN: AddRmsNormBias (Add + RmsNorm + Bias 三合一融合)
```

</details>

### 4.3 🟠 激活函数融合差异

<details>
<summary>🟠 训练: Swish (独立SiLU) → 推理: SwiGlu (SiLU+Gate融合) — 调用栈详情</summary>

**训练路径**:
```
megatron/core/transformer/mlp.py → MLP.forward()
  → CANN: Swish (独立 SiLU 激活)
```

**推理路径**:
```
vllm_ascend/layers/activation.py → AscendSiluAndMul.forward()
  → CANN: SwiGlu (SiLU + Gate逐元素乘法 融合)
```

</details>

### 4.4 🟠 RoPE 算子差异

<details>
<summary>🟠 训练: Sin+Cos (独立正余弦) → 推理: _triton_rope (Triton融合) — 调用栈详情</summary>

**训练路径**:
```
megatron/core/transformer/attention.py → SelfAttention.forward()
  → megatron/core/rotary_pos_embedding.py → apply_rotary_pos_emb()
    → CANN: Sin, Cos (独立正余弦计算 + Mul)
```

**推理路径**:
```
vllm_ascend/layers/rotary_embedding.py → AscendRotaryEmbedding.forward()
  → Triton kernel: _triton_rope (正余弦+旋转 单 kernel 融合)
```

</details>

---

### 6.3 证据闭环

- ✅ 所有算子数据来自完整 RL 脚本运行时 profiling（msprof level1），非静态源码扫描
- {unverified_items}

