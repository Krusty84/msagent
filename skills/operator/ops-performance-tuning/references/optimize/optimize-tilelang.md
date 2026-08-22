# §5 TileLang-Ascend

> 本文件覆盖 TileLang-Ascend 的优化技术（按算子类型、Persistent 调度、Double Buffer 前置分析）。

## 5.1 按算子类型组织的优化技术

| 类型 | 优化技术（文字模式） |
|---|---|
| Vector 型 | NPU 内动态生成 Mask、Tile API 向量化、归约遍数融合 |
| Cube 型 | 多缓冲流水线、细粒度 Flag 同步、MMA intrinsic、L0 分块、负载均衡 |
| CV 融合型 | num_stages 流水线、批量 Softmax、Cross-core Semaphore、多 shape 适配 |

## 5.2 Persistent 调度（PR 案例，含代码模式关键词）

- **理论说明**：task-grid 调度下每个短 task 都有独立 mixed-block 初始化/流水排空/AIC-AIV 完成确认，产生集群内 C/V 调度空洞；改 persistent resident block 后跨 task C/V overlap。
- **代码/参数模式**（来自 PR 正文关键词）：`T.Persistent`（physical_grid = min(logical_grid, aicore_num)）；`PERSISTENT_QUEUE_DEPTH=1`（release 放在 late reduction 后、tail mask 前）；显式 `T.wait_flag("M","MTE1",...)` 保护跨迭代 L1/L0 buffer；mask 改 full/invalid/partial 三路（热路径无 mask 指令）。
- **性能对比**：A3 paged block-sparse MQA decode，Batch 8 时 42.979→27.839us（−35.2%，1.54x）（tilelang-ascend PR #1390：<https://github.com/tile-ai/tilelang-ascend/pull/1390>； 作者注明"非标准不严谨测试"，详见 [cases/tilelang/pr_tilelang_persistent_mqa_decode.md](cases/tilelang/pr_tilelang_persistent_mqa_decode.md)）。

## 5.3 Double Buffer 实施前置分析

实施前必须完成 DB-ANALYSIS：循环内有 MTE3？跨迭代累加器？同步方式。实现时只使用目标版本公开 API，并在修改记录中附源码位置与验证命令。
