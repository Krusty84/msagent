# SHMEM reduce_scatter：AIV 内跨 PE 串行链路消除【非 PR：agent-skills 官方优化模式库】

## 基本信息
- 算子类别：communication
- DSL/框架：shmem
- 类型：非PR（官方优化模式库文档）
- 来源可信度：官方文档（Ascend/agent-skills 仓优化模式文档）

## 来源链接
- 出处链接：<https://gitcode.com/Ascend/agent-skills/blob/master/community/Op/shmem-ops-performance-optim/references/optimization-patterns.md>（§5.2、§6； 已验证可达，验证日期 2026-08）
- 优化代码查看：同上出处链接（文档每条含瓶颈/优化/来源 example/错误 vs 正确代码；该案例为优化模式文档条目，非独立 PR，无单独 diff 页）
- 关联知识：[../../optimize-shmem.md](../../optimize-shmem.md) §2.5、§2 头部优化优先级表

## 问题与瓶颈
SHMEM reduce 类算子（reduce_scatter 等）最常见的性能根因是**单 AIV 内 `for peer` 串行 get+wait+add**：每个 AIV 依次向各源 PE 拉取数据、等待、累加，形成长度正比于 PE 数的串行链路，通信与累加完全无法并行。实测瓶颈数据：8PE reduce_scatter 串行实现实测 bus bandwidth 仅为 **HCCL 的 65%~71%**。

## 优化方法（理论手段）
两个优化子模式：①**按源 PE 分组并行拉取 + 本地汇总**（多 AIV 分工拉不同 PE，本地 reduce）；②**Sender put + Receiver 本地汇总**（发送方主动 put 到接收方 symmetric heap，接收方只做本地归约）。机制：

1. 串行 `get+wait+add` 的端到端时间 ≈ n_pes ×（单跳时延+等待），总线带宽被单 AIV 串行占用，其余核闲置；分组并行后链路长度降为 O(1)~O(log)，带宽按参与核数成倍放大；
2. Sender put 模式把"拉+等"变成"推"，消除 receiver 端的 wait 气泡，与"串行 Peer 迭代消除"被列为 shmem-ops-performance-optim **优化优先级第 1 位**（reduce 类预期收益 20%+）。

## 性能对比
optimization-patterns.md §5.2 原文实测：

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| bus bandwidth（8PE reduce_scatter 串行实现） | HCCL 的 65%~71% | 原文未附实测终值 | — |

按 §6 优化优先级表，串行 Peer 迭代消除在 reduce 类算子**预期收益 20%+**（原文为预期值，非该案例实测终值）。

## 适用范围与警示
- 适用算子：SHMEM reduce 类算子（reduce_scatter 等）；瓶颈随 PE 数增长而加剧（串行链路长度正比于 PE 数）。
- 警示：表中的 20%+ 为优化优先级表给出的**预期值**，并非本案例实测终值；串行实现的 65%~71% 为 8PE 下的实测对照，迁移到其他 PE 规模需重新实测。
