# PR #208 / #218 "fix(smit): atomic operations using SIMD for better performance"【PR】 后被 revert

## 基本信息
- 算子类别：reduction
- DSL/框架：triton
- 类型：PR
- 来源可信度：一手 PR 原文

## 来源链接
- PR/出处链接：<https://github.com/triton-lang/triton-ascend/pull/218>（2026-05-25 合入；同内容前身 PR #208 <https://github.com/triton-lang/triton-ascend/pull/208>）（ 已验证可达，验证日期 2026-08）
- 优化代码查看：<https://github.com/triton-lang/triton-ascend/pull/218/files>
- 回滚 PR：<https://github.com/triton-lang/triton-ascend/pull/376>（"Revert ..."）、<https://github.com/triton-lang/triton-ascend/pull/405>（"Revert fix(smit)... (#218)"），均于 2026-06-03 合入（ 已验证可达，验证日期 2026-08）

## 问题与瓶颈
SIMT 模式下带离散掩码的原子操作（atomic add/max/min/and/or/xor）逐元素串行发射，耗时极高。

## 优化方法（理论手段）
1. 改用 SIMD 向量化路径实现离散掩码原子操作——减少标量指令、提升向量利用率，因此大幅降低原子操作耗时。

## 性能对比
（PR 正文 Performance Test 表，shape 16,16,16，单位 µs）

| 指标 | 优化前（SIMT） | 优化后（SIMD） | 变化 |
|---|---|---|---|
| atomic_add_3d INT32 | 81.163 | 3.309 | 约 24.5 倍 |
| atomic_and_3d INT32 | 96.141 | 4.114 | 约 23 倍 |
| atomic_min_3d FLOAT | 92.389 | 3.372 | 约 27 倍 |

另 ROLLBACK 路径 3.5~4.3 µs。

## 适用范围与警示
-  风险说明：该改动合入后曾引发问题并被 revert（PR #376 "Revert ..."、PR #405 "Revert fix(smit)... (#218)"，均于 2026-06-03 合入）——引用时需说明该优化经历了"合入→回滚"过程，仅适合作为"SIMT→SIMD 原子操作向量化"的理论方法案例。
