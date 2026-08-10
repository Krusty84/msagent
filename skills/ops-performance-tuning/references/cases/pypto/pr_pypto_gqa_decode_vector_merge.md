# GQA Decode Attention：Vector 合轴 + 合图优化

## 基本信息
- 算子类别：attention
- DSL/框架：pypto
- 类型：非PR（内部案例库，cannbot-skills 官方调优案例库）
- 来源可信度：官方文档（cann/cannbot-skills 仓 `ops/pypto-op-perf-tune` 调优案例库，含 4 轮迭代失败分析与实测数字）

## 链接
- PR/出处链接：https://gitcode.com/cann/cannbot-skills/blob/master/ops/pypto-op-perf-tune/tune-frontecases/vector-axis-merge-softmax.md（ 已验证可达，验证日期 2026-08）
- 优化代码查看：非代码 MR 类案例，无 diff/files 页；出处链接即为案例原文，内含完整优化前后代码与迭代记录

## 问题与瓶颈
GQA decode attention 算子中，softmax 及前后 vector 操作（mul/amax/sub/exp/sum/div/cast）在 3D shape `[8, 4, 2048]` 下执行，产生 6 个独立 vector 子图，调度开销大。属开箱调优（PHASE_FRONTEND）阶段案例。

## 优化方法（理论手段）
①matmul 输出 `reshape(inplace)` 为 2D `[32, 2048]`；②`set_vec_tile_shapes(8, 2048)` + `set_pass_options(sg_set_scope=1)` 使所有 vector 操作在 2D 下合图执行；③reshape 回 3D 传给下一个 matmul。机制：

1. **归约轴必须不切分**：vec_tile_shapes 第二维应等于实际归约轴长度（2048），切分为 512 会产生跨子图 reduce 开销（迭代 1 实测 +16.6% 回退）；
2. **合图减少调度开销**：`sg_set_scope` 将连续 vector 操作合并为单个子图，任务数 168→137；
3. **UB 容量决定第一维上限**：`第一维 × 第二维 × dtype × tensor 总数` 不能超 UB（约 128KB 保守估计），`(32,2048)` 即因超 UB 编译失败；
4. 合轴后必须显式设 vec_tile_shapes，否则 reduce op 尾轴 32B 对齐推导失败。

## 性能对比
| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 耗时 | 275.44 us | 258.98 us | -6.0% |
| 任务数 | 168 | 137 | -18.5% |
| 子图数 | 6 | 1 | 合并为单图 |
| 精度（Max difference） | 0.000031 | 0.000031 | 无变化 |

## 适用范围与警示
- 适用：GQA decode attention 等 softmax 前后连续 vector 操作链、多子图调度开销大的场景。
- 警示：归约轴切分会回退（迭代 1 实测 +16.6%）；第一维受 UB 容量限制（约 128KB 保守估计），`(32,2048)` 超 UB 编译失败；合轴后必须显式设 vec_tile_shapes，否则 reduce op 尾轴 32B 对齐推导失败。
