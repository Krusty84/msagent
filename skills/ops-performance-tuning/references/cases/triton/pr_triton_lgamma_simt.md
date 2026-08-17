# PR #693 "[libdevice] fix: Optimize the performance of lgamma under the SIMT mode"【PR】 后被回退

## 基本信息
- 算子类别：vector-elementwise
- DSL/框架：triton
- 类型：PR
- 来源可信度：一手 PR 原文

## 来源链接
- PR/出处链接：<https://github.com/triton-lang/triton-ascend/pull/693>（2026-06-23 合入）（ 已验证可达，验证日期 2026-08）
- 优化代码查看：<https://github.com/triton-lang/triton-ascend/pull/693/files>
- 回退/重启用 PR：<https://github.com/triton-lang/triton-ascend/pull/815>（"[libdevice] revert: Revert high-performance optimization scheme"，2026-06-26 合入）、<https://github.com/triton-lang/triton-ascend/pull/833>（重新 Enable，Draft 未合入）（ 已验证可达，验证日期 2026-08）

## 问题与瓶颈
SIMT 模式下 lgamma 性能低（原文性能分仅 0.12，L20 平台）。

## 优化方法（理论手段）
1. 在 SIMT 模式下优化 libdevice lgamma 实现（高性能方案）。原文未详述具体机制。

## 性能对比
（PR 正文原文）："The original lgamma implementation achieved a performance score of only 0.12 on the L20, which has been optimized to over 0.5 after improvement."

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| lgamma 性能分（L20） | 0.12 | 0.5+ | 约 4 倍 |

## 适用范围与警示
-  风险说明：后续 PR #815（"[libdevice] revert: Revert high-performance optimization scheme"，2026-06-26 合入）因 libdevice.tanh 测试失败回退了 libdevice 高性能方案的默认启用及 lgamma 优化；PR #833 尝试重新 Enable（Draft 未合入）。引用时说明其反复状态。
