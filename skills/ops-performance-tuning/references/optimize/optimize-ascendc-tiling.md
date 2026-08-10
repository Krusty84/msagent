# §1.1 Ascend C · Tiling 与核间负载

> 本文件覆盖 Ascend C 的 Tiling 与核间负载均衡优化技术。

| 技术 | 理论说明 | 代码/参数模式 | 性能对比 |
|---|---|---|---|
| blockDim 候选 | 分别读取目标 SoC 的 AIV、AIC 和 MIX 核组能力；候选不得超过该 kernel 类型可用核数 | 从当前工具链/运行时取核数，按工作量 sweep | 以最长 block 时延和 event 总时延共同选择 |
| L2 工作集切分 | 仅在重复 GM 流量且工作集超过目标设备可用 L2 时，按当前设备容量规划 tile | 记录每 tile 输入、输出、中间量与并发核占用 | 验证 GM 流量、L2 hit 与总时延，不固化容量或带宽常数 |
| 核间负载均衡 | 将整块与尾块工作量显式分配，避免少数 block 长尾 | `base = total / blocks`，前 `total % blocks` 个 block 多处理一个单位 | 比较每 block 任务量与最大/平均时延 |
