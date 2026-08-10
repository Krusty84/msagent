# msOpProf 采集与基础数据分析

> 覆盖 torch_npu.profiler、msprof op Task Duration、SHMEMI_PROF、e2e 与 kernel 双口径；跨版本命令路由见 [算子类型路由](../case-routing.md)。

## 1. 特性介绍

基础数据分析指不依赖重型指标的常规采集与统计：算子总耗时、双路径（自定义 vs 标杆）对比、e2e vs kernel 双口径、phase 级分解。它回答的是调优最基础的问题——"这个算子到底花了多少时间、比标杆快还是慢、时间花在哪个阶段"，是所有后续深度分析（occupancy / roofline / l2cache / pipe-timeline）的入口和基线。

## 2. 特性功能

按采集途径分四类输出：

- **torch_npu.profiler → `op_statistic.csv`**：输出 profiler 统计；只在工程正式性能口径本身使用 torch_npu.profiler 时作为基线。
- **msprof op → Task Duration**（§1.2，Triton / TileLang / 单算子）：取 `OpBasicInfo.csv` 的 Task Duration 作为 kernel 耗时。
- **SHMEMI_PROF 打点 → Device Frame Table**（§1.3，SHMEM 通信算子）：`SHMEMI_PROF_START/END(pf_id)` 宏 kernel 侧打点（最多 64 block × 1024 frame），Host 侧初始化后 `aclshmemx_show_prof` 导出；frame 数据含 phase、cycles、avg_us、max_core_us、% of e2e。
- **e2e vs kernel 双口径指标**（§1.4，SHMEM）：`e2e_latency_us`（含 staging copy + barrier + kernel + sync，与 HCCL 公平对比用）与 `kernel_latency_us`（纯 kernel，内部定位用），可选第三指标 `comm_only_us`。

派生指标：`algo_bandwidth = logical_payload_bytes / latency`。`bus_bandwidth` 的换算因通信原语和拓扑而异，必须使用目标通信库文档给出的 bus factor；峰值带宽从目标设备与拓扑资料获取，不在 Skill 中固化。

## 3. 如何使用

先做能力探测：优先尝试 `msprof op --help`，不可用时尝试 `msopprof --help`；再从本机帮助确认应用是位置参数还是 `--application`，以及是否支持 `--warm-up`、`--launch-count`、`--kernel-name` 和指标选项。完整模板见 [算子类型路由 §3](../case-routing.md)。

- 命令行：
  - torch_npu.profiler：沿用工程正式 schedule；前后保持相同 warmup/active，每用例每实现独立采集。输出完整用例表，不只展示提升项。
  - msOpProf 单算子（§1.2）：常见命令为 `msprof op --kernel-name="<op>_kernel" --launch-count=20 <application> <args>`；TileLang 侧使用 `--kernel-name="main_kernel"` 或真实生成的 kernel 名。`--warm-up` 仅在帮助中存在时使用，否则在应用内部固定 warmup；禁止用 Python/Torch 计时代替正式 kernel 性能口径。
  - msprof op 上板采集（以 Ascend 950PR 上 add_custom 用例的实际用法为例）：

    ```bash
    # 输出目录必须先 chmod 755，否则 msprof 报权限错
    mkdir -p msprof_repro/baseline && chmod 755 msprof_repro msprof_repro/baseline
    # 当前官方常见形式
    msprof op --output=./msprof_repro/baseline \
      --kernel-name=add_custom --launch-count=20 \
      ./execute_add_op

    # 某些 CANN 版本支持以下形式；只在本机 --help 存在时使用
    msprof op --output=./msprof_repro/baseline \
      --application="DEVICE_ID=1 ./execute_add_op"
    ```

  - MC2/多 rank：本机帮助明确支持时使用 `msprof op`；否则回退完整 `msprof`。不同 rank 使用独立输出目录并核对全部 kernel。
  - SHMEMI_PROF（§1.3）：头文件 `utils/prof/shmemi_prof.h`；采集规范：warmup 轮跳过、多卡取平均；三阶段采集（A 仅 HCCL baseline → B 仅 SHMEM，间隔 ≥30s 新开 shell → C 离线对比；禁止同 shell 混跑）。
- 使用场景：任何调优的第一步——建立基线、双路径对比、判定收益；SHMEM 场景需要 phase 级分解与 e2e/kernel 双口径时用 SHMEMI_PROF。
- 参考示例：torch_npu.profiler 的 LayerNorm（fp16，大 shape）参考用例，自定义实现相对标杆加速比 1.49~1.89。

### A2/A3/A5 采集能力边界

| 能力 | A2 | A3 | A5 |
|---|---:|---:|---:|
| KernelScale / Occupancy / Source | ✓ | ✓ | ✓ |
| MemoryDetail / TimelineDetail | ✓ | ✓ | — |
| PipeTimeline / PcSampling | — | — | ✓ |

该矩阵来自官方 msOpProf 用户指南；安装版本可能早于文档，最终以本机帮助为准。未指定 `--kernel-name` 时，部分版本只采集第一个被调度的算子，多 kernel 程序必须逐个核对 `OpBasicInfo.csv`。

官方参考：[msOpProf 使用说明](https://github.com/Ascend/msopprof/blob/master/docs/en/user_guide/msopprof_usage.md)、[msOpProf 用户指南](https://github.com/Ascend/msopprof/blob/master/docs/en/user_guide/msopprof_user_guide.md)、[Triton-Ascend Profiling](https://github.com/triton-lang/triton-ascend/blob/main/docs/en/debug_guide/profiling.md)。

## 4. 采集完成检查

- 输出目录中包含目标 kernel 的 `OpBasicInfo.csv`，kernel 数量与运行日志一致。
- 每个指标都能追溯到完整命令和本机帮助；不支持或失败的项写入 `partial`。
- baseline 与 optimized 的设备、频率、shape、dtype、TilingKey、blockDim、launch 和指标组一致。
- 短 kernel 同时保留正式 event 计时；msOpProf 绝对耗时只用于结构与趋势解释。
