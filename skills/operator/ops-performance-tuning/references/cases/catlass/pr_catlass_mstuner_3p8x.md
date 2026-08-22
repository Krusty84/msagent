# MR !966 msTuner GetTiling 改运行时获取（附 msTuner 寻优数据）【PR】

## 基本信息
- 算子类别：matmul
- DSL/框架：catlass
- 类型：PR
- 来源可信度：一手 PR 原文

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/catlass/pull/966>（ 已验证可达，验证日期 2026-08）；关联 issue：<https://gitcode.com/cann/catlass/issues/328>（ 已验证可达，验证日期 2026-08）
- 优化代码查看：<https://gitcode.com/cann/catlass/pull/966/files>（ 已验证可达，验证日期 2026-08）

## 问题与瓶颈
GetTiling 原本按 SoC 名称硬编码查表获取 AICore 数量与 L2 Cache 大小，新芯片需手工维护静态映射表，易出错、不可扩展。

## 优化方法（理论手段）
1. **GetTiling 改运行时获取**：由 SoC 名称硬编码查表改为运行时 `aclrtGetDeviceInfo` 获取 AICore 数量与 L2 Cache 大小。
2. **删除静态映射表**：消除逐芯片维护的硬编码表，提升可移植性（标签为 Bug修复/refactor，非性能优化，但 MR 中附了 msTuner 在 Ascend 950 上的 GEMM tiling 寻优实测数据）。

## 性能对比
（MR 测试节原文，Ascend 950 上 GEMM tiling 寻优实测）

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| task_duration | 164.028 us（初始 case：256x256x128_256x256x32_swizzle3x1） | 43.291 us（msTuner 寻优 Top-1：128x128x128_128x128x64_swizzle3x1） | tiling 寻优带来约 3.8 倍差异 |

佐证 tiling/swizzle 选择对 GEMM 性能影响极大。

## 适用范围与警示
- 上述 3.8 倍差异是同一 case 下不同 tiling/swizzle 配置之间的差异（初始配置 vs msTuner 寻优 Top-1），来自 MR 测试节实测数据，并非 MR 代码改动本身带来的性能提升——MR 本身为 Bug修复/refactor。
- 数据在 Ascend 950 上测得；其他芯片/其他 shape 下不保证同等差异。
