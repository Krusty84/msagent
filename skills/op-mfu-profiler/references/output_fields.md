# 输出字段说明

`msprof-analyze --agent -m operator_mfu` 在 `-o`（默认 `-d`）路径下生成 `cluster_analysis_output`：`--export_type text` 时产出 `OperatorMfu/operator_mfu_kernel_{rank_id}.xlsx`、`operator_mfu_module_{rank_id}.xlsx`；`--export_type db`（默认）时产出 `cluster_analysis.db`，内含 `OperatorMFU`/`ModuleMFU` 表。下表字段对两种格式都适用（`OperatorMFU` / `operator_mfu_kernel` 对应 kernel 级明细，`ModuleMFU` / `operator_mfu_module` 对应 module 级统计）。

## OperatorMFU（kernel 级 MFU 明细）

| 字段名 | 说明 |
| --- | --- |
| `rank_id` | Rank ID |
| `op_name` | 框架侧算子名称 |
| `kernel_name` | Device 侧 kernel 名称 |
| `kernel_start(ns)` | kernel 开始时间，单位 ns |
| `kernel_end(ns)` | kernel 结束时间，单位 ns |
| `kernel_duration(ns)` | kernel 执行时长，单位 ns |
| `mfu` | MFU 比值 |
| `actual_tflops` | 按当前 kernel 时长计算的实际 TFLOPS |
| `chip_peak_tflops` | 按 kernel 输入数据类型匹配到的芯片理论峰值，单位 TFLOPS |
| `flops` | 采集侧记录的算子 FLOPs |
| `flops_op_name` | 采集侧记录 FLOPs 时对应的算子名称 |
| `input_shapes` | kernel 输入 shape |
| `output_shapes` | kernel 输出 shape |

## ModuleMFU（module 级 MFU 统计，需 `Module` domain msTX）

| 字段名 | 说明 |
| --- | --- |
| `rank_id` | Rank ID |
| `parent_module` | 上层 Module 名称 |
| `module` | 最底层 Module 名称 |
| `op_name` | 框架侧算子名称 |
| `kernel_list` | 框架侧算子下发到 Device 侧执行的 kernel 序列 |
| `total_kernel_duration(ns)` | 框架侧算子对应 Device 侧 kernel 运行总时间，单位 ns |
| `avg_kernel_duration(ns)` | 框架侧算子对应 Device 侧 kernel 平均运行时间，单位 ns |
| `op_count` | 框架侧算子在采集周期内运行的次数 |
| `avg_mfu` | 按同一 kernel 位置聚合得到的平均 MFU，百分比格式 |

## MFU 计算逻辑（细节）

```
actual_tflops = FLOPs / (kernelDuration(ns) * 1e-9) / 1e12
mfu           = FLOPs / (kernelDuration(ns) * 1e-9) / chipPeakFLOPS
```

- `chipPeakFLOPS`：当前芯片、当前数据类型对应的理论峰值。
- 数据类型来源：解析侧用「同一 FLOPs 记录时间范围内首个 kernel 的输入数据类型」匹配峰值。
- 兜底：若输入类型解析不出来，默认按 **FP16** 处理。

### 芯片理论峰值算力（参考）

msprof-analyze 解析时从 profiling 数据读 `ai_core_num`/`aic_frequency` 自动估算 `chipPeakFLOPS`，无需手填。下表供结果解读时交叉核对（单位 TFLOPs/s）：

| 系列 | 芯片 / 形态 | FP16/BF16 理论峰值 |
| --- | --- | --- |
| A2 | 华为 Ascend 910B1 | ≈ 378.88 TFLOPs/s |
| A2 | 华为 Ascend 910B2 | ≈ 353.89 TFLOPs/s |
| A2 | 华为 Ascend 910B3 | ≈ 294.91 TFLOPs/s |
| A2 | 华为 Ascend 910B4 | ≈ 270 TFLOPs/s |
| A3 | 一体机 | ≈ 560 TFLOPs/s |
| A3 | 超节点 | ≈ 752 TFLOPs/s |

MFU 区间评估参考：

| MFU 范围 | 评估 |
| --- | --- |
| < 20% | 算子远未吃满算力，可能受内存带宽、launch overhead、shape 不规则拖累 |
| 30%–60% | 中等偏上，许多通用工作负载大致在此区间 |
| > 70% | 算子形状、并行度和实现都接近设备上限 |
