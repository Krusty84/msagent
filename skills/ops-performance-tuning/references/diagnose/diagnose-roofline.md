# roofline

> roofline（§3）：msOpProf `--aic-metrics=Roofline` 瓶颈分析与 bound 类型判读；工具总览与五类对照表见 [msOpProf 采集指南](../profile/profile-msopprof.md)。

## 1. 特性介绍

Roofline 瓶颈分析：把算子的实际算力/带宽投射到硬件理论上限的 roofline 图上，判定算子是 **Compute Bound / Memory Bound / Latency Bound**，从而决定优化方向（提算力利用率、减访存流量、还是消延迟/断档）。

## 2. 特性功能

- **输出文件**：`visualize_data.bin` 用 MindStudio Insight 看 Roofline 图；瓶颈数据源 CSV 含 `ArithmeticUtilization.csv`、`Memory.csv` 等。
- **判读结论**：bound 类型三分类（Compute / Memory / Latency Bound），与 Default 基础指标绑定使用。

## 3. 如何使用

- 命令行：若本机帮助支持，使用 msOpProf `Roofline` 并按该版本要求与 `Default` 组合。以位置参数形式为例（输出目录需先 `chmod 755`）：

  ```bash
  mkdir -p msprof_roofline && chmod 755 msprof_roofline
  msprof op --output=./msprof_roofline --aic-metrics=Roofline \
    --kernel-name=add_custom ./execute_add_op
  ```

  参数形式和芯片支持边界见 [跨版本探测模板](case-routing.md#3-msopprof-跨版本采集先探测再选命令)。判读顺序：先看 Default 基础指标 → 开 Roofline 判 bound 类型。
- 使用场景：拿到基线后、动手优化前判定 bound 类型；也用于优化后确认是否已贴近硬件上限（继续调是否还有空间）。

无 Roofline 图时的**利用率式自建判据**：

- 通信算子 `utilization = bus_bw / peak_bw`；
- CATLASS "Cube 利用率 ~90%+ 即接近 roofline，再提速受硬件结构约束"；
- MTE2 Bound 先判断是否已达理论带宽；
- Triton simulator 侧真·硬件极限判据：`MMAD`>50%。

## 4. 分析案例

若当前归档中没有 Roofline 专属输出，不得推断 Roofline Bound。先按上一节检查本机帮助并补采；版本不支持时改用 OpBasicInfo、PipeUtilization、带宽与算量共同给出 `UNRESOLVED` 或低置信度结论。

可作为替代证据的是 Roofline 瓶颈数据源 CSV（`ArithmeticUtilization.csv`、`Memory.csv`）在该用例上的真实 before/after 对照——它支撑了"Memory Bound（搬运方式）"的判定：

| 指标（字段） | before | after |
|---|---|---|
| `ArithmeticUtilization.csv` → `aiv_vec_ratio` | 0.0594~0.0660（Vec 仅约 6%） | 0.2041~0.2074（约 20.5%） |
| `Memory.csv` → `aiv_gm_to_ub_bw(GB/s)` | 2.031~2.258 | 70.16~71.29 |
| `Memory.csv` → `GM_to_UB_bw_usage_rate(%)` | 1.245~1.384 | 43.01~43.70 |

（均为 block_id 0–7 各行范围）

判读：before 时计算利用率约 6%、GM→UB 带宽使用率仅约 1.2~1.4%——算力与带宽都离 roofline 上限很远，属"搬运方式瓶颈"（400B 非对齐小包）而非真带宽上限；after 改 16KB 对齐大包 + Double Buffer 后带宽使用率升至约 43%、带宽 2.0→70.6 GB/s（约 33 倍），Vec 利用率同步升至约 20.5%，仍非 Compute Bound——即"瓶颈=搬运方式，不是带宽上限也不是计算"。
