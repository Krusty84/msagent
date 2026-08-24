# MR !9692（ops-transformer）Chunk Gated Delta Rule 求逆改 VF【PR】

## 基本信息
- 算子类别：attention
- DSL/框架：ascendc
- 类型：PR
- 来源可信度：一手 PR 合并描述（GitCode 仓首页提交记录）

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/ops-transformer/pull/9692>（ 路由可达，合并描述已核验，验证日期 2026-08）
- 优化代码查看：<https://gitcode.com/cann/ops-transformer/pull/9692/files>（ 路由可达，验证日期 2026-08；重点目录 `attention/chunk_gated_delta_rule/`）

## 问题与瓶颈
Chunk Gated Delta Rule 的 stage1 求逆部分未充分使用 A5 VF/RegBase 向量路径，存在可合并的逐元素/小矩阵计算与中间同步。原文未附 profiling 指标和量化时延。

## 优化方法（理论手段）
1. **求逆子流程 VF 化**：将适合寄存器向量执行的计算搬到 VF 函数，减少高层 API 调用边界。
2. **双层向量流水**：合并连续计算链，尽量让中间值停留在 RegTensor，降低 UB 往返和 PipeBarrier。
3. **局部替换而非整算子重写**：只替换 stage1 热点子流程，保留外围 Tiling、接口和其他阶段，降低精度回归面。

## 性能对比
原文未附量化数字。

## 适用范围与警示
- 适用于 A5 上可用 VF/RegBase 表达的 attention 前处理、递推状态更新或小规模求逆子流程。
- 求逆对数值稳定性敏感；必须覆盖极端值、接近奇异矩阵与不同 dtype，不能只做随机精度样例。
- VF 属 A5 专属/强化路径时，A2/A3 必须保留原实现或另做同构适配。
