# HCCL/HCOMM 通信显存行为模式

> 用途：分析集合通信（AllReduce/ReduceScatter/AllToAll 等）相关显存占用时使用，聚焦**通信库在 device 侧申请的 Buffer 行为模式**（何时分配、多大、如何复用、何时释放），不关注实现细节。

## 1. 通信内存总体架构：两类 Buffer，两个管理器

一个通信域（Communicator）的 device 侧内存由两个独立管理器分管：

```
HcclCommunicator
 ├─ CCLBufferManager      <-- CCL Buffer（通信域级，一次性分配，持久）
 │        ├─ inCCLbuffer_    输入 CCL Buffer
 │        ├─ outCCLbuffer_   输出 CCL Buffer
 │        └─ winExpBuffer_   扩展 Buffer（1MB，供 MC2 使用）
 │
 └─ WorkspaceResourceImpl  <-- Scratch Buffer（算子级，按需分配）
          └─ WorkSpaceMem (Per-Tag)  <-- 按 tag 隔离的内存池
```

| 维度 | CCL Buffer | Scratch Buffer |
| --- | --- | --- |
| 生命周期 | 通信域初始化时分配一次，持久存在，域销毁时释放 | 需要 scratch 的算子首次执行时按需分配，按 tag 复用，域销毁时释放 |
| 分配时机 | 通信域创建/首次建链时（懒分配：首次使用时 size==0 才创建） | 首次执行需要 scratch 的通信算子时 |
| 作用域 | 整个通信域共享 | 按 tag 隔离，不同算子/不同调用链独立 |
| 默认大小 | in/out 各 200MB（`HCCL_BUFFSIZE` 可配） | 算子自行计算（多数为 0，见 §3） |
| 分配方式 | 单次大块连续分配，内部切片（in｜out｜winExp 三段） | 按 tag 从池中分配 |

## 2. CCL Buffer 管理

### 2.1 大小

- **in / out 各 200MB 默认**：`HCCL_CCL_COMM_DEFAULT_BUFFER_SIZE = 200`（MB）× 1MB/单位。环境变量 `HCCL_BUFFSIZE` 控制，单位 MB，**最小 1MB**。in/out 大小相同、各自独立。
- **winExpBuffer_ 固定 1MB**（`EXP_BUFFER_SIZE`），供 MC2（多卡协同计算）使用，不可配置。
- 共享模式下（bufferName 非空）整段（in+out+winExp 合计）按名共享，见 §5。

### 2.2 内存布局

单次 `DeviceMem::alloc(totalSize)` 申请连续内存，再切片：

```
|<- inCCLbuffer_ (200MB) ->|<- outCCLbuffer_ (200MB) ->|<- winExpBuffer_ (1MB) ->|
```

- 总占用 = in + out + winExp = **401MB**（默认配置下每个通信域）。
- 释放：通信域析构时一次性 `free`（共享 buffer 由 `ShareCCLbufferMgr` 引用计数管理，最后一域释放才真正归还）。

### 2.3 msmemscope 观测要点

- 每个通信域对应一次 HAL 层大块申请（driver 侧一次 malloc ~401MB），可在 HAL MALLOC 事件中看到 size ≈ in+out+winExp 的大段。
- 开多通信域（如多进程/多 npu 场景、vLLM 每进程一域）会叠加多份 401MB——**通信 Buffer 是"每通信域一份"的固定开销**，不随数据量变化。
- `HCCL_BUFFSIZE` 调小可省显存，但可能降低大报文通信性能（报文分段搬运）。

## 3. Scratch Buffer：各算子所需大小

- 每个算子 executor 通过覆写 `CalcScratchMemSize()` 决定 scratch 大小；**基类 `CollNativeExecutorBase` 默认返回 0**（绝大多数 Ring 算法就是 0）。
- scratch 内存 `WorkSpaceMem` 按 **tag** 隔离分配、可复用（同 tag 的逐次调用不重复申请，取最大值）。
- 两种运行模式：**图模式**（`HCCL_WORKFLOW_MODE_OPS_KERNEL_INFO_LIB`，无 CCL Buffer 可用，需要 scratch 时大小通常为整包数据量）与 **OP_BASE 模式**（单算子调用，可复用 CCL Buffer，scratch 通常为 0 或等于 inCCLbufferSize_）。

### 3.1 AllReduce

| 算法变体 | 触发条件 | scratch 大小 | 备注 |
| --- | --- | --- | --- |
| Ring（默认） | — | 0 | 环形不需要 scratch |
| Mesh HD（小数据） | **图模式** + 910B + deterministic 使能 + 卡数∈(2,8) + 数据 ≤64KB：910B 为 `totalSize × (rankSize−1)`；910_93 为 `totalSize × log2(2×rankSize−1)` | 非零 | 存 rankSize−1 份/多级中间规约结果 |
| Mesh AIV（910_93） | 图模式确定性 | `2×count×size + (rankSize+1)²×size` | AIV 模式路径 |
| 确定性 Pipeline | 图模式（910B 优化路径） | `totalSize + rankSize × HCCL_MIN_SLICE_ALIGN_910B(16KB)` | Pipeline 额外 slice 对齐开销 |
| Order Preserved | **图模式**且 `inputSize < (rankSize−1)×sizePerBlock` | `totalSize = max(sizePerBlock×rankSize, inputSize)`，`sizePerBlock` 按 16KB 上取整 | 保序需暂存中间数据；不满足条件时 0 |
| 其他（非优化路径） | 一般环境 | `count×size×2 + (rankSize+1)²×size` | legacy operator 路径 |

> **总结**：绝大多数场景 Ring、scratch=0；只有 Mesh HD、确定性 Pipeline、Order Preserved 需要。

### 3.2 ReduceScatter

| 算法变体 | 触发条件 | scratch 大小 | 备注 |
| --- | --- | --- | --- |
| Ring/Mesh（OP_BASE） | 满足 SDMA 规约 + RDMA 规约（`IsSupportSDMAReduce` + `IsSupportRDMAReduce`，**仅判 910B**） | 0 | 复用 CCL Buffer，DMA inline 直接写收 |
| Ring/Mesh（OP_BASE） | 不满足上述条件 | `inCCLbufferSize_`（默认 200MB） | DMA inline 不可用时需拷贝中间数据 |
| Ring/Mesh（图模式） | — | `totalSize`（= count×size） | 图模式无 CCL Buffer，存完整中间结果 |
| 确定性 | 图模式 | `totalSize` | |
| 确定性 Pipeline | 图模式 | `totalSize + rankSize × HCCL_MIN_SLICE_ALIGN_910B(16KB)` | |
| DMA Inline | SDMA+RDMA 规约使能 | 0 | output 直接写入 CCL out buffer |

> **总结**：OP_BASE 满足规约（910B）则 0；不满足则同 `inCCLbufferSize_`（Think：OP_BASE 下最坏情况 scratch ≈ 一个 CCL Buffer 大小，double 通信内存）；图模式按数据总量。

### 3.3 ReduceScatterV

| 算法变体 | 触发条件 | scratch 大小 | 备注 |
| --- | --- | --- | --- |
| 确定性 Mesh | 图模式 + Mesh 拓扑 | `maxCount × rankSize × dataTypeSize` | 变长模式按最大 count 计算 |
| Mesh OpBase Pipeline | OP_BASE | 0 | 复用 CCL Buffer 做两级通信 |
| Mesh | 构造函数显式置 `scratchMemFlag_=false` | 0 | |
| Ring / Fast Double Ring | 不覆写 | 0 | 继承基类默认 |
| AIV（BigCount / Mesh SmallCount） | AIV 模式 | 0 | 使用 AIV buffer |

> **总结**：绝大多数 scratch=0，仅确定性 Mesh（图模式）需要。

### 3.4 Broadcast

| 算法变体 | 触发条件 | scratch 大小 | 备注 |
| --- | --- | --- | --- |
| Ring（默认） | — | 0 | |
| Mesh HD / 小数据 | 图模式 | `totalSize × (log2(2×rankSize−1) − 1)` | HD 扇出需中间存储 |
| Mesh AIV（910_93） | 图模式 | `totalSize` | 910_93 AIV 路径 |

> **总结**：绝大多数为 0，仅 Mesh HD（图模式）需要。

### 3.5 AllToAll / AllToAllV

| 算子 | 算法变体 | 触发条件 | scratch 大小 |
| --- | --- | --- | --- |
| AllToAll（标准） | — | — | **0（不覆写基类）** |
| AllToAllV | Staged | 图模式 | `workSpaceMemSize`（当前 rank 的 mesh 聚合 sendLength 之和） |
| AllToAllV | Staged | OP_BASE | `max(maxWorkSpaceMemSize, inCCLbufferSize_, 2MB)` — 满足最大 rank 需求 |
| AllToAllV | Staged | `workSpaceMemSize==0` | `TINY_MEM_SIZE = 2MB`（兜底） |
| AllToAllV | LevelPipeline | 图模式 + 910B | `CalAlltoAllVScratchMemSize(max(maxBlockSize×rankSize, sendOffset+sendLength, recvOffset+recvLength))` — 两级流水中间 scratch |
| AllToAllV | LevelPipeline | OP_BASE / 910_93 | `2MB (TINY_MEM_SIZE)` — 走 CCL buffer |
| AllToAllV | FullMesh / DirectFullMesh / ContinuousPipeline | — | 0 |


## 4. AIV / MC2 特殊 Buffer

### 4.1 AIV Buffer（`HCCL_OP_EXPANSION_MODE=AIV` 时启用）

启用 AIV 模式后，除 CCL Buffer 外还需申请（`CreateCommAIVbuffer(useOpbaseFlag)`，`useOpbaseFlag=true` 为 OP_BASE 模式，否则 Offload 模式）：

| buffer | 条件 | 大小 |
| --- | --- | --- |
| inAivOpbaseBuffer_ | OP_BASE | 36MB（AIV_DATA_SIZE） |
| outAivOpbaseBuffer_ | OP_BASE | 4MB（AIV_FLAG_SIZE） |
| inAivOffloadBuffer_ | Offload | 36MB |
| outAivOffloadBuffer_ | Offload | 4MB |
| aivCommInfoBuffer_ | 始终 | 32KB（AIV_COMM_INFO_SIZE） |

- 两种模式合计 ≈ **40MB + 32KB/通信域**；仅 AIV 算子实际执行时创建。
- 独立于 CCL Buffer 申请（各自一次 HAL malloc）。

### 4.2 MC2 Buffer

- 除 CCL Buffer 内置的 **winExpBuffer_（1MB）**外，MC2（AIC 与 CCU 交互）另申请独立 **16MB `MC2_WORKSPACE_SIZE`** workspace。

## 5. Buffer 共享机制

- `ShareCCLbufferMgr`：**进程粒度单例**，按 `bufferName` 管理共享 CCL Buffer；`refCount` 引用计数，最后一个通信域释放时才真正 `free`。
- **约束：共享 buffer 的算子必须下发到同一条流**——`CheckCCLbuffConflict(bufferName, streamId)` 首次绑定后，后续不同流访问直接报 stream conflict。
- 注意共享的是**整段**（in+out+winExp 合计）而非 in/out 分别共享。

## 6. 分析与调优速查（msmemscope 视角）

### 6.1 预期曲线形态（通信内存事件视角）

```
进程启动：通信域初始化 ──> 运行期（scratch 按需分配）──> 进程结束：域销毁
   ▲ 域创建时一次大段       ▲ 需要 scratch 的算子首次执行时增量     ▲ 一次性释放
   （默认 ~401MB / 域）     （多数算子为 0，见 §3）
```

| 曲线形态 | 通信侧信号 | 含义 |
| --- | --- | --- |
| 启动时出现 size≈401MB 的一次性大段 | CCL Buffer 申请（in+out+winExp 连续段） | 每通信域一份的固定开销；多进程/多卡叠加多份 |
| 该大段此后保持平台、不随 step 变化 | alloc/free 生命周期贯穿整个通信域 | 正常——与框架池扩容（阶段性增长）的关键区别 |
| 运行中途出现增量申请 | scratch（首次执行需要 scratch 的算子）或 AIV buffer 首次启用 | 对照 §3 表格按算子/模式估算应然大小核对 |
| 增量段累积不回落 | scratch 被引用未释放 | 可疑：正常应在域销毁时释放；同 tag 本应复用取最大 |
| 多域共享时总量小于逐域累加 | 共享 buffer 整段共享、refCount 引用计数 | 正常；但受同流约束（§5） |

### 6.2 应然值核对表（正向：按场景查预期分配）

> 分析"**这个 HAL 大段是不是通信内存、该多大**"时，按通信场景与运行模式（图模式 vs OP_BASE）直接查下表——多数场景预期增量为 0，出现非 0 分配是对照 §3 详细公式深挖的信号。

| 通信场景 | 运行模式 | 预期增量（非 0 时） | 出现时机 |
| --- | --- | --- | --- |
| 通信域创建（任意场景） | — | **~401MB/域**（in 200 + out 200 + winExp 1，`HCCL_BUFFSIZE` 可调） | 通信域初始化/首次建链，一次性 |
| Ring AllReduce / Ring ReduceScatter / Ring Broadcast / 标准 AllToAll | 任 | 0（scratch=0） | — |
| ReduceScatter（Ring/Mesh） | OP_BASE | 0（910B 且满足 SDMA+RDMA 规约）否则 `inCCLbufferSize_`（200MB 级） | 首次 RS 执行 |
| ReduceScatter（Ring/Mesh） | 图模式 | `totalSize`（数据总量） | 首次图执行 |
| AllReduce / Broadcast（Mesh HD 小数据） | 图模式 + 确定性 | `totalSize×(rankSize−1)`（910B）；`totalSize×log2(2N−1)`（910_93） | 首次触发该算法 |
| AllReduce 确定性 Pipeline | 图模式 | `totalSize + N×16KB` | 首次触发 |
| AllToAllV Staged | OP_BASE | `max(workSpaceMemSize, inCCLbufferSize_, 2MB)`（≤200MB 级） | 首次执行 |
| AllToAllV Staged | 图模式 | workSpaceMemSize（当前 rank mesh 聚合 sendLength 和）；ws=0 时 2MB | 首次执行 |
| ReduceScatterV 确定性 Mesh | 图模式 | `maxCount×rankSize×dtypeSize` | 首次触发 |
| AIV 模式启用（`HCCL_OP_EXPANSION_MODE=AIV`） | — | +36MB + 4MB + 32KB/域 | 启动时创建 AIV buffer |
| MC2 算子 | — | +16MB（MC2 workspace）+ winExp 1MB 在其内部 | MC2 算子首次执行 |
| 多域共享 buffer（bufferName 非空） | — | 总占用**小于**逐域累加（整段共享、refCount） | 第二个可共享域起不再新增 |

**用法**：事件流中看到大段 HAL/PTA 分配 → 按上表定位应然大小 → 与实测对照；偏大则继续查 §3 公式细节（确定性开关、数据量、卡数。）与 §2 `HCCL_BUFFSIZE`。

### 6.3 异常点定位表

| 现象 | 检查顺序 |
| --- | --- |
| 通信内存总量异常大 | 1) 通信域数量（每域 401MB 是否叠加了多进程/多卡/多域）；2) `HCCL_BUFFSIZE` 是否被改大；3) AIV 模式（+40MB）或 MC2（+16MB）是否启用（§4）；4) scratch 是否累积 |
| 运行中途显存跳增 | 1) 按 §6.2 应然值核对表 + 运行模式查预期；2) 对照事件流实际分配核对；3) 异常放大查 `HCCL_DETERMINISTIC`/算法选择 |
| 通信 buffer 分配失败/OOM | 1) 域数量 × 401MB 是否超卡上可用；2) `HCCL_BUFFSIZE` 调小（最小 1MB）验证；3) 检查卡上是否其他进程占满 |
| 多域共享 buffer 报流冲突 | `CheckCCLbuffConflict`：共享 buffer 的算子必须下发到同一条流（§5） |
| scratch 长期不释放/总量超预期 | 正常应在域销毁时释放；同 tag 应复用取最大——持续只增不降是可疑点，检查是否有域泄漏或 tag 滥用 |

### 6.4 优化杠杆速查

| 症状 / 目标 | 首选杠杆 | 方向 | 关联 |
| --- | --- | --- | --- |
| 通信固定开销大（多域叠加） | `HCCL_BUFFSIZE` 调小（单位 MB，最小 1MB） | 每域省 `2×HCCL_BUFFSIZE` MB；可能降大报文通信性能 | §2.1 / §2.3 |
| 需省可用显存 | 多个通信域间开启共享 buffer | 整段（in+out+winExp）按名共享，refCount 释放——总占用小于逐域累加；受同流约束 | §5 |
| 运行时 scratch 占用大 | 改用 OP_BASE 模式（如非图模式） | OP_BASE 可复用 CCL buffer，多数算子 scratch=0 或=inCCLbufferSize_（ReduceScatter 非规约场景） | §3 总述 |
| 图模式下 scratch 大 | 关闭 deterministic/非默认算法 | Ring 为默认且 scratch=0；确定性 Mesh/Pipeline 等才有非零 scratch | §3.1~§3.5 |
| AIV 场景占用高 | 关闭 `HCCL_OP_EXPANSION_MODE=AIV` | 省 40MB+32KB/域（AIV 性能换显存） | §4.1 |
| MC2 场景占用高 | 确认 `MC2_WORKSPACE_SIZE`（16MB）+ winExp（1MB）为固定项；vLLM 侧调 `mega_moe_max_tokens` | workspace 与 mega_moe_max_tokens 线性相关，调小可降 | §4.2、[[vllm_ascend_memory_management]] §4 |

> 与框架池（[[pta_memory_management]]）的区别：通信段是一次性成对（alloc/free 贯穿通信域），**不随 step 增长**；HAL 事件中看到 ~401MB 大段且长时间平台，优先归因为 CCL Buffer 而非框架扩容。

> 关联：[[pta_memory_management]]（框架内存池）、[[msmemscope_data]]（HAL 事件字段口径）。