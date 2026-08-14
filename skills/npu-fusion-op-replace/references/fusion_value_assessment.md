# 融合价值评估

本文件是 SKILL.md §1.2 的展开：如何从 profiling 数据判断一段代码到底值不值得做融合。**有 profiling 数据时读本文件**。

## 数据在哪

这些信息在 profiling 数据的 `kernel_details.csv` 或 db 文件里。关键字段：

| 字段 | 用途 |
|------|------|
| `aic_mac_ratio` | Cube 算力利用率，接近 100% = Compute-bound |
| 搬运占比相关字段 | 数据搬运耗时占比，高 = Memory-bound |
| kernel 耗时 + kernel gap | 耗时极小但总耗时大、有空闲间隙 = Launch/Sync-bound |
| 调用次数 | 高频小算子 = Small-operator fusion 候选 |

## 融合在 Roofline 上的加速上限

先判断目标算子在 Roofline 的哪一段，决定融合值不值得做：

| 算子位置 | 融合加速上限 | 机制 |
|----------|------------|------|
| **Memory-bound** | ~2×（减少 GM 读写） | 中间结果不落地，省掉 GM 带宽 |
| **Compute-bound** | 极低（融合不减少计算量） | 需要换量化或 tiling 优化，不是融合 |
| **Launch / Comm / Sync-bound** | 中等（融合减少 kernel launch 次数） | Runtime 级 Fusion 将多次提交合并为一次 |

**结论**：Memory-bound 和 Launch-bound 值得做融合；Compute-bound 别浪费时间做融合，要走量化/tiling 路线。

## 瓶颈类型现象对照

在 profiling 里观察到的现象对应哪种瓶颈：

| 瓶颈类型 | 本质 | 你观察到的现象 |
|----------|------|---------------|
| **Memory-Bound** | 数据搬运耗时 > 计算耗时，算子卡在 GM/L1/UB 带宽上 | AI Core 空闲等待数据，`aic_mac_ratio` 低，搬运占比高 |
| **Compute-Bound** | 计算耗时 > 数据搬运耗时，算子卡在 Cube/Vector 算力上 | `aic_mac_ratio` 接近 100%，搬运流水线有空闲 |
| **Launch/Sync-Bound** | Host 侧下发开销、核间同步、通信等待占主导 | NPU trace 中出现明显空闲间隙；小 shape 下 kernel 耗时极小但总耗时大 |

## 三类融合模式，什么时候尝试

### Cube-Vector fusion

合并计算 + 向量/数据搬运类操作。

- **收益大**：减少中间结果落地、改善 cache/locality 时。
- **收益有限**：数据量小时，省下的搬运抵不上融合开销。

### Vector-Vector fusion

合并向量类操作。

- **收益明显**：重复的 load/store/cast 开销被消掉时。
- 常见于一连串的 elementwise / cast / reduce 操作。

### Small-operator fusion

- **收益场景**：launch/下发开销占主导，尤其高频微小算子。
- NPU trace 里看到一堆耗时极小但数量巨大的 kernel，就是这种。

## 操作流程

1. 在 `kernel_details.csv` 找高频算子序列（按耗时排序，找 top）。
2. 确认这些算子**属于同一个模块、上下衔接**——跨模块的算子在硬件上不一定连续执行，拼起来没意义。
3. 按 pattern（如 `slice+matmul+gelu`）去知识图谱或 `scripts/query_torch_npu_api.py search <关键词>` 找现成融合 API。
4. 评估能否等价替换：
   - 有现成 API → 列入候选替换清单。
   - 没有现成 API → 指出"建议做自定义融合算子"，给性能提升估计（参照 Roofline 加速上限表）。
5. 把识别结果按 SKILL.md §1.3 输出格式写入识别报告。

## 估算收益时注意

- Memory-bound 的 ~2× 是上限，实际取决于中间结果落地占比，可能打不到。
- Launch-bound 的收益取决于 launch 次数减少了多少，看调用次数字段。
- Compute-bound 别估高，融合不减少计算量，最多省一点 launch 开销。
