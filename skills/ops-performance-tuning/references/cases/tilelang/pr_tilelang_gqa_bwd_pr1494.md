# PR #1494（GQA-BWD 性能数据，Refactor 类但附 benchmark）【PR】

## 基本信息
- 算子类别：attention
- DSL/框架：tilelang
- 类型：PR（Refactor 类，但附 benchmark 数据）
- 来源可信度：一手 PR 原文

## 来源链接
- PR/出处链接：<https://github.com/tile-ai/tilelang-ascend/pull/1494>（ 已验证可达，验证日期 2026-08；2026-07-30 合入）
- 优化代码查看：<https://github.com/tile-ai/tilelang-ascend/pull/1494/files>

## 问题与瓶颈
原文未附 profiling 瓶颈定位数据；PR 为 Refactor 类改动，附带的 benchmark 可用于说明 tilelang FA 反向与框架级实现仍有差距的现状。

## 优化方法（理论手段）
1. GQA 反向 kernel 重构（Refactor 类），原文未附具体优化机制说明。

## 性能对比
数字（B=8 H=32 N=1024 D_qk=192 D_v=128 groups=16 fp16）：

| 指标 | TileLang Backward (pipeline) | TileLang Forward v4 | PyTorch Fwd+Bwd (e2e) |
|---|---|---|---|
| 耗时 | 34.42 ms | 10.13 ms | 20.12 ms |
| 算力 | 12.98 TFLOPS | 16.95 TFLOPS | 30.75 TFLOPS |

## 适用范围与警示
- 上述数字仅为单一 shape（B=8 H=32 N=1024 D_qk=192 D_v=128 groups=16 fp16）下的结果；可用于说明 tilelang FA 反向与框架级实现仍有差距的现状。
