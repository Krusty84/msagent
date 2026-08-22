# §1.2 Ascend C · 数据搬运

> 本文件覆盖 Ascend C 的数据搬运优化技术。

| 技术 | 理论说明 | 代码/参数模式 | 性能对比 |
|---|---|---|---|
| 合并小搬运 | DMA 发起开销可能使小包 active bandwidth 偏低；阈值随 SoC、方向和 API 变化 | 在 UB 容量与正确性允许范围内 sweep 搬运粒度 | 同时比较指令数、active bandwidth 与总时延 |
| 地址与长度对齐 | 使用目标 API 要求的最小对齐，并将更大对齐作为候选而非通用规则 | 从目标版本 API 文档获取对齐约束 | 逐档实测，禁止跨 SoC 复用收益数字 |
| DataCopyParams 批量搬运 | 当布局可用 block/stride 描述时，用一次参数化 DMA 替代逐行循环 | `blockCount/blockLen/srcStride/dstStride` 全部按当前 API 单位换算 | 精度通过后检查 DMA 指令数和 MTE 时延 |
