# 官方 benchmark：TileLang vs 手写 AscendC【非 PR：README 官方数据】

## 基本信息
- 算子类别：misc（覆盖 Cube 算子 GEMM/batch_gemm、Vector 算子 hc_sinkhorn、FA 等融合算子）
- DSL/框架：tilelang
- 类型：非PR（官方文档：仓库 README 官方数据）
- 来源可信度：官方文档

## 来源链接
- PR/出处链接：<https://github.com/tile-ai/tilelang-mlir-ascend/blob/main/README.md>（ 已验证可达，验证日期 2026-08）
- 优化代码查看：无独立代码 diff 链接——该案例为 README 官方 benchmark 汇总数据，非单个 PR 的代码改动。

## 问题与瓶颈
原文未附 profiling 瓶颈定位数据；该 benchmark 对比 TileLang 生成 kernel 与手写 AscendC 的耗时差距。

## 优化方法（理论手段）
未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证——该案例为官方 benchmark 数据汇总，原文未描述具体优化方法。

## 性能对比
- Cube 算子 GEMM (4096³)：AscendC 497.930 us vs TileLang 501.190 us（0.993x）；batch_gemm 8×4096³：3800.376 vs 3963.859 us（0.959x）。
- Vector 算子 hc_sinkhorn (DSV4 mHC)：b*s=16384 时 1902.1 vs 1850.12 us（1.028x，反超手写）；平均 0.96x。
- FA 等融合算子平均约 0.95x；"FA written in TileLang achieve performance on Ascend hardware that matches hand-written AscendC equivalents at a 1.0x level"（README Latest News 原文）。

## 适用范围与警示
- 数据为官方 README 公布的 benchmark，具体 shape/芯片条件以 README 原文为准；上述数字为 TileLang 相对手写 AscendC 的对比（0.96x~1.028x），非优化前后对比。
