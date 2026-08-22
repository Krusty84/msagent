# MR !8443（ops-nn）Logit A5 多核均衡与 MicroAPI 向量化【PR】

## 基本信息
- 算子类别：vector-elementwise
- DSL/框架：ascendc
- 类型：PR
- 来源可信度：一手 PR 合并描述（GitCode 仓首页提交记录）

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/ops-nn/pull/8443>（ 路由可达，合并描述已核验，验证日期 2026-08）
- 优化代码查看：<https://gitcode.com/cann/ops-nn/pull/8443/files>（ 路由可达，验证日期 2026-08；重点文件 `loss/logit/op_kernel/logit.h`）

## 问题与瓶颈
原实现按 loop 做核间分配，尾核容易出现负载不均；A5 的 clamp 路径使用两次 `CompareScalar + Select`，指令数偏多；后续 `Muls → Adds → Div → Log` 使用高层 API，存在中间 `PipeBarrier`。原文未附 msOpProf 指标与量化时延。

## 优化方法（理论手段）
1. **按元素均衡切核**：用 `perCore + remainder` 代替按 loop 分配，优先消除 Occupancy 视图中的尾核长尾。
2. **压缩 clamp 指令链**：A5 且 `eps <= 0.5` 时用 `Mins + Maxs` 代替两组比较与选择，将四条核心指令压为两条；其他条件保留原路径。
3. **MicroAPI RegTensor 串链**：在 A5 分支中把 `Muls → Adds → Div → Log` 放进寄存器向量路径，减少中间 PipeBarrier 和 UB 往返。
4. **跨代分支隔离**：A2/A3 保留原高层 API 路径，避免把 A5 专属优化无条件下沉到旧架构。

## 性能对比
原文未附量化数字；只能作为“负载均衡 + 指令压缩 + RegTensor 串链”的机制先例，必须在目标 shape 上重新采集。

## 适用范围与警示
- 适用：A5 Logit 或相似 elementwise 链，尤其是尾核不均、Compare/Select 较重、多个高层 API 之间同步偏多的场景。
- `eps > 0.5`、A2、A3 不应照搬 A5 快路径；必须保留语义分支并重新做精度验证。
- 无公开加速比，禁止在报告中写成已证明的性能收益。
