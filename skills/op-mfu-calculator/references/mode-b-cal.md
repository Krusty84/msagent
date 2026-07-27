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

当用户只给出算子名（或代码片段中的算子名），需要查找该算子的 FLOPs 公式时，按以下优先级依次处理：

1. **先在下方"常见算子 FLOPs 公式表"中查找**。找到则直接使用。

2. **询问用户能否提供源码**：公式表中未找到时，询问用户是否有该算子的实现代码或源码链接，等待用户回应：
   - **用户提供了源码**：根据实现代码手动推导 FLOPs 公式
   - **用户无法提供**：尝试推导 FLOPs 公式，并注明此为估算值

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
| Ascend A3 | FP16/BF16 | ≈ 354 TFLOPs/s |

如果用户没有给出确切的峰值算力，先询问具体型号和精度模式，或使用上表典型近似值并明确声明。

### 常见算子 FLOPs 公式表

| 算子 | 特殊维度说明 | FLOPs 公式 |
| ---- | ------------ | ---------- |
| `torch.mm` | - | `2 * M * K * N` |
| `torch.bmm` | batch=B | `2 * B * M * K * N` |
| `torch.matmul` | 解析 batch_shape | `2 * prod(batch_shape) * M * K * N` |
| `torch.nn.functional.linear` | M=prod(shape[:-1]) | `2 * prod(shape[:-1]) * D_out * D_in` |
| `torch.addmm` | - | `2 * M * K * N` |
| `torch_npu.npu_transpose_batchmatmul` | 按 perm_x1/perm_x2 解析 | `2 * B * M * K * N` |
| `torch_npu.npu_grouped_matmul` | 多组累加 | `sum_i(2 * M_i * K_i * N_i)` |
| `torch_npu.npu_quant_matmul_gelu` | - | `2 * total_m * K * N` |
| `torch_npu.npu_grouped_matmul_swiglu_quant_v2` | - | `2 * M * K * N` |
| `torch_npu.npu_all_gather_base_mm` | AllGather 后 | `2 * (m_local * world_size) * K * N` |
| `torch_npu.npu_alltoallv_gmm` | 路由专家+共享专家 | `2 * T_route * H1 * N1 [+ 2 * BS * H2 * N2]` |
| `torch_npu.npu_gmm_alltoallv` | 同上 | 同上 |
| `torch_npu.npu_fusion_attention` | TND/Common/sparse_mode 见 `operator_mfu_instruct.md` | `2 * score_elems * (D_q + D_k)` |
| `torch_npu.npu_fused_infer_attention_score` | 支持 GQA | `2 * score_elems * (D_q + D_k)` |
| `torch_npu.npu_block_sparse_attention` | block mask | `2 * score_elems * (D_q + D_k)` |