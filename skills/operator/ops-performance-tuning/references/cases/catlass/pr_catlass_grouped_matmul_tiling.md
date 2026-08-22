# MR !978 更新 cppgen grouped matmul 的 default tiling 策略【PR】

## 基本信息
- 算子类别：matmul
- DSL/框架：catlass
- 类型：PR
- 来源可信度：一手 PR 原文

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/catlass/pull/978>（ 已验证可达，验证日期 2026-08）；关联 issue：<https://gitcode.com/cann/catlass/issues/386>（ 已验证可达，验证日期 2026-08）
- 优化代码查看：<https://gitcode.com/cann/catlass/pull/978/files>（ 已验证可达，验证日期 2026-08）

## 问题与瓶颈
`get_default_tile_shape` 返回固定 `GemmShape(256,256,256), GemmShape(256,256,64)`，fp32 下所需 L1 空间 `256*256*sizeof(float)*(L1A_STAGES+L1B_STAGES)` = 1024KB，超过 512KB 硬件约束导致报错。

## 优化方法（理论手段）
1. **按 `element_max_size` 计算默认 tiling**：不再返回固定 tile 形状，而是根据元素最大尺寸推导默认 tiling，保证片上存储需求不超过硬件容量。
2. **tiling 形状与片上存储（L1）容量联合约束求解**——典型的 tile-size/存储容量权衡案例。

## 性能对比
原文未附量化数字（本 MR 为修复硬件容量约束导致的报错，属正确性/可用性修复而非性能优化）。

## 适用范围与警示
- 触发场景为 cppgen grouped matmul 在 fp32 等较大元素 dtype 下默认 tiling 超出 L1 容量（512KB 约束）；fp32 以外 dtype 的固定默认值同样存在越界风险，本修复统一改为按 `element_max_size` 计算。
