# Swizzle 负载均衡调优【非 PR：官方调优指引文档】

## 基本信息
- 算子类别：matmul
- DSL/框架：catlass
- 类型：非PR（官方文档）
- 来源可信度：官方文档（内容经腾讯云开发者社区转载，二手转载标注）

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/catlass/blob/v1.2.0/docs/catlass_optimize_guidance.md>（docs/catlass_optimize_guidance.md； 已验证可达，验证日期 2026-08）；腾讯云开发者社区转载：<https://cloud.tencent.com/developer/article/2612963>（ 已验证可达，验证日期 2026-08）
- 优化代码查看：不适用——本案例为官方调优指引文档中的调优案例，非代码 PR，无 diff 可查；swizzle 参数使用方法见上述文档原文。

## 问题与瓶颈
使用 swizzle `<3,1>` 导致某些核心承担了多于其他核心的 tile 数，负载不均。

## 优化方法（理论手段）
1. **BlockScheduler swizzle 调整**：改用 `<4,1>` 使各核心分工更加均衡，提升流水线饱满度（AI Core 间任务分配均衡性调优）。

## 性能对比

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 耗时 | 40.6µs（swizzle `<3,1>`） | 35.3µs（swizzle `<4,1>`） | 降低 5.3µs（约 13%） |

## 适用范围与警示
- 本案例数字来自腾讯云开发者社区的**二手转载**，原文出处为官方调优指引 docs/catlass_optimize_guidance.md（v1.2.0）；引用时注意二手来源属性。
- 最优 swizzle 参数与 shape、核数强相关，`<4,1>` 并非普适值，需按实际 tile 分布寻优。
