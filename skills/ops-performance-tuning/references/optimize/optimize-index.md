# 算子优化技术库（Reference）：索引

> 本目录按主题整理通用算子优化技术，每条给出理论说明、代码示范与性能对比数字。

**技术线索引**：Ascend C（§1，按主题拆为 5 个文件）· SHMEM 通信算子（§2）· CATLASS（§3）· Triton-Ascend（§4）· TileLang-Ascend（§5）· PyPTO（§6）

## 文件索引

| 文件 | 内容 | 原小节 |
|---|---|---|
| [ascendc-tiling.md](optimize/optimize-ascendc-tiling.md) | Ascend C · Tiling 与核间负载 | §1.1 |
| [data-copy.md](optimize/optimize-data-copy.md) | Ascend C · 数据搬运 | §1.2 |
| [api-usage.md](optimize/optimize-api-usage.md) | Ascend C · API 使用 | §1.3 |
| [memory-hierarchy.md](optimize/optimize-memory-hierarchy.md) | Ascend C · 内存层级利用 | §1.4 |
| [pipeline.md](optimize/optimize-pipeline.md) | Ascend C · 流水与 Double Buffer | §1.5 |
| [shmem.md](optimize/optimize-shmem.md) | SHMEM 通信算子（通信/流水/同步/内存/分核 + 端到端实战数据） | §2.1–2.6 |
| [catlass.md](optimize/optimize-catlass.md) | CATLASS（workspace 驻留 L2、容量约束、DispatchPolicy、诊断映射、Swizzle） | §3.1–3.7 |
| [triton.md](optimize/optimize-triton.md) | Triton-Ascend（Vector 类深度优化、30 优化点、SIMT→SIMD 原子操作、constexpr） | §4.1–4.4 |
| [tilelang.md](optimize/optimize-tilelang.md) | TileLang-Ascend（按算子类型、Persistent 调度、Double Buffer 前置分析） | §5.1–5.3 |
| [pypto.md](optimize/optimize-pypto.md) | PyPTO（三阶段调优框架、诊断决策树） | §6.1–6.2 |

---

## 附：跨技术线共性优化原语对照

| 原语 | Ascend C | SHMEM | CATLASS | Triton | TileLang |
|---|---|---|---|---|---|
| 双缓冲 | `InitBuffer(queue, 2, size)` | ping-pong 两组 UB+event id（95KB×2 实战） | `MmadAtlasA2Pingpong` STAGES | UB 留 50% 保证 DB | DB-ANALYSIS 后实施 |
| 流水掩盖 | CopyIn/Compute/CopyOut 三级 | 通信-计算 chunk 重叠 | DispatchPolicy Preload 系列 | 加载与计算交织 | num_stages / T.Pipelined |
| 同步降本 | SetFlag/WaitFlag 按需 | barrier→signal/wait 逐 chunk | workspaceStages 消空泡 | — | Cross-core Semaphore / wait_flag |
| 分核/负载均衡 | blockDim=核数、尾块交替 | sender/receiver 分核、串行 peer 消除 | swizzle `<4,1>` 均衡 | Grid 匹配核数 | `T.Persistent` physical_grid |
| 存储层级利用 | L2 分块、UB 融合、L0C 原地累加 | 避免 GM scratch | C 驻留 L2（命中 96–99%） | UB 容量规划 85KB | alloc_L1/alloc_ub |
| 标量削减 | 禁 std 数学函数、SetMaskCount | — | — | 循环不变量外提、优化点 5/6/17 | — |
