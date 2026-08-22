# ops-nn 官方 Wiki：MatMul VCV 场景流水优化【官方文档】

## 基本信息
- 算子类别：matmul
- DSL/框架：ascendc
- 类型：非PR（官方 Wiki）
- 来源可信度：官方文档

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/ops-nn/wiki/MatMul%E7%AE%97%E5%AD%90VCV%E6%80%A7%E8%83%BD%E4%BC%98%E5%8C%96%E5%AE%9E%E8%B7%B5%E4%B8%8E%E6%95%88%E6%9E%9C%E5%88%86%E6%9E%90.md>（ 已验证可达，验证日期 2026-08）
- 优化代码查看：官方 Wiki 文章内代码段；无独立 PR diff

## 问题与瓶颈
VCV 指 Cube 的源数据来自 Vector 预处理，同时 Cube 输出还需进入 Vector 后处理。若三段按顺序串行执行，会同时出现 Vector/Cube 等待、跨核同步过密与中间结果反复落 GM/UB 的问题。

## 优化方法（理论手段）
1. **Vector→Cube→Vector 分阶段流水**：按 tile 组织前处理、MMAD、后处理，让相邻 tile 在不同 Pipe 并行。
2. **中间数据片上化**：能在 L1/L0C/UB 之间直接衔接时避免回写 GM，再由下一阶段读回。
3. **同步粒度跟随 tile**：用最小必要的 SetFlag/WaitFlag 或事件连接阶段，避免整算子级 barrier。
4. **Tiling 联合求解**：tile 既要满足 Cube 形状效率，也要给前后 Vector 阶段留出 UB 与双缓冲空间。

## 性能对比
搜索可见的官方摘要未附可可靠抄录的量化表；具体数字以原 Wiki 页面为准，落库时不推算。

## 适用范围与警示
- 适用于量化/反量化、激活、归一化、转置等 Vector 前后处理包围 MatMul 的融合算子。
- 只增大 Cube tile 可能挤压 Vector UB 并破坏整体流水；必须看全链路 PipeTimeline，而不是只看 Cube 利用率。
- 不同 SoC 的 MIX 核组织与同步 API 不同，需按 A2/A3/A5 分支实现。
