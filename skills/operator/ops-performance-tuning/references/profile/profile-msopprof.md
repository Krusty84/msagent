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

## 5. 实测坑（A5 / CANN 9.1 验证）

1. **`--kernel-name` 匹配的是 C++ mangled 符号名，不是源码函数名**。Ascend C `__global__` kernel（尤其匿名命名空间内的）实际符号形如 `_ZN12_GLOBAL__N_127gelu_eltwise_regbase_kernelEPhS0_`，用 `--kernel-name=gelu_eltwise_regbase_kernel` 会**静默过滤掉全部 launch**——应用照常跑完、只有一行 WARN、产出为空，极易误判为采集成功。正确做法：**先不加过滤全量采集一次，从 `OpBasicInfo.csv` 抄回真实 kernel 名**再决定是否过滤。
2. **输出目录布局随参数组合变化**：`--kill=on --launch-count=1` 时 CSV 直接在 `OPPROF_*/` 下；多 launch/多 kernel 时在 `OPPROF_*/device<N>/<mangled_name>/<idx>/` 下。脚本化取数必须先 `find` 探测，不要写死路径。
3. **fork 多进程程序（SHMEM 多 rank 样例）采集不完整**：实测只能捕到每个进程**第一个** kernel（后续 kernel 无产出），多 kernel 程序的 kernel 清单核对（§4 第一条）会在此场景失败，应标记 `partial` 并改用 SHMEMI_PROF 或完整 `msprof`。
4. **A5 上部分字段恒为 NA**：vector kernel 的 `aiv_mte3_active_bw`、cube kernel 的 `aiv_*` 整列、`MemoryUB.csv` 对 cube kernel 大量列均为 NA；`--aic-metrics=Occupancy` 可能只产出 `OpBasicInfo.csv` 而无独立 occupancy 数据。诊断前先确认本次产出里哪些列有效，不要用 NA 列参与判读。
5. **`--warm-up` 语义未文档化**：默认 warm-up 次数（实测 5）在 replay 模式下会重放应用前 N 次 launch，短程序反复重启进程会放大进程派生/初始化开销，解释 e2e 数字时注意区分。
6. **可忽略的噪声报错**：采集日志中 `[ERROR] <CheckInputFileValid> ... is not a file`、`Failed to load so libprofapi.so`、`child process exited 1` 与 CSV 正常产出可以并存，以 §4 的产出检查为准，不要仅凭 ERROR 行判失败。
7. **多 pipe 占用互补求和≈1 是串行无重叠的典型指纹**（如 cube 0.44+mte2 0.42+mte1 0.18）：判读时把各 pipe ratio 加总，接近 1 且无一条饱和 → 优先怀疑流水未重叠而非单 pipe 瓶颈（详见 [diagnose-pipeline.md 判读规则](../diagnose/diagnose-pipeline.md)）。
