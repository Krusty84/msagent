# MR !1589（cann/ops-transformer）"add mhc" —— AscendC mHC 算子【PR】

## 基本信息
- 算子类别：misc（einsum 类小矩阵批量计算，mHC / Manifold-Constrained Hyper-Connections）
- DSL/框架：ascendc
- 类型：PR
- 来源可信度：一手 PR 原文（内容来自 GitCode 仓库提交记录页）

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/ops-transformer/pull/1589>（ 已验证可达，验证日期 2026-08）
- 优化代码查看：<https://gitcode.com/cann/ops-transformer/pull/1589/files>（ 已验证可达，验证日期 2026-08）

## 问题与瓶颈
面向昇腾 NPU 的 mHC（Manifold-Constrained Hyper-Connections）算子，基线为 torch.einsum；原文未附 profiling 指标数据。实现为 AscendC（智子芯元 KernelCAT 智能体生成）。

## 优化方法（理论手段）
1. **einsum 类小矩阵批量计算的融合 + 片上复用**：消除中间结果 GM 往返。

## 性能对比
（MR 描述原文，对比 torch.einsum，Ascend 910B2）

| 算子 | 加速比 |
|---|---|
| mhc_pre | 24x ~ 52x |
| mhc_post | 2x ~ 5x |
| mhc_res | 24x ~ 50x |

## 适用范围与警示
- 适用：Ascend 910B2；对比基线为 torch.einsum，非优化后 AscendC 实现间对比。
