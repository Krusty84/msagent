# §3 CATLASS

> 本文件覆盖 CATLASS 模板库的优化技术（workspace 驻留 L2、容量约束、DispatchPolicy、诊断映射、Swizzle 等）。

## 3.1 头号优化：消除整块 C 的 HBM 往返（workspace 驻留 L2）

- **理论说明**：`MatmulActivation/MatmulEpilogue` 把整块 `[M,N]` fp32 C 写回 HBM 再读回（流量 2·M·N·4 B）；换为"多级轮转 workspace + `MmadAtlasA2PreloadAsyncWithCallback`"后 C 驻留 L2，AIC/AIV 细粒度重叠；量化场景用 `QuantMatmulMultiStageWorkspace`。
- **性能对比**：C 驻留 L2 实测命中 **96–99%**。
- **禁止事项**：大 N 场景禁止"仍是整块 HBM 往返时只调 TileShape 就交付"。

## 3.2 容量约束与经验起点

- L1 占用 ≈ `(L1.M·L1.K + L1.K·L1.N)·sizeof·l1Stages ≤ ~512KB`；L0C ≈ `L1.M·L1.N·4 ≤ 128KB`。
- fp16 起点 `L1<128,256,256>` / `L0<128,256,64>`；int8 可到 K=512。

## 3.3 DispatchPolicy 调度策略（流水排布）

机制细节见 catlass 官方文档 `docs/dispatch_policies`。

| DispatchPolicy | 机制 |
|---|---|
| `MmadAtlasA2Pingpong` | L1/L0 双缓冲（STAGES 控制片数，ENABLE_UNIT_FLAG 控制 Mmad 与 L0C 拷出并行） |
| `MmadAtlasA2Preload` | + ShuffleK（重排 K 轴顺序优化 Cache 利用率）+ Block 间预加载 |
| `MmadAtlasA2PreloadAsync` | nBuffer 机制，L1/L0 可配不同缓冲片数，支持 group 间预加载（用于 Grouped Matmul） |

## 3.4 瓶颈诊断 → 调参映射表

| 症状 | 手段 |
|---|---|
| HBM 带宽高 + L2 命中低 | 换 workspace Kernel（见 §3.1） |
| MTE2 占比高 | Preload / ShuffleK / 调大 N-tile |
| Cube 高 Vector 闲 | 调大 K-tile |
| 任务块 < AIC 核数 | 调小 M-tile / SplitK |
| 小 shape | SmallMatmul Kernel |
| A 重读 | FullLoadA |
| 同步空泡 | workspaceStages |
| Cube ~90%+ | 即接近 roofline，再提速受硬件结构约束 |

## 3.5 Swizzle 负载均衡

- **理论说明**：BlockScheduler swizzle 调整 AI Core 间任务分配均衡性（提升流水线饱满度）。
- **代码/参数模式**：swizzle `<3,1>` → `<4,1>`。
- **性能对比**：性能从 40.6µs 提升到 35.3µs（出处：catlass `docs/catlass_optimize_guidance.md`，<https://gitcode.com/cann/catlass/blob/v1.2.0/docs/catlass_optimize_guidance.md>；经腾讯云社区转载 <https://cloud.tencent.com/developer/article/2612963>；案例全文见 [cases/catlass/pr_catlass_swizzle_balance.md](cases/catlass/pr_catlass_swizzle_balance.md)）。

## 3.6 可调参数总表

TileShape（L1/L0 层）、DispatchPolicy、Swizzle 策略（含 Swizzle offset、Preload、Split-K、TLA 等待试项）、Kernel 类型。

## 3.7 tiling/swizzle 影响量级佐证

msTuner 在 Ascend 950 上的 GEMM tiling 寻优实测：初始 case `task_duration: 164.028 us`（256x256x128_256x256x32_swizzle3x1）→ Top-1 配置（128x128x128_128x128x64_swizzle3x1）**43.291 us**，约 3.8 倍差异（数据出自 catlass MR !966 测试节，<https://gitcode.com/cann/catlass/pull/966>；案例全文见 [cases/catlass/pr_catlass_mstuner_3p8x.md](cases/catlass/pr_catlass_mstuner_3p8x.md)）。
