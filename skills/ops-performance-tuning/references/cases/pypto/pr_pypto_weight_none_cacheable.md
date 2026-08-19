# 权重矩阵批量 NONE_CACHEABLE（Pangu 7B Fused Layer，优化点 I-2）

## 基本信息
- 算子类别：misc
- DSL/框架：pypto
- 类型：非PR（内部案例库，cannbot-skills 官方调优案例库）
- 来源可信度：官方文档（cann/cannbot-skills 仓 `ops/pypto-op-perf-tune` 调优案例库，含 5 轮迭代失败分析与实测数字）

## 链接
- PR/出处链接：https://gitcode.com/cann/cannbot-skills/blob/master/ops/pypto-op-perf-tune/tune-incocases/weight-none-l2-cacheable.md（ 已验证可达，验证日期 2026-08）；另见 https://gitcode.com/cann/cannbot-skills/blob/master/ops/pypto-op-perf-tune/shared/optimization_catalog.md（[I-2] 条目）（ 已验证可达，验证日期 2026-08）
- 优化代码查看：非代码 MR 类案例，无 diff/files 页；出处链接即为案例原文，内含完整优化前后代码与迭代记录

## 问题与瓶颈
Pangu 7B 单 Kernel 融合 Layer 算子（7 个 matmul + 2 个 RMSNorm + softmax + SwiGLU，Decode M=1，目标 ≤400us），经前端+泳道图调优后降至 437.28us 进入瓶颈。算子含 5 个只读一次的大型 BF16 权重矩阵（qkv 50MB / o 33MB / gate 25MB / up 25MB / down 50MB，合计约 183MB）。属核内调优（PHASE_INCORE）阶段案例。

## 优化方法（理论手段）
对 5 个权重**同时**设置 `tensor.set_cache_policy(pypto.CachePolicy.NONE_CACHEABLE, True)`，使其访问绕过 L2 Cache 直接访问 HBM。机制：

1. 只读一次、不复用的常量权重没有进 L2 的必要——L2 命中率为零却污染 Cache；
2. **融合算子中必须批量设置**：单独绕过某个权重会打破 L2 平衡，导致其他权重访问变慢（案例 5 轮迭代的核心教训）；全部绕过才能把 L2 容量释放给 KV Cache 等真正频繁访问的数据；
3. 输入/输出 Tensor 不适合 NONE_CACHEABLE：输入数据量小、硬件预取已足够；输出需写回主存，绕过 L2 增加写回延迟；
4. Cache 策略效果高度依赖数据访问模式与硬件状态，无法仅凭理论判断，必须逐个实测验证（简单算子场景）。

## 性能对比
| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 耗时 | 437.28 us | 354 us | -19.1% |

## 适用范围与警示
- 适用：含只读一次、不复用的大型常量权重矩阵的融合算子（如 Pangu 7B Fused Layer）。
- 警示：融合算子中必须批量设置，单独绕过某个权重会打破 L2 平衡导致其他权重访问变慢（5 轮迭代的核心教训）；输入/输出 Tensor 不适合 NONE_CACHEABLE；Cache 策略效果高度依赖数据访问模式与硬件状态，必须逐个实测验证。
