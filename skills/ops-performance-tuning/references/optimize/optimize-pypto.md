# §6 PyPTO

> 本文件覆盖 PyPTO 的优化技术（三阶段调优框架、诊断决策树）。

## 6.1 三阶段调优框架（机制优先的体现）

1. **PHASE_FRONTEND（开箱调优）**：代码写法、TileShape、BLOCK_SIZE、基础 runtime_options；连续 5 轮无提升退出。
2. **PHASE_SWIMLANE（深度调优）**：泳道图分析、核使用率、负载均衡、合图、Stitch、调度策略、TileShape 深度调优；连续 8 轮无提升退出。
3. **PHASE_INCORE（核内调优）**：指令级优化、核内流水、特殊 Shape（如 L2 Cache 策略 NONE_CACHEABLE）。
4. 可选算法级优化：减少中间 tensor/搬运/cast、增加 reuse、改进 loop 顺序等。

## 6.2 诊断决策树（按症状索引 optimization_catalog.md 编号）

| 症状 | 优化点编号 |
|---|---|
| 气泡率 > 10% | F-3 循环次数 / F-8 unroll / S-9 Stitch / S-4,5 合图 |
| 利用率 < 50% | F-1 任务粒度 / F-9 Cube TileShape / S-1 核使用率 / S-2 核填充 |
| 负载不均 | S-3 负载均衡分析 / S-11 TileShape 深度调优 |
| 单 task 过长 | I-1~I-9（小 Shape 矩阵乘、L2 Cache 策略、冗余计算、尾轴长度、valid_shape 零填充等） |

- 性能采集模式（代码模式）：`debug_options={"runtime_debug_mode": 1}` 加在 `@pypto.frontend.jit` 上，产出 `merged_swimlane.json`、`machine_runtime_operator_trace.json`、`bubble_analysis.log`；调优结束必须还原。
- 优化点文档（F/S/I 编号）的实现时只使用目标版本公开 API，并在修改记录中附源码位置与验证命令。
