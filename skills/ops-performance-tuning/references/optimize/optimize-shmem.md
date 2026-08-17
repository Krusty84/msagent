# §2 SHMEM 通信算子

> 本文件覆盖 SHMEM 通信算子的优化技术（通信 / 流水 / 同步 / 内存 / 分核 + 端到端实战数据）。

候选优先级：串行 Peer 迭代消除 → Copy-to-symmetric overlap → Double buffer → Barrier 改为有依赖证明的 signal/wait → Engine 与 chunk sweep → blockDim/负载均衡 → 对齐与减少 GM 中转。最终顺序由当前 profiling 证据决定。

## 2.1 通信优化

| 技术 | 理论说明 | 代码/参数模式 | 性能对比 |
|---|---|---|---|
| Copy-to-Symmetric-Heap Overlap | sender/receiver 分核，sender 逐 chunk 拷贝到 symmetric heap 并逐 chunk signal，receiver 轮询 signal 立即拉取；无需全局 barrier | signal 用 `magic + chunk_count` 编码 | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |
| AllToAllV 大消息禁止双拷贝 staging | 远端直接 put，避免"sendbuf→本地 symm→远端 symm"双倍流量（否则带宽封顶 ~50%） | `mte_put(sendbuf+displ, remote_symm+put_off, pe=dst)` | 不优化时带宽封顶 ~50% |
| Engine 选择 | 根据消息量、节点内/跨节点拓扑和目标库能力比较 MTE、SDMA、RDMA/RoCE | 只调用目标版本公开 API；记录引擎与拓扑 | 按消息档位实测，不固化跨环境阈值 |
| Chunk Size 调优 | chunk 受发起开销、UB、buffer 数与链路特性共同约束 | 从合法最小值到容量上限做对数或工程候选 sweep | 同时报告 e2e、kernel、algBw/busBw 和最慢 rank |
| 非连续搬运 | 固定 stride 用 `iput/iget` 或 `non_contiguous_copy_param`；多段小块合并；gather 先本地 pack 再一次 put | `iput/iget`、`non_contiguous_copy_param` | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |

## 2.2 流水线与 Double Buffer

| 技术 | 理论说明 | 代码/参数模式 | 性能对比 |
|---|---|---|---|
| Ping-Pong Buffer | 两组 UB buffer + 两组 event id 交替 | `SetFlag`/`WaitFlag<HardEvent::MTE3_MTE2>` 成对 | alltoallv 实战：ping-pong 双缓冲(95KB×2) 单轮 +16%（详见 §2.6） |
| 通信-计算 Overlap | chunk k 通信与 chunk k-1 计算重叠，尾部 drain | 文字模式：两组 data buffer + event/signal 通知 | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |
| Multi-Stage Pipeline（STAGES>2） | circular index 管理多级 buffer（L1→L0A→L0B→Compute） | 文字模式：circular index 多级 buffer | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |

## 2.3 同步优化

| 技术 | 理论说明 | 代码/参数模式 |
|---|---|---|
| Barrier→Signal/Wait | 全局 barrier 改 producer-consumer 点对点同步 | `aclshmemx_signal_op` / `aclshmem_signal_wait_until` |
| Per-Chunk Signaling | 逐 chunk signal，独立 flag offset | flag offset：`aivIndex * SYNC_FLAG_INTERVAL` |
| Phase 合并 | reduce-scatter+allgather 融合、compute+comm epilogue 同 kernel | 文字模式 |

## 2.4 内存优化

- **Avoid GM Scratch**：`get_nbi` 直接搬 UB 做 streaming reduce，替代"get→GM tmp→DataCopy UB"。
- **UB Buffer Fusion**：多步 Vector 中间结果留 UB。
- 对齐与单次搬运下限见 [data-copy.md](optimize/optimize-data-copy.md)（§1.2：512B 对齐 +30%、≥16KB）。

## 2.5 分核策略

| 技术 | 理论说明 | 代码/参数模式 | 性能对比 |
|---|---|---|---|
| Sender/Receiver Core Split | 收发角色各占一半 AIV | `core_group_num = aivNum / 2` | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |
| **AIV 内跨 PE 串行链路消除（核心模式）** | 单 AIV 内 `for peer` 串行 get+wait+add 是常见根因；两个子模式：①按源 PE 分组并行拉取+本地汇总 ②Sender put + Receiver 本地汇总 | 文字模式（`for peer` 串行 get+wait+add → 分组并行） | 8PE reduce_scatter 串行实现实测 bus bandwidth 仅 65%~71% HCCL；优化优先级第 1 位，reduce 类预期 20%+ |
| 分层规约 | PE pair 局部规约 + 二级 reduce_scatter | 文字模式 | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |
| Dynamic Block Dim | 按消息大小和 PE 数分档 | 候选由可用 AIV 数、PE 数与每核最小工作量生成 | 以最慢 rank 和尾块时延选择，不固化核数 |
| 负载均衡 / Tail 合并 | `base + (idx < extra)` 分配法；尾块合并 | `base + (idx < extra)` | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |

## 2.6 SHMEM 端到端实战数据（alltoallv 8PE full-mesh）

数据出自交付 MR !152（<https://gitcode.com/Ascend/agent-skills/merge_requests/152>）：

- 最终优化手段：向量 barrier + ping-pong 双缓冲(95KB×2) + PipeBarrier。
- **L 档 8M 带宽 68.99→79.75 GB/s（+15.6%）；e2e 1702.4→1472.7μs（−13.5%）；峰值利用率 35.2%→40.7%**；主收益来自 Round 2 ping-pong 双缓冲（+16%）。
