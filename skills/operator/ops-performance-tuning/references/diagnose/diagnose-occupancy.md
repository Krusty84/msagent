# occupancy（核间负载分析）

> occupancy（§2）：msOpProf `--aic-metrics=Occupancy` 的核间负载分析；工具总览与五类对照表见 [msOpProf 采集指南](../profile/profile-msopprof.md)。

## 1. 特性介绍

occupancy 特性用于**核间负载分析**：判断多核之间任务分配是否均衡、是否存在部分核空转。当前官方用户指南列出 A2/A3/A5 支持；安装版本仍须以本机 `--help` 为准。

> **说明**：没有以 "occupancy" 命名的独立工具；该能力以 msOpProf 的 `--aic-metrics=Occupancy` 选项形式出现。

## 2. 特性功能

- **输出文件**：与 msOpProf 归档一同产出（`visualize_data.bin` 可用 MindStudio Insight 看热力图）；输出文件名随版本变化，必须从本次输出目录识别。
- **判读**：怀疑核间不均衡时开启；推荐分析路径"先看 Default 基础指标 → 开 Roofline 判 bound 类型 → 偏内存看 Memory.csv/MemoryDetail → 怀疑核间不均衡开 Occupancy → 看 MTE 与 VECTOR/CUBE 是否并行、有无 bubble"。
- **其他出现形式**：核间负载不均衡也作为 10 类瓶颈之一判读（无独立 occupancy 指标，靠 PipeUtilization 等交叉诊断）；SHMEM 侧用 SHMEMI_PROF frame 的 `max_core_us` vs `avg_us` 观察核间差异；PyPTO 侧以"负载均衡度"指标出现。

## 3. 如何使用

- 命令行：在本机帮助声明支持的指标参数中选择 `Occupancy`。以位置参数形式为例（输出目录需先 `chmod 755`）：

  ```bash
  mkdir -p msprof_occupancy && chmod 755 msprof_occupancy
  msprof op --output=./msprof_occupancy --aic-metrics=Occupancy \
    --kernel-name=add_custom ./execute_add_op
  ```

  若本机使用独立 `msopprof`、`--application` 或不同指标选项名，按 [跨版本探测模板](case-routing.md#3-msopprof-跨版本采集先探测再选命令) 切换。

- 使用场景：多核算子疑似负载不均（如各核处理数据量不同、尾核拖后腿）、 PipeUtilization 各 block 行数值差异大、SHMEM frame 的 `max_core_us` 明显高于 `avg_us` 时开启。

## 4. 分析案例

若当前归档没有 Occupancy 数据，不得从单个 block 时延外推所有核的负载。先按上一节检查本机帮助并补采；版本不支持时使用完整 block 时延、任务规模和 blockDim 作为替代证据，并降低置信度。

**两类 occupancy 问题要区分开（实测常见误判）**：

- **核间不均衡**：Block Dim 已接近物理核数，但各 block 行时延差异大（长尾核拖慢整体）——本文档主体覆盖的场景。
- **核数不足（占用率低）**：`OpBasicInfo.csv` 的 Block Dim 远小于 SoC 物理核数（如 Block Dim=1/4 vs A5 约 48 AIV），PipeUtilization 只有对应行数——此时各 block 行数值一致也不能排除 occupancy，瓶颈是"根本没用满核"，机制上对应 blockDim 提升/工作量重切（optimize-ascendc-tiling.md 的 blockDim 候选）。A5 实测物理核数与 `GetCoreNumAiv()` 等**逻辑核数接口可能不一致**（如逻辑报 54/56、实测单波满载约 48），分核参数用 blockDim sweep 实测校准，不要直接照抄接口返回值。

另外注意：SIMT/VF 样例的网格语义是 `BLOCKS × THREAD_NUM`，**只加 BLOCKS 而不重切线程映射是空操作**（新增核的起始行越界直接空转）；这两者是同一机制的原子对，必须一起改。

可给出的替代证据是"靠 PipeUtilization 交叉诊断核间均衡"判读路径在该用例上的真实应用：`PipeUtilization.csv` 按 `block_id` 0–7 分行，8 核数值几乎一致——

- before：各核 `aiv_mte2_ratio` 0.8805~0.9058、`aiv_mte2_active_bw(GB/s)` 2.2420~2.5649、`aiv_time(us)` 864.9~961.9；
- after：各核 `aiv_mte2_ratio` 0.9298~0.9422、`aiv_mte2_active_bw(GB/s)` 75.3424~75.7526、`aiv_time(us)` 21.41~21.75。

核间差异 ≤13%（仍属均衡范围），说明该用例的 8 核均分（204800/核）负载均衡，瓶颈不在核间不均衡——这正是"先看各 block 行是否一致、不一致才开 Occupancy"判读顺序的实际用法。
