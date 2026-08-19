# MR !9716（ops-transformer）AllGatherMM Scalar 与尾块对齐优化【PR】

## 基本信息
- 算子类别：communication（MC² / AllGather + MatMul）
- DSL/框架：ascendc
- 类型：PR
- 来源可信度：一手 PR 合并描述（GitCode 仓首页提交记录）

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/ops-transformer/pull/9716>（ 路由可达，合并描述已核验，验证日期 2026-08）
- 优化代码查看：<https://gitcode.com/cann/ops-transformer/pull/9716/files>（ 路由可达，验证日期 2026-08；重点目录 `mc2/`）

## 问题与瓶颈
AllGatherMM 用例存在 Scalar 侧 `fragmentTensor` 基础接口开销；Host Tiling 的尾块 M 未按要求对齐，导致 L0C 回写 GM 地址出现 1024 非对齐并触发精度异常。原文未附量化时延。

## 优化方法（理论手段）
1. **缩短基础接口热路径**：优化 `fragmentTensor` 基础接口，降低循环内 Scalar 地址/元数据处理成本。
2. **尾块 M 做 16 对齐**：在 Host Tiling 阶段修正尾块形状，保证 L0C→GM 回写地址与硬件约束一致。
3. **精度与性能脚本同步修改**：对齐改变后同时更新两类验证脚本，避免“性能快但覆盖错 shape”或“精度脚本仍按旧布局”造成误判。

## 性能对比
原文未附量化数字。

## 适用范围与警示
- 适用于 MC²、AllGatherMM 及其他通信后接 Cube 的尾块场景。
- 这是“Scalar 性能 + 精度对齐”联合修复；对齐 padding 会改变实际处理量，前后对比必须固定逻辑 shape 并记录物理 tile。
- MC² 多 rank 应使用支持通算流水的 `msprof op`/msOpProf 采集方式；不可只采单 kernel 后下端到端结论。
