# 仿真调优深度指南

## 完整调优流程

### 1. 编译准备

- 常规算子：正常编译（添加 `-g` 可查看代码行映射）
- catlass 模板库算子：编译脚本需增加 `--simulator` 选项

```bash
bash scripts/build.sh --simulator 00_basic_matmul
```

### 2. 数据采集

#### 基本用法

```bash
# 指定芯片型号 + 可执行文件
msprof op simulator --soc-version=Ascend910B4 --output=./output_sim ./execute_add_op

# 指定芯片型号 + .o 文件（需设置 LD_LIBRARY_PATH）
export LD_LIBRARY_PATH=${INSTALL_DIR}/tools/simulator/Ascend910B4/lib:$LD_LIBRARY_PATH
msprof op simulator --config=./add_test.json --output=./output_sim
```

#### --soc-version 取值

参考 `${INSTALL_DIR}/tools/simulator/` 路径下的仿真器目录名，例如：
- `Ascend910B1`、`Ascend910B4`（A2 系列）
- `Ascend910_9391`（A3 系列）
- `Ascend310B4`（310B 系列）
- `Ascend950`（A5 / Atlas 350 加速卡）

也可通过设置 `LD_LIBRARY_PATH` 替代 `--soc-version`。

#### 超时控制

对于数据量大、计算重复的算子，完整仿真耗时很长。可设置超时只获取部分数据：

```bash
# 最多仿真 1 分钟
msprof op simulator --soc-version=Ascend910B4 --timeout=1 --output=./output_sim ./app
```

超时后工具会自动终止仿真进程并进入解析。取值范围 1-2880 分钟。

#### 解析已有 dump 数据

```bash
msprof op simulator --soc-version=Ascend910B4 --export=./dump_dir --output=./output_sim
```

**注意**：
- `--export` 指定的文件夹只允许存放多核数据及 `aicore_binary.o`
- 需将 .o 文件手动重命名为 `aicore_binary.o`
- 仅提供 dump 文件时，无法生成代码行映射

#### 核选择

```bash
# 只解析 0 号和 31 号核
msprof op simulator --soc-version=Ascend910B4 --core-id="0|31" --output=./output_sim ./app
```

适用于算子分布均匀的场景，减少解析耗时。取值范围 [0,49]。

### 3. 结果查看

仿真输出目录结构：

```
OPPROF_{timestamp}_XXX/
├── core0/
│   ├── tracing.json            # 0 号核的指令流水图
│   └── ...
├── core1/
│   └── ...
├── visualize_data.bin          # 汇总的可视化数据
├── code_exe.csv                # 代码执行情况（按核分）
├── instr_exe.csv               # 指令执行情况（按核分）
└── dump/                       # 原始仿真数据
```

## 功能视图详解

### 指令流水图

通过 MindStudio Insight 或 Chrome `chrome://tracing` 查看。

展示算子在仿真器中各流水线单元的指令执行时序：
- **SCALAR**：标量指令
- **VECTOR**：向量计算指令
- **CUBE**：矩阵乘指令
- **MTE**：数据搬运指令
- **FIXP**：定点指令
- **FLOWCTRL**：流控指令

**关键分析点**：
- 各 Pipe 之间是否存在长时间空闲（bubble）
- 搬运指令（MTE）和计算指令（VECTOR/CUBE）是否充分并行
- SET_FLAG/WAIT_FLAG 同步指令是否造成不必要等待

**--aic-metrics 对流水图的影响**：
- `PipeUtilization`：只显示指令流水，不含同步事件
- `ResourceConflictRatio`：显示指令流水 + SET/WAIT FLAG 同步指令细节
- 默认同时开启两者

### 算子代码热点图

比上板热点图提供更丰富的指令级分析：

| 功能 | 说明 |
|------|------|
| 源码与指令映射 | 算子源码与 PC 指令集的对应关系 |
| GPR Count | 寄存器使用数量（A5 支持） |
| GPR Status | 寄存器读写状态（A5 支持） |
| UB Conflict Read/Write | UB Bank 上读写冲突 |
| Vector 利用率 | Vector 计算单元利用率 |
| Process Bytes | 与 GM 有关的数据搬运量 |
| 执行次数 | 每个指令/代码行的执行次数 |
| Cycles | 每个指令/代码行的耗时周期 |

### 内存通路吞吐率波形图

通过 `--aic-metrics=PMSampling` 开启。展示以下通路的带宽波形：

| 通路 | 说明 |
|------|------|
| GM <-> L1 | Global Memory 与 L1 之间 |
| GM <-> UB | Global Memory 与 Unified Buffer 之间 |
| GM <-> other | Global Memory 与其他存储之间 |

以 1us 为时间间隔，计算搬运数据量除以时间得到带宽值，共 6 张带宽图（读/写各 3 张）。

> **注意**：PMSampling 解析全部核，`--core-id` 对其不生效。

## 仿真特有技巧

### 1. 分核分析

对于多核算子，先全核采集确认哪些核有问题，再用 `--core-id` 只解析目标核：

```bash
# 先全核快速确认
msprof op simulator --soc-version=Ascend910B4 --timeout=1 --output=./quick ./app

# 再指定核深度分析
msprof op simulator --soc-version=Ascend910B4 --core-id="0" --output=./detail ./app
```

### 2. 超时截断

大算子完整仿真可能数小时，用 `--timeout` 截断获取部分流水即可定位大多数问题：

```bash
msprof op simulator --soc-version=Ascend910B4 --timeout=5 --output=./output ./big_op
```

### 3. dump 文件复用

仿真 dump 文件可以反复解析，无需重新仿真：

```bash
# 第一次仿真（保留 dump）
msprof op simulator --soc-version=Ascend910B4 --dump=on --output=./output ./app

# 后续直接从 dump 解析
msprof op simulator --soc-version=Ascend910B4 --export=./output/dump --output=./output2
```

## 仿真 vs 上板热点图差异

| 功能 | 上板 | 仿真 |
|------|------|------|
| GPR Count | 不支持 | 支持 |
| L2Cache 命中率（代码行/指令维度） | 支持 | 不支持 |
| Process Bytes | 支持 | 支持 |
| UB Conflict | 不支持 | 支持 |
| Vector 利用率 | 不支持 | 支持 |
| Cycles 耗时 | 不支持 | 支持 |
| 执行次数 | 支持 | 支持 |
| Core 信息 | 不支持 | 支持 |

## 常见问题

### 仿真器版本获取

```bash
ls ${INSTALL_DIR}/tools/simulator/
```

### 仿真运行时间过长

- 使用 `--timeout` 截断
- 使用 `--core-id` 只解析部分核
- 检查算子 block_dim 是否过大

### 代码行映射缺失

- 确认编译时添加了 `-g` 选项
- 使用 `--export` 模式时，确认 dump 目录中有 `aicore_binary.o`

### PMSampling 数据为空

- PMSampling 默认不开启，需显式指定 `--aic-metrics=PMSampling`
- PMSampling 解析全部核，`--core-id` 不影响
