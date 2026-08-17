# MR !8276（ops-nn）Conv3DDX L1 搬运 TQue→TBuf【PR】

## 基本信息
- 算子类别：misc（convolution）
- DSL/框架：ascendc
- 类型：PR
- 来源可信度：一手 PR 合并描述（GitCode 仓首页提交记录）

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/ops-nn/pull/8276>（ 路由可达，合并描述已核验，验证日期 2026-08）
- 优化代码查看：<https://gitcode.com/cann/ops-nn/pull/8276/files>（ 路由可达，验证日期 2026-08；重点目录 `conv/`）

## 问题与瓶颈
Conv3D 反向输入算子的 L1 搬运使用 TQue 管理，而该缓冲在对应阶段更接近固定生命周期的临时存储。PR 以性能优化标签合入，但原文未附 msOpProf 指标与量化数字。

## 优化方法（理论手段）
1. **按生命周期选缓冲抽象**：将不需要生产者/消费者队列语义的 L1 暂存从 TQue 改为 TBuf，减少 EnQue/DeQue/FreeTensor 管理与同步开销。
2. **保留真正的异步流水队列**：仅替换固定暂存，CopyIn/Compute 间确有并行依赖的队列仍使用 TQue，避免为“少同步”破坏流水。
3. **跨芯片回归**：原 PR 记录相关芯片版本 RDV、daily 通过，说明此类改动必须做多 SoC 精度与稳定性回归。

## 性能对比
原文未附量化数字。

## 适用范围与警示
- 适用于卷积、MatMul 等 L1 暂存明确、没有队列生产消费关系的缓冲。
- 不应机械地把所有 TQue 改为 TBuf；若队列承担 double buffer 或跨 Pipe 同步，替换会破坏正确性或并行性。
- 无公开加速比，必须通过 PipeUtilization/Timeline 和 event 时延验证收益。
