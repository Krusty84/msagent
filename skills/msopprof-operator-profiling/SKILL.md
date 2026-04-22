---
name: msopprof-operator-profiling
description: 使用 msOpProf (msprof op) 对昇腾 AI 算子进行性能调优。指导用户完成上板调优和仿真调优的数据采集、结果分析和瓶颈定位。
---

# msOpProf 算子性能调优

## 技能目标

帮助算子开发者使用 msOpProf 工具采集和分析昇腾 AI 算子的关键性能指标，定位性能瓶颈并给出优化方向。

## 两种调优模式

| 模式 | 命令 | 适用场景 |
|------|------|----------|
| **上板调优** | `msprof op [参数] ./app` | 有真实 NPU 卡，采集真实硬件性能数据 |
| **仿真调优** | `msprof op simulator --soc-version=X [参数] ./app` | 无硬件或需指令级分析，使用仿真器 |

**上板 vs 仿真互补**：上板精确捕获真实耗时、Pipe 使用、内存带宽、Cache 行为；仿真在指令流追踪、代码热点定位方面更完整。建议两种方式结合使用。

## 快速开始

### 前提条件

1. 已安装 CANN 包，环境变量已配置
2. 算子工程已编译（如需代码热点图，编译时添加 `-g` 选项）
3. 建议安装 MindStudio Insight 用于图形化查看结果

### 上板采集

```bash
# 基础采集（单算子，默认指标）
msprof op --output=./output_npu ./execute_add_op

# 指定指标采集
msprof op --aic-metrics=Roofline,Default --output=./output_npu ./execute_add_op

# 多算子采集
msprof op --launch-count=10 --kernel-name="Add|Sub" --output=./output_npu ./test
```

### 仿真采集

```bash
# 基础仿真采集（需指定芯片型号）
msprof op simulator --soc-version=Ascend910B4 --output=./output_sim ./execute_add_op

# 仅解析已有 dump 数据
msprof op simulator --soc-version=Ascend910B4 --export=./dump_dir --output=./output_sim
```

### 查看结果

- **CSV 文件**：直接用文本编辑器或 Excel 打开
- **visualize_data.bin**：用 MindStudio Insight 导入查看热力图、Roofline、流水图等
- **trace.json**：用 Chrome `chrome://tracing` 或 MindStudio Insight 查看通算流水图

## 核心参数速查

### 通用参数（上板 + 仿真）

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--output` | 结果输出路径 | 当前目录 |
| `--kernel-name` | 目标算子名（支持前缀匹配、`\|` 拼接、`*` 通配） | 第一个算子 |
| `--launch-count` | 最大采集算子数量 | 1 |
| `--launch-skip-before-match` | 跳过前 N 个算子不采集 | 0 |
| `--kill` | 采集完自动停止程序 (on/off) | off |
| `--mstx` | 使能 mstx API (on/off) | off |
| `--config` | 直接指定算子 .o 文件（JSON 配置） | - |
| `--core-id` | 指定部分逻辑核（`\|` 拼接） | 全部核 |

### 上板专用参数

| 参数 | 说明 |
|------|------|
| `--aic-metrics` | 性能指标选择（见下方指标表） |
| `--replay-mode` | 重放模式：kernel（默认）/ application / range |
| `--warm-up` | 预热次数 [0,500]，提升 AI 处理器频率 | 5 |

### 仿真专用参数

| 参数 | 说明 |
|------|------|
| `--soc-version` | 仿真器芯片型号（必选，值参考 `$INSTALL_DIR/tools/simulator`） |
| `--timeout` | 仿真超时时间 [1,2880] 分钟 |
| `--dump` | 是否保留仿真器 dump 文件 (on/off) | off |

## 性能指标体系

### 上板 aic-metrics 选项

| 指标 | 说明 | 产物 |
|------|------|------|
| `Default` | 全部基础 CSV 指标 | 7 个 CSV 文件 |
| `Roofline` | Roofline 瓶颈分析图 | visualize_data.bin |
| `Occupancy` | 核间负载分析图 | visualize_data.bin |
| `Source` | 算子代码热点图（需 -g 编译） | visualize_data.bin |
| `MemoryDetail` | Cache 性能 + 内存热力图详情 | CSV + bin |
| `TimelineDetail` | 指令流水图 + 代码热点图（仅 A2/A3） | bin |
| `PipeTimeline` | Pipe 流水图（仅 Atlas 350 加速卡） | trace.json + bin |
| `KernelScale` | 指定代码段范围采集 | CSV |
| `PcSampling` | SIMT 算子 Stall 信息（仅 Atlas 350 加速卡） | bin |
| `BasicInfo` | 仅算子基础信息 | OpBasicInfo.csv |

**组合用法**：`--aic-metrics=Roofline,Source,Default`（逗号分隔）

### 仿真 aic-metrics 选项

| 指标 | 说明 |
|------|------|
| `PipeUtilization`（默认） | 指令流水图 |
| `ResourceConflictRatio`（默认） | SET/WAIT FLAG 指令细节 |
| `PMSampling` | GM<->L1/UB/other 带宽波形图 |

## 输出产物结构

```
OPPROF_{timestamp}_XXX/
├── OpBasicInfo.csv              # 算子基础信息（名称、block dim、耗时）
├── PipeUtilization.csv          # 计算单元和搬运单元耗时占比
├── ArithmeticUtilization.csv    # Cube/Vector 指令耗时和占比
├── Memory.csv                   # UB/L1/L2/GM 读写带宽速率
├── MemoryL0.csv                 # L0A/L0B/L0C 读写带宽速率
├── MemoryUB.csv                 # UB 读写带宽速率（按 block 分）
├── L2Cache.csv                  # L2 Cache 命中率
├── ResourceConflictRatio.csv    # 资源冲突占比
├── visualize_data.bin           # 可视化数据（MindStudio Insight 导入）
├── trace.json                   # 通算流水图（MC2/LCCL 算子）
└── dump/                        # 原始数据（过程件）
```

## 分段调优策略

msOpProf 按以下顺序逐层过滤采集范围：

1. **--launch-skip-before-match** -> 跳过前 N 个算子
2. **--mstx** -> 只采集 mstx 范围内的算子
3. **--kernel-name** -> 匹配目标算子名称
4. **--aic-metrics** -> 选择要采集的指标项
5. **--kill=on** -> 采集完 --launch-count 个算子后自动停止

## Roofline 瓶颈分析

Roofline 图分析要点：
- **性能百分比 > 80%** -> Compute Bound（计算瓶颈）或 Memory Bound（内存瓶颈）
- **性能百分比 < 80%** -> Latency Bound：
  - pipeline ratio < 80% -> `latency bound: pipeline caused`
  - 最大 pipeline 为 compute 类型 -> `latency bound: compute caused`
  - 最大 pipeline 为 memory 类型 -> `latency bound: memory caused`

## 深度参考

- [上板调优深度指南](references/device-tuning-guide.md) - 上板调优完整流程、各视图详解、芯片差异、高级用法
- [仿真调优深度指南](references/simulator-tuning-guide.md) - 仿真调优完整流程、指令流水图、代码热点图、带宽波形图详解

## 经验沉淀

- [仿真模式必须使用 sim 编译的可执行文件](experiences/simulator-needs-sim-build.md) - 仿真拉起报 `signal 6 / Bad address` 的根因与解决方案
