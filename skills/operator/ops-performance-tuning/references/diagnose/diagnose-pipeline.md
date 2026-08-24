# pipetimeline（流水时间线）

> pipetimeline（§5）：PipeUtilization / ResourceConflictRatio / simulator 指令流水的采集与判读；工具总览与五类对照表见 [msOpProf 采集指南](../profile/profile-msopprof.md)。

## 1. 特性介绍

流水时间线分析：观察各条硬件流水线（MTE1/MTE2/MTE3/Cube/Vector/Scalar/FixPipe）的占用与并行情况，定位"哪条 pipe 是瓶颈、pipe 之间是否有 bubble（空等）、搬运与计算是否重叠"。

## 2. 特性功能

- **`PipeUtilization.csv`**：MTE1/MTE2/Cube/Vector/Scalar/FixPipe 各 pipe 的 time、ratio 与 active_bw（瓶颈数据源之一）。
- **`ResourceConflictRatio.csv`**：各 pipe wait_ratio 与 vec load/store 冲突比，用于确认"谁在等谁"。
- **simulator 侧 `instr_exe.csv`**（各 pipe cycles 占比，定瓶颈类型）+ **`code_exe.csv`**（热源码行，定位置），Cube 核与 Vector 核分开看 4 张表。
- **trace.json**（PipeTimeline，当前官方指南列为 **A5** 能力）；`visualize_data.bin` 用 MindStudio Insight 看流水图。

## 3. 如何使用

- 命令行：
  - msOpProf `PipeTimeline`：A5 Pipe 流水图；`TimelineDetail`：A2/A3 的指令流水与上板热点增强，对二级指针、Triton、MC² 等有限制。参数名和组合限制必须以安装版本帮助为准。
  - `msprof op simulator --soc-version=Ascend910B1 {cmd}`：仿真侧 per-instruction pipe 采集。
  - 上板默认采集即含 `PipeUtilization.csv`（7 组 aic-metrics 之一）；以 Ascend 950PR 上 add_custom 用例为例（输出目录需先 `chmod 755`）：

    ```bash
    mkdir -p msprof_repro/baseline && chmod 755 msprof_repro msprof_repro/baseline
    msprof op --output=./msprof_repro/baseline \
      --kernel-name=add_custom ./execute_add_op
    ```

  跨版本能力矩阵与 `msprof op`/`msopprof` 切换方式见 [算子类型路由 §3](../case-routing.md)。

- 使用场景：怀疑流水断档/串行（搬运与计算未重叠）、需要确认瓶颈 pipe、评估 Double Buffer / 三段流水改造收益时。

判读方法：

- 看 MTE 与 VECTOR/CUBE 是否并行、有无 bubble（msOpProf 推荐分析路径）；
- simulator 诊断速查：Cube `WAIT_FLAG_DEVI`>50% 且 `MMAD`<5% → Cube 空等 Vector（→triton 优化点 19/21）；Vector `SCALAR`>30% → i32 比较标量降级（→6/5/17）；MTE2/MTE3 dominant → 访存 bound（→7/21/10）；BAR 高 → 同步（→19/增大 tile）；`MMAD`>50% → 真·硬件极限；
- Triton 侧 hardware_constraints 材料提供 MTE/Vector/Scalar 流水并行的理想流水、破坏流水的常见操作、流水检查方法；
- 自建等价物：SHMEMI_PROF Device frame（phase 级 avg_us / % e2e，等价自建 pipeline 分解）；PyPTO 泳道图 `merged_swimlane.json` + `bubble_analysis.log` 气泡率（终止条件"核心利用率 > 80% 且气泡率 < 10%"）。

## 4. 判读规则

1. busy ratio 高且 active bandwidth 接近目标设备可达上限，才支持带宽受限结论。
2. busy ratio 高但 active bandwidth 很低时，优先检查小包、非对齐、指令数量和频繁同步，不宣称 HBM 饱和。
3. Vector/Cube ratio 低时结合等待比例和时间线区分“没有计算量”与“计算 Pipe 被上游阻塞”。
4. 修改后必须同时验证总时延、active bandwidth、等待比例和精度；单一 ratio 改善不足以采纳。
5. **所有 pipe 占用都低（均值 <50%）不是"无 bound"**：排除 compute 与带宽饱和后，典型为流水未重叠、频繁同步（SetFlag/WaitFlag/PipeBarrier/CrossCore flag）或小包轮询等待，按 latency/synchronization bound 处理；bound_analyzer.py 对此输出 `LATENCY/SYNC 可疑`。
6. **多 pipe 占用互补求和≈1 是串行无重叠的指纹**（如 cube 0.44+mte2 0.42+mte1 0.18 ≈ 1）：各 pipe 轮流忙、无一饱和，说明搬运与计算完全串行，机制上对应单缓冲 + 逐级 SetFlag/WaitFlag，候选 Double Buffer/流水重叠。
7. Scalar pipe 占用高（>90%）而 vec/MTE 低时，优先怀疑逐元素标量访问（GetValue/SetValue）、标量地址计算或控制流开销，对应 optimize-api-usage.md 的标量削减方向。
