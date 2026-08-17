# msOpProf 官方快速入门：Add 核间负载差异判读【官方文档】

## 基本信息
- 算子类别：vector-elementwise
- DSL/框架：ascendc
- 类型：非PR（官方工具快速入门）
- 来源可信度：官方文档（Ascend/msot 文档站）

## 来源链接
- PR/出处链接：<https://mindstudio-operator-tools-docs.readthedocs.io/zh-cn/latest/msopprof/source/quick_start/msopprof_quick_start/>（ 已验证可达，验证日期 2026-08）
- 优化代码查看：文档中的 AddCustom 与 `MemoryUB.csv` 示例；无独立 PR diff

## 问题与瓶颈
官方 Add 示例均分为 8 个 block，但不同 block 的 Vector 执行时间和 UB 带宽存在明显差异：Block 0 为 7.456666 us、UB 读带宽 1.023164 GB/s；Block 2 为 10.001111 us、0.762855 GB/s。若差异继续扩大，说明核间工作量、尾块或访问模式存在优化空间。

## 优化方法（理论手段）
1. **先看核间分布再改 blockDim**：用 `MemoryUB.csv`/Occupancy 比较每核 time、throughput、cache hit，而不是只看平均 Task Duration。
2. **定位长尾来源**：检查 `perCore + remainder`、尾块对齐、每核 tile 数与分支是否一致。
3. **上板与仿真互补**：上板看真实时延、带宽和 Cache；仿真看指令流水与代码热点，不能用仿真绝对时间替代上板结论。

## 性能对比
该文档是诊断示例，不是优化前后对比；只提供 8 个 block 的单次采集数据。

## 适用范围与警示
- 适用于 elementwise、reduction、norm 等可按元素均匀切分的多核算子。
- 单次核间差异可能受系统噪声影响；应多轮采集并结合每核处理元素数确认。
- 文档示例的带宽值不是硬件峰值，禁止直接用作 A2/A3/A5 的性能目标。
