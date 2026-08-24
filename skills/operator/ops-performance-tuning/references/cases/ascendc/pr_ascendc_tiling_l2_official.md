# Ascend C 官方技术文章：超 L2 数据量的 L2Cache Tiling【官方文档】

## 基本信息
- 算子类别：misc（通用 Tiling）
- DSL/框架：ascendc
- 类型：非PR（官方技术文章）
- 来源可信度：昇腾官方技术文章

## 来源链接
- PR/出处链接：<https://www.hiascend.com/developer/techArticles/20240920-1>（ 已验证可达，验证日期 2026-08）
- 优化代码查看：文章内“Ascend C 算子优化实用技巧 04——Tiling 优化”示例；无独立 PR diff

## 问题与瓶颈
输入/输出数据量超过 L2Cache 时，简单按核连续切分可能使当前 tile 的复用数据在后续阶段到来前被逐出，造成重复 GM 访问；仅增大 blockDim 不会自动解决数据局部性。

## 优化方法（理论手段）
1. **使能 L2Cache 友好切分**：让相邻计算块在时间和地址上复用同一批 L2 数据，减少回源 GM。
2. **以工作集而非总 tensor 估容量**：计算每轮 input/output/workspace 的实际驻留量，按 L2 可容纳的工作集确定切分。
3. **用指标闭环**：优化前后对比 L2 hit、GM traffic、MTE active bandwidth 与 event 时延，避免只凭理论容量判断。

## 性能对比
本文落库仅引用切分原则，未抄录可复用的统一性能数字；不同算子与 SoC 必须重新测量。

## 适用范围与警示
- 适用于数据量大于 L2、存在输入或权重重复使用的 MatMul、Conv、Reduction、融合算子。
- L2 容量、路数和共享方式随 A2/A3/A5 变化；禁止硬编码单一容量阈值。
- 若数据无复用、纯流式读写，L2 切分可能无收益并增加 Tiling 复杂度。
