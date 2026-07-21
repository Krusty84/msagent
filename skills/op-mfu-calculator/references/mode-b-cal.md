# 模式 B：单算子 FLOPs / MFU 计算

> 当用户只关心某个具体算子、不涉及 Profiling 数据时，按此模式处理。

## 前置判断

根据用户提供的信息，走不同分支：

| 用户提供的信息 | 处理方式 |
| -------------- | -------- |
| **没有给出耗时** | 只计算 FLOPs，给出公式和计算结果，不计算 MFU |
| **给出了耗时 + 维度** | 计算 FLOPs → 计算 Achieved TFLOPs/s → 计算 MFU |

## 基本概念

- **MFU 定义**
  ```
  MFU = 实际计算产生的 FLOPs / 同时间内硬件理论可执行的 FLOPs
     = Achieved FLOPs / Peak FLOPs
  ```
- **单位约定**
  - FLOPs：浮点运算次数
  - TFLOPs/s：每秒万亿次浮点运算
  - 实际 FLOPs / 执行时间 = Achieved FLOPs/s
  - Achieved TFLOPs/s = Achieved FLOPs/s / 1e12

## 第一步：算子查找与 FLOPs 获取

根据用户给出的算子名，判断该算子属于以下哪种情况，按优先级依次处理：

1. **对应公式表**：如果算子名能明确对应到下方"算子 FLOPs 公式表"中的某个条目（例如用户说"matmul"即对应 `torch.matmul`），直接使用表中对应的 FLOPs 计算公式。

2. **属于 GEMM 或 Attention 大类**：如果算子名不在公式表中，但明显属于矩阵乘（GEMM）或 Attention 类别（如 `aclnnMatmul_MatMulV3Common_MatMulV3` 显然是 GEMM，`aclnnFlashAttentionScore` 显然是 Attention），按下方公式推导。

3. **无法归类**：前两步都无法确定时，去 `https://gitcode.com/Ascend/op-plugin` 检索算子源码，从实现中提取 FLOPs 计算方式。如果 op-plugin 也无法检索到有效代码，直接告知用户当前无法确定该算子的 FLOPs 公式。

### Matmul / GEMM FLOPs 计算

当用户提到矩阵乘/线性层/attention 中的 matmul 时，按如下规则估算 FLOPs：

- **标准矩阵乘 (GEMM)**：形状 `(M, K)` 与 `(K, N)`
  ```
  FLOPs = 2 x M x N x K
  ```
  这里的 2 来自「一次乘法 + 一次加法」。

- **带 batch 维度的 matmul**：形状 `(B, M, K)` 与 `(B, K, N)`
  ```
  FLOPs = 2 x B x M x N x K
  ```

- **常见情形举例**（可直接类比）：
  - 线性层：输入 `(B, L, D_in)`，权重 `(D_in, D_out)` → `M = B x L, K = D_in, N = D_out`
  - Attention QK^T：`Q=(B, H, L_q, D_h), K=(B, H, L_k, D_h)` → `B' = B x H, M = L_q, N = L_k, K = D_h`

### FlashAttention FLOPs 计算

当用户提到 FlashAttention 算子时，根据输入布局（layout）和稀疏模式（sparse_mode）计算 FLOPs。Attention FLOPs 只统计 `Q @ K^T` 和 `P @ V` 两次矩阵乘，不统计 softmax、mask、dropout 等后处理。

**输入布局**：统一转换为 `(B, N, S, D)` 格式：
- **BNSD**：`(B, N, S, D)` → 直接使用
- **BSND**：`(B, S, N, D)` → 转换为 `(B, N, S, D)`
- **BSH**：`(B, S, D)` → 转换为 `(B, 1, S, D)`（单头）
- **SBH**：`(S, B, D)` → 转换为 `(B, 1, S, D)`（单头）
- **TND**：`(T, N, D)` → varlen 场景，需要实际序列长度信息

**TND Layout**（需要 `actual_seq_qlen` 和 `actual_seq_kvlen`）：
1. 解析序列长度：`q_lens[i] = actual_seq_qlen[i] - actual_seq_qlen[i-1]`（去末尾 0）
2. 序列工作量：`acl_seq_workload = sum_i (q_lens[i] x kv_lens[i])`
3. FLOPs：`2 x N x (D_q + D_k) x acl_seq_workload`

**Common Layout**（BNSD/BSND/BSH/SBH，需要 `sparse_mode`）：
- 基础：`full_attention = 2 x q_b x q_n x q_s x k_s x (q_d + k_d)`
- sparse_mode == 0：`FLOPs = full_attention`
- sparse_mode == 2/3，q_s == k_s（causal）：`FLOPs = full_attention x 0.5`
- sparse_mode == 2，q_s > k_s：`FLOPs = full_attention x (q_s x k_s - k_s^2 / 2) / (k_s^2)`
- sparse_mode == 2，q_s < k_s：`FLOPs = full_attention x (q_s^2 / 2) / (q_s x k_s)`
- sparse_mode == 3，q_d > k_d：`FLOPs = full_attention x (k_s^2 / 2) / (q_s x k_s)`
- sparse_mode == 3，q_d < k_d：`FLOPs = full_attention x (q_s x k_s - q_s^2 / 2) / (q_s x k_s)`

## 第二步：计算 MFU

当用户希望你计算某个算子的 MFU 时，严格按照以下步骤：

1. **确认信息是否充分**
   向用户要齐以下信息（如果缺失就明确提出）：
   - 算子类型（例如 matmul / GEMM / FlashAttention 等）。
   - 参与运算的张量维度（包含 batch / head / sequence 等关键维度）。
   - 单次算子执行的耗时（例如毫秒 ms）。
   - 硬件单卡的理论峰值算力（例如 312 TFLOPs/s，注明是 FP16/BF16 还是 FP8 等）。

2. **计算算子 FLOPs**
   - 根据算子类型和维度，用上面的公式算出 **单次调用的 FLOPs**。
   - 如果用户给了「每迭代包含多少次该算子」或「多个相同算子」，先计算单次，然后乘以调用次数。

3. **计算 Achieved FLOPs/s**
   - 先换算执行时间到秒，例如：`t_s = time_ms / 1000`。
   - Achieved FLOPs/s = FLOPs / t_s。
   - 再换算到 TFLOPs/s：Achieved TFLOPs/s = Achieved FLOPs/s / 1e12。

4. **计算 MFU**
   - MFU = Achieved TFLOPs/s / Peak TFLOPs/s。
   - 最终给出百分比形式，例如 0.42 → 42%。

5. **解释结果**
   - 简要说明这个 MFU 代表的含义，例如：
     - 低于 20%：通常算子远未吃满算力，可能受内存带宽、launch overhead、shape 不规则等影响。
     - 30%–60%：中等偏上水平，许多通用工作负载大致在这个区间。
     - 高于 70%：算子形状、并行度和实现都比较接近设备上限。

## 第三步：输出结果

按如下结构作答：

1. 开头说明：（本回答基于 msprof-analyze-mfu-calculator Skill 的 MFU 计算规范）
2. **先复述输入信息**（算子类型、张量维度、时间、峰值算力）
3. **列出关键公式**（FLOPs、Achieved TFLOPs/s、MFU），代入具体数字展示中间计算过程
4. **给出最终 MFU 数值**（保留 2–3 位有效数字，百分比形式）
5. **简单分析**产生这个 MFU 的可能原因或优化方向

如果信息不全，不要瞎猜，而是明确列出还缺哪些数字，并给出如何从 profiler / 日志中拿到这些信息的建议。

## 附录：常用参考

### 常见芯片理论峰值算力

| 芯片型号 | 精度 | 峰值算力 |
| -------- | ---- | -------- |
| Ascend 910B1 | FP16/BF16 | ≈ 378.88 TFLOPs/s |
| Ascend 910B2 | FP16/BF16 | ≈ 353.89 TFLOPs/s |
| Ascend 910B3 | FP16/BF16 | ≈ 294.91 TFLOPs/s |
| Ascend 910B4 | FP16/BF16 | ≈ 270 TFLOPs/s |

如果用户没有给出确切的峰值算力，先询问具体型号和精度模式，或使用上表典型近似值并明确声明。

### 算子 FLOPs 公式表

统一口径：
- 矩阵乘按 multiply-add 计为两次操作，即 `2 * M * K * N`。
- 融合算子默认只统计核心矩阵乘或 Attention 主体。
- 通信、数据重排、transpose、bias、scale、mask、Softmax、dropout、量化/反量化和激活等融合后处理不额外计入 FLOPs。

| 算子 | FLOPs 计算逻辑 |
| ---- | -------------- |
| `torch.mm` | `2 * M * K * N`。 |
| `torch.bmm` | `2 * B * M * K * N`。 |
| `torch.matmul` | 根据向量、矩阵和 broadcast batch 维度解析后计算；通用矩阵场景为 `2 * prod(batch_shape) * M * K * N`。 |
| `torch.nn.functional.linear` | `2 * prod(input.shape[:-1]) * out_features * in_features`。 |
| `torch.addmm` | `2 * M * K * N`，只统计 `mat1 @ mat2`。 |
| `torch_npu.npu_all_gather_base_mm` | `2 * (m_local * world_size) * K * N`，只统计 AllGather 后的 GEMM。 |
| `torch_npu.npu_transpose_batchmatmul` | 先按 `perm_x1/perm_x2` 解析参与 GEMM 的 shape，再按矩阵乘计算；三维 Batch GEMM 场景为 `2 * B * M * K * N`。 |
| `torch_npu.npu_grouped_matmul` | 如果 `x` 和 `weight` 分组一一对应，计算 `sum_i(2 * M_i * K_i * N_i)`；如果一个 `x` 对应多个 `weight`，按 `group_list` 拆分 token 后累加各组 GEMM。 |
| `torch_npu.npu_quant_matmul_gelu` | `2 * total_m * K * N`，只统计量化矩阵乘主体。 |
| `torch_npu.npu_grouped_matmul_swiglu_quant_v2` | `2 * M * K * N`，只统计 Grouped GEMM 主体。 |
| `torch_npu.npu_alltoallv_gmm` | 路由专家 GMM 为 `2 * T_route * H1 * N1`；如果传入共享专家 `mm_x/mm_weight`，额外加 `2 * BS * H2 * N2`。 |
| `torch_npu.npu_gmm_alltoallv` | 路由专家 GMM 为 `2 * T_route * H1 * N1`；如果传入共享专家 `mm_x/mm_weight`，额外加 `2 * BS * H2 * N2`。 |
| `torch_npu.npu_fusion_attention` | 只统计 `Q @ K^T` 和 `P @ V`：`2 * score_elems * q_dim + 2 * score_elems * value_dim`。普通 layout 按 `input_layout` 解析 batch、head、seq 和 head_dim；`TND` layout 使用 `actual_seq_qlen/actual_seq_kvlen` 计算有效序列长度。 |
| `torch_npu.npu_fused_infer_attention_score` | 与 `npu_fusion_attention` 同一口径，支持 `num_heads` 和 `num_key_value_heads`。 |
| `torch_npu.npu_block_sparse_attention` | 只统计有效 block pair 中的 `Q @ K^T` 和 `P @ V`：`2 * score_elems * q_dim + 2 * score_elems * value_dim`，其中 `score_elems` 按 `block_sparse_mask` 中有效块的 `q_tokens * kv_tokens` 累加。 |

Attention 中 `score_elems` 表示实际参与 QK/PV 计算的 attention score 元素数量，已包含 batch 和 head。稠密场景为 `batch * head * q_seq * kv_seq`；因果或稀疏场景会按 `sparse_mode` 或 block mask 减少有效 score 元素数。
