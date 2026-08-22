# PR #1390 "perf(HISA): 使用 persistent 调度优化 A3 paged block-sparse MQA decode"【PR】 数据不严谨声明

## 基本信息
- 算子类别：attention
- DSL/框架：tilelang
- 类型：PR
- 来源可信度：一手 PR 原文（ 但 PR 作者注明数据来自非标准不严谨测试，见"适用范围与警示"）

## 来源链接
- PR/出处链接：<https://github.com/tile-ai/tilelang-ascend/pull/1390>（ 已验证可达，验证日期 2026-08；2026-07-16 提交，抓取时为 open 状态）
- 优化代码查看：<https://github.com/tile-ai/tilelang-ascend/pull/1390/files>

## 问题与瓶颈
task-grid 调度下每个短 task 都有独立 mixed-block 初始化/流水排空/AIC-AIV 完成确认；当 AIC 已完成 MMA 而 AIV 仍在 reduce/mask/MTE3 输出时产生集群内 C/V 调度空洞。

## 优化方法（理论手段）
1. task-grid → persistent resident block（`T.Persistent`，physical_grid = min(logical_grid, aicore_num)）——常驻 block 复用，摊销每 task 的初始化/排空开销。
2. 新增 A3 单发布窗口 V→C release 协议（`PERSISTENT_QUEUE_DEPTH=1`，release 放在 late reduction 后、tail mask 前）——使下一 task 的 AIC 预计算覆盖上一 task 的 AIV 尾部，消除 C/V 调度空洞。
3. 闭合 mode2 广播 ready event 生命周期——避免 event 泄漏/悬挂。
4. 显式 `T.wait_flag("M","MTE1",...)` 保护跨迭代 L1/L0 buffer——防止跨迭代数据竞争。
5. mask 改 full/invalid/partial 三路——热路径无 mask 指令。

## 性能对比
PR 正文数据（Ascend910_9362，20 AIC/40 AIV，1650MHz；seq_len=1, topk=64, heads=32, index_dim=128, kv_block=128）：

| Batch | 原 task-grid | Persistent | 耗时降低 | 加速比 |
|---|---|---|---|---|
| 1 | 12.040 us | 10.940 us | 9.1% | 1.10x |
| 2 | 18.000 us | 13.620 us | 24.3% | 1.32x |
| 4 | 28.319 us | 18.700 us | 34.0% | 1.51x |
| 8 | 42.979 us | 27.839 us | 35.2% | 1.54x |

## 适用范围与警示
- 适用场景：A3 paged block-sparse MQA decode；测试 shape 为 seq_len=1, topk=64, heads=32, index_dim=128, kv_block=128。
- ** 风险说明**：PR 作者注明"该数据来自开发过程中的非标准不严谨测试"，引用时需保留此说明。
