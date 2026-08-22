# 大 Shape Matmul 分核布局优化（L2 命中率，优化点 S-13）

## 基本信息
- 算子类别：matmul
- DSL/框架：pypto
- 类型：非PR（内部案例库，cannbot-skills 官方调优案例库）
- 来源可信度：官方文档（cann/cannbot-skills 仓 `ops/pypto-op-perf-tune`；属深度调优 PHASE_SWIMLANE 阶段，前置条件为 S-2（核填充）已完成）

## 链接
- PR/出处链接：https://gitcode.com/cann/cannbot-skills/blob/master/ops/pypto-op-perf-tune/shared/optimization_catalog.md（Matmul 分核布局/L2 命中率条目，编号 S-13）（ 已验证可达，验证日期 2026-08）；另见 https://gitcode.com/cann/cannbot-skills/blob/master/ops/pypto-op-perf-tune/tune-swimlane/SKILL.md（§6）（ 已验证可达，验证日期 2026-08）
- 优化代码查看：非代码 MR 类案例，无 diff/files 页；上述出处链接原文含优化原理与典型收益描述

## 问题与瓶颈
大 shape Matmul（M、N、K 均较大）在 L2 命中率偏低、MTE2 带宽利用率不足时成为瓶颈。

## 优化方法（理论手段）
在 M、N 轴外层手动添加一层 loop，控制每轮 M 和 N 的计算范围，从而控制单轮次的分核数 mDim、nDim 与 L1 tile mL1、nL1 的组合。

机制（catalog 原文"优化原理"）：L2 命中率由单轮次分核数 mDim、nDim 和 mL1、nL1 共同决定；最优条件为 **`nDim·nL1 = mDim·mL1`**，使 M、N 轴分核到 L1 的数据量相等，A/B 矩阵在 L2 中的复用最大化，减少 HBM 重复搬运。

## 性能对比
| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 耗时（大 shape，M=N=K=6144） | 2.1 ms | 1.6 ms | +31% |

## 适用范围与警示
- 适用：M、N、K 均较大的大 shape Matmul，且 profiling 显示 L2 命中率偏低、MTE2 带宽利用率不足。
- 警示：前置条件为 S-2（核填充）已完成；属深度调优（PHASE_SWIMLANE）阶段手段。
