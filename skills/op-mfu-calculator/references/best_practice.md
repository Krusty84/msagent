# 算子 MFU 估算最佳实践

本指南适用于使用 `op-mfu-calculator` 对单个算子或一组同构算子进行**理论 FLOPs 估算 + 实测耗时**的 MFU 计算。它用于快速判断算子是否接近设备计算上限；需要基于 profiling 数据得到 kernel 级归因时，应使用 `op-mfu-profiler`。

## 1. 先明确统计边界

MFU 的可信度首先取决于分子、分母和时间是否描述同一件事：

$$
\mathrm{MFU}=\frac{\mathrm{FLOPs}}{t\;(s)\times\mathrm{Peak}\;(\mathrm{FLOPs/s})}
$$

每次计算都应明确以下边界：

| 项目 | 必须确认 | 常见错误 |
| --- | --- | --- |
| 计算范围 | 是单次调用、N 次调用，还是一个完整 iteration | FLOPs 为单次、时间却使用整步时间 |
| 时间范围 | 使用 device 侧算子执行时间，单位为 ms 或 us | 使用包含数据加载、同步或其他算子的 host 端墙钟时间 |
| 精度与峰值 | 峰值必须对应芯片型号、实际输入精度和计算单元 | 用 FP16/BF16 峰值计算 FP32 或 FP8 算子 |
| 硬件范围 | 单卡时间对应单卡峰值；并行聚合时间对应聚合峰值 | 将单卡峰值与多卡汇总 FLOPs 混用 |
| 算子范围 | FLOPs 公式覆盖的工作与计时范围一致 | 将 bias、激活、transpose 等额外算子计入一侧 |

当上述任意一项不明确时，不应输出看似精确的 MFU 百分比，而应标为“待确认”。

## 2. 采集可靠的耗时

1. 优先使用 profiler 中的 device kernel duration；若只有框架计时，应在计时区间前后同步，并说明结果为端到端近似值。
2. 区分冷启动与稳态。先完成 warm-up，再采集多个稳定样本；报告中使用均值或中位数，并同时给出样本数。
3. 对短算子尤其谨慎。短耗时容易受 launch、同步和计时精度影响，低 MFU 不一定代表计算实现差。
4. 多流或异步执行时，不能把 host 侧提交耗时当作 kernel 执行时长；应从对应 stream 的 device 时间线取值。
5. 记录原始单位。推荐原始保存 `duration_ns` 或 `duration_us`，仅在展示时转换为 ms，避免重复换算造成 1000 倍误差。

## 3. 正确计算 FLOPs

### GEMM / Matmul

对于 $(M,K)\times(K,N)$，单次 GEMM 的 FLOPs 为：

$$
2MKN
$$

批量矩阵乘需乘以 batch 数 $B$。线性层可展开为 $M=B\times L$、$K=D_{in}$、$N=D_{out}$。计算前确认转置仅改变逻辑维度映射，不会让收缩维度不匹配。

### FlashAttention

先按 `input_layout` 规范化理解维度；BNSD/BSND/BSH/SBH 统一为 $(B,N,S,D)$，TND 必须使用 `actual_seq_qlen` 和 `actual_seq_kvlen` 恢复每个样本的真实长度。不要将 padding 后的最大长度直接代替 varlen 的实际长度。

完整 attention 的计算量为：

$$
2B N S_q S_k(D_q+D_k)
$$

`sparse_mode` 会改变有效 attention 区域。必须从实际算子参数读取 `sparse_mode`，再按 `SKILL.md` 中与布局、序列长度匹配的分支计算；未知时只能给出完整 attention 上界，不能把上界当作最终 MFU。

### 非标准算子

公式优先级如下：

1. 算子官方定义或实现源码；
2. 已验证的同类算子公式；
3. 将算子拆成已知基本操作后逐项求和。

无法可靠推导时，明确说明“不计算 MFU”，不要用输出张量元素数或经验系数猜测 FLOPs。对于融合算子，需先明确是按融合前逻辑工作量，还是按实际 device kernel 工作量统计，并在报告中固定一种口径。

## 4. 统一单位并做交叉校验

推荐按下列顺序计算，避免在同一公式内混用 ms、ns、TFLOPs/s 和 FLOPs/s：

$$
\begin{aligned}
t_s &= \frac{t_{ms}}{1000}\\
\mathrm{Achieved\ TFLOPs/s} &= \frac{\mathrm{FLOPs}}{t_s\times10^{12}}\\
\mathrm{MFU} &= \frac{\mathrm{Achieved\ TFLOPs/s}}{\mathrm{Peak\ TFLOPs/s}}
\end{aligned}
$$

计算完成后至少执行三项检查：

- **量纲检查**：最终 MFU 无单位，实际吞吐的单位是 TFLOPs/s。
- **数量级检查**：MFU 大于 100% 时，优先排查时间单位、重复次数、峰值精度和多卡口径；除非有明确的峰值定义差异，否则不要直接接受结果。
- **反向检查**：用 `Peak TFLOPs/s × MFU × duration_s` 回算 FLOPs，应与原始 FLOPs 一致。

## 5. 解释结果时避免过度结论

MFU 是计算吞吐指标，不是端到端性能的唯一指标。低 MFU 可能来自小 shape、低并行度、内存带宽、访存不连续、动态 shape、稀疏掩码、算子融合不足或 launch 开销；高 MFU 也不代表整网性能最优。

建议按同一算子、同一精度下的 shape 分组对比，而不是跨算子简单排名。MFU 低于 20%、30%--60%、高于 70% 可分别作为“需要进一步分析”“中等偏上”“接近计算上限”的经验信号，而非跨硬件、跨算子通用的验收阈值。

当目标是优化时，先根据 profiling 判断计算受限、带宽受限还是调度受限，再选择动作：增大有效矩阵维度或 batch、改善数据布局、减少 padding、采用合适的融合实现，或降低小算子的 launch 次数。不要仅因 MFU 低就盲目调整 batch 或改变算法。

## 6. 建议交付格式

每次输出都保留可复算的输入和计算过程：

```text
统计对象：<算子名；单次/聚合次数>
硬件与精度：<型号；dtype；Peak TFLOPs/s；单卡/多卡>
输入：<各张量 shape；layout/sparse_mode（如适用）>
耗时：<数值 + 单位；device/host；样本数；均值或中位数>
FLOPs 公式：<公式与代入值>
实际吞吐：<Achieved TFLOPs/s>
MFU：<百分比>
口径与限制：<是否包含融合、padding、通信或其他近似>
下一步：<需要的 profiling 字段或建议的优化验证>
```

这样可以让他人独立复算，也能在 shape、精度或硬件变化后快速比较结果。
