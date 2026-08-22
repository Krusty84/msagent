# 融合算子/MC² 通算融合【非 PR：社区文章， 二手来源】

## 基本信息
- 算子类别：communication（通算融合，兼含 matmul/attention）
- DSL/框架：ascendc
- 类型：非PR（社区文章）
- 来源可信度：二手转载（ 二手来源，非 PR 原文，引用时建议标注）

## 来源链接
- PR/出处链接：<https://hwcomputing.csdn.net/6a156a06662f9a54cb7740f2.html>（CANN ops-transformer 技术解读； 已验证可达，验证日期 2026-08）
- 优化代码查看：二手社区文章，无公网代码链接，原文未附代码改动页

## 问题与瓶颈
大模型中 AllGather+MatMul、ReduceScatter+Softmax 等通信与计算串行执行，通信无法与计算重叠。原文未附 profiling 指标数据。

## 优化方法（理论手段）
1. **AllGather+MatMul、ReduceScatter+Softmax 通算融合**：通信与计算重叠，端到端加速比可达 1.3x–1.5x（大模型）。
2. **ops-nn MLA 全链路融合**：Cube 算完直接在 L1 上做 attention，不回写 GM。

## 性能对比
| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 端到端耗时（大模型） | 原文未附 | 原文未附 | 加速比 1.3x–1.5x |

## 适用范围与警示
- ** 风险说明**：以上数字来自社区文章，非 PR 原文，引用时建议标注为二手来源。
- 适用场景：大模型通算融合；具体适用 shape/dtype/芯片原文未附。
