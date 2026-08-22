# §1.5 Ascend C · 流水与 Double Buffer

> 本文件覆盖 Ascend C 的流水与 Double Buffer 优化技术。

| 技术 | 理论说明 | 代码/参数模式 | 性能对比 |
|---|---|---|---|
| 三级流水范式 | CopyIn/Compute/CopyOut 三级流水，TQue 级间同步，映射 MTE2/V/MTE3 硬件队列 | 文字模式：TQue 队列 + SetFlag/WaitFlag 级间同步 | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |
| Double Buffer | `InitBuffer` buffer 数=2；前提：循环 ≥2 且搬运时间不可忽略；反例：未开双缓冲时 Vector 利用率仅约 33% | `InitBuffer(queue, 2, size)`；华为云博客示例 `pipe.InitBuffer(inQueueX, 2, 256)`（<https://bbs.huaweicloud.com/blogs/433234>） | 反例：Vector 利用率仅约 33% |
| MIX 模式异步 Iterate | MIX 模式 `Iterate<false>()` 仅首次发 AIC/AIV 同步消息 | `Iterate<false>()` | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |
