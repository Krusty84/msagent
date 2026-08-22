# 多 Matmul 独立 TileShape 优化（Decode Attention）

## 基本信息
- 算子类别：matmul
- DSL/框架：pypto
- 类型：非PR（内部案例库，cannbot-skills 官方调优案例库）
- 来源可信度：官方文档（cann/cannbot-skills 仓 `ops/pypto-op-perf-tune` 调优案例库，含 3 轮迭代失败分析与实测数字）

## 链接
- PR/出处链接：https://gitcode.com/cann/cannbot-skills/blob/master/ops/pypto-op-perf-tune/tune-frontecases/per-matmul-tile-shapes.md（ 已验证可达，验证日期 2026-08）
- 优化代码查看：非代码 MR 类案例，无 diff/files 页；出处链接即为案例原文，内含完整优化前后代码与迭代记录

## 问题与瓶颈
Decode Attention 算子含 3 个 shape 特征不同的 matmul（Q@K^T：M=4/K=128/N=2048；attn@V：M=4/K=2048/N=128；output_proj：M=1/K=4096/N=4096）。原实现用统一的 `set_cube_tile_shapes([16,256],[128,256],[256,256])` 应用于所有 matmul，部分 matmul 的 L1 tile 超过实际轴长（K=128 时 kL1=256、N=128 时 nL1=256），浪费 L1 空间。属开箱调优（PHASE_FRONTEND）阶段案例。

## 优化方法（理论手段）
每个 matmul 前独立设置 tile——小轴不切（L0=L1=实际值）、大轴用大 tile 减少任务数。机制：

1. **L1 不超过实际轴长**：kL1=256 而 K 实际只有 128 时没有意义，纯属浪费 L1 容量；
2. **M 极小时 mL1 也要小**：M=4/1 时 mL1=256 会挤占 K/N 大轴的 L1 空间（迭代 2 实测 +7% 回退证明）；
3. **不同 shape 的 matmul 最优 tile 不同**，统一配置无法按各自特征优化；
4. 约束：`kL0 <= kL1 && kL1 % kL0 == 0`，违反会编译失败；分设时先设统一值确认编译通过再分别调整。

## 性能对比
| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 耗时（统一 tile 基线 → 分设 mL1=16） | 257.12 us | 237.12 us | -7.8% |
| 耗时（分设但 mL1=256，回退对照） | 257.12 us | 275.08 us | +7% 回退 |
| 累计耗时（含前置优化） | 439.54 us | 237.12 us | -46.1% |
| 精度（Max difference） | 0.000031 | 0.000031 | 无变化 |

## 适用范围与警示
- 适用：同一算子内多个 shape 特征差异大的 matmul（如 Decode Attention）。
- 警示：分设 tile 需先设统一值确认编译通过再分别调整；M 极小维度不要盲目给大 mL1（实测回退）；`kL0 <= kL1 && kL1 % kL0 == 0` 约束违反会编译失败。
