# 小 Shape 矩阵乘：Vector 预处理构造标准 Shape（优化点 I-1）

## 基本信息
- 算子类别：matmul
- DSL/框架：pypto
- 类型：非PR（内部案例库，cannbot-skills 官方调优案例库）
- 来源可信度：官方文档（cann/cannbot-skills 仓 `ops/pypto-op-perf-tune`；属核内调优 PHASE_INCORE 阶段，优先级 ⭐⭐⭐ P0）

## 链接
- PR/出处链接：https://gitcode.com/cann/cannbot-skills/blob/master/ops/pypto-op-perf-tune/tune-incore/SKILL.md（§1.1）（ 已验证可达，验证日期 2026-08）；另见 https://gitcode.com/cann/cannbot-skills/blob/master/ops/pypto-op-perf-tune/shared/optimization_catalog.md（[I-1] 条目）（ 已验证可达，验证日期 2026-08）
- 优化代码查看：非代码 MR 类案例，无 diff/files 页；上述出处链接原文内含关键代码片段（见下）

## 问题与瓶颈
左右矩阵 Shape 分别为 (884736, 16) 和 (16, 16) 的矩阵乘——M 极大、K/N 极小，Cube 单元无法在如此小的 K/N 上有效流水。

## 优化方法（理论手段）
用 Vector 操作提前处理输入矩阵，通过 `concat`/`reshape` 构造标准 Shape——将 4 个重复的右矩阵与零矩阵在对角线拼成 (64, 64)，左矩阵 reshape 为 (221184, 64)，再做一次标准 matmul 后 reshape/assemble 回原形状。关键代码：

```python
pypto.set_vec_tile_shapes(64, 64)
d = pypto.full([16, 16], 0.0, pypto.DT_BF16)
c = pypto.concat([...b 与 d 对角拼接...], 0)   # (16,16) → (64,64)
a = pypto.reshape(a, [221184, 64])
pypto.set_pass_options(cube_l1_reuse_setting={-1: 9})
pypto.set_cube_tile_shapes([512, 512], [64, 64], [64, 64], True)
e = pypto.matmul(a, c, pypto.DT_BF16)
```

机制：Cube 单元按 16×16 分形工作，K=N=16 时每次 MMAD 的有效计算占比极低、流水无法展开；把多个小右矩阵沿对角拼成大矩阵后，一次标准 Shape 的 matmul 即可等价完成原计算，Cube 利用率大幅提升；配合 `cube_l1_reuse_setting` 提升 L1 复用。

## 性能对比
| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 耗时 | 500 us | 40 us | 约 12.5 倍提升 |

## 适用范围与警示
- 适用：M 极大、K/N 极小的小 shape 矩阵乘，Cube 流水无法展开的场景。
- 警示：需保证对角拼接的等价性（右矩阵可重复语义）；优先级 ⭐⭐⭐ P0，属核内调优阶段手段。
