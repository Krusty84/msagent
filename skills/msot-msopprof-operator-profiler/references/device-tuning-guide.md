# 上板调优深度指南

## 完整调优流程

### 1. 编译准备

算子编译时添加 `-g` 选项以生成调试信息（代码热点图、Cache 热力图跳转必需）：

```bash
# 在 Kernel 侧 CMakeLists.txt 首行添加
add_ops_compile_options(ALL OPTIONS -g)

# 然后重新编译部署
bash ./build.sh
MY_OP_PKG=$(find ./build_out -maxdepth 1 -name "custom_opp_*.run" | head -1) && bash $MY_OP_PKG
```

> **注意**：`-g` 会附带调试信息，需限制二进制文件访问权限。不支持 `-O0` 编译选项。

### 2. 数据采集

#### 基本用法

```bash
# 单算子全量采集
msprof op --output=./output ./execute_add_op

# 指定指标采集
msprof op --aic-metrics=Roofline,Source,MemoryDetail,Default --output=./output ./execute_add_op
```

#### 多算子场景

```bash
# 采集前 10 个 Add 或 Sub 算子
msprof op --launch-count=10 --kernel-name="Add|Sub" --output=./output ./test

# 跳过前 3 个算子，采集第 4 个开始的 5 个算子
msprof op --launch-skip-before-match=3 --launch-count=5 --output=./output ./test
```

#### 基于 .o 文件（无需可执行程序）

```bash
msprof op --config=./add_test.json --aic-metrics=Default --output=./output
```

JSON 配置文件格式参见 extended_functions.md。

#### 重放模式

| 模式 | 命令 | 特点 |
|------|------|------|
| `kernel`（默认） | `--replay-mode=kernel` | 单个算子核函数多次重放，需内存备份 + L2Cache 清理 |
| `application` | `--replay-mode=application` | 保留 L2Cache 状态，多次启动进程采集 |
| `range` | `--replay-mode=range --mstx=on` | 基于 mstxRangeStart/End 框定范围整体重放 |

**range 模式限制**：
- 需配合 `--mstx=on`
- 仅 A2/A3 系列
- 不支持 MC2/LCCL 通算融合算子
- 不支持与 `--kill=on`、`MemoryDetail`、`TimelineDetail`、`Source` 同时使能

### 3. 结果查看

#### CSV 文件分析

| CSV 文件 | 关键字段 | 分析重点 |
|----------|----------|----------|
| OpBasicInfo.csv | 算子名称、block_dim、总耗时 | 整体耗时是否正常 |
| PipeUtilization.csv | 各 pipe 耗时占比 | 计算 vs 搬运占比 |
| ArithmeticUtilization.csv | Cube/Vector 指令耗时 | Cube 或 Vector 利用率 |
| Memory.csv | UB/L1/L2/GM 读写带宽 | 带宽是否达到理论峰值 |
| MemoryUB.csv | 按 block 分的 UB 带宽 | 核间负载是否均衡 |
| L2Cache.csv | L2 Cache 命中率 | 命中率是否过低 |
| ResourceConflictRatio.csv | Bank conflict 占比 | 资源冲突是否严重 |

#### MindStudio Insight 图形化查看

1. 安装 MindStudio Insight
2. 打开后点击 Import Data，导入 `visualize_data.bin`
3. 在 Details 页面查看各类图表

## 功能视图详解

### 计算内存热力图

以资源维度展示：
- **核间负载分析（Core Occupancy）**：各物理核的耗时、吞吐量、Cache 命中率对比。若最大/最小差距 > 10%，提示负载不均衡
- **计算负载分析（Compute Workload）**：Cube/Vector 计算资源利用率
- **内存负载分析（Memory Workload）**：MTE 各通路活跃带宽值

### Roofline 瓶颈分析图

按算子类型（Vector/Cube/Mix）和芯片型号呈现不同视图：

| 视图 | Vector | Cube | Mix |
|------|--------|------|-----|
| GM/L2 视图 | Y | Y | Y |
| Vector 内存单元视图 | Y | - | Y |
| Vector 内存通路视图 | Y | - | Y |
| Vector Pipeline 视图 | Y | - | Y |
| Cube 内存单元视图 | - | Y | Y |
| Cube 内存通路视图 | - | Y | Y |
| Cube Pipeline 视图 | - | Y | Y |

**分析要点**：
- 横轴 = 算术强度（Ops/Byte），纵轴 = 计算性能（TOps/s）
- 屋顶线 = 理论最大计算性能，带宽斜线 = 理论最大带宽
- 实际点与屋顶线的距离 = 性能提升空间

**瓶颈判定**：
- 性能百分比 > 80% -> Compute Bound 或 Memory Bound
- 性能百分比 < 80% -> Latency Bound（需区分 pipeline/memory/compute caused）

### Cache 热力图

展示 L2 Cache 访问情况：
- Hit/Miss 分布，可跳转至源码界面
- 需开启 `--aic-metrics=Source` 并添加 `-g` 编译选项
- 不适用于 Atlas 推理系列产品
- MC2/LCCL 算子不支持

### 通算流水图

适用于 MC2/LCCL/ASC 通算融合算子：

| 字段 | 说明 |
|------|------|
| AI CORE | 算子在 AI Core 上的整体运行 |
| AI CPU | 算子在 AI CPU 上的运行（仅 MC2） |
| AIC BLOCK | Cube 核运行情况 |
| AIV BLOCK | Vector 核运行情况 |
| HCCL | 多卡集合通信流水（仅 MC2） |
| AscendC API | 用户打点 API 在每个 block 上的耗时 |

通过 `AscendC::PrintTimeStamp` API 可在算子 block 上标记耗时。

### Pipe 流水图

展示算子各 Pipe 的运行情况（仅 Atlas 350 加速卡）。
支持通过 `AscendC::MarkStamp` 接口在任意代码处打点标识流水范围。

### 算子代码热点图

左侧：源码维度 -> L2Cache 命中率、GM 搬运量、指令数
右侧：指令维度 -> 具体指令的命中率、搬运量、执行次数

**芯片差异**：

| 功能 | A2/A3 | Atlas 350 加速卡 |
|------|-------|------------------|
| 源码/PC/PIPE | 支持 | 支持 |
| 执行次数 | 支持 | 支持 |
| GPR Count | 不支持 | 支持 |
| L2Cache 命中率 | 支持 | 不支持 |
| Process Bytes | 支持 | 支持 |
| Stall Sampling | 不支持 | 支持 |

## 芯片型号对照

| 芯片系列 | 型号示例 | 说明 |
|----------|----------|------|
| Ascend910B (A2) | 910B1/B2/B3/B4/B2C/B4-1 | 训练/推理 |
| Ascend910_93 (A3) | 9391/9392/9381/9382/9372/9362 | 训练/推理 |
| Ascend310B | 310B1/B2/B3/B4 | 推理 |
| Ascend310P | 310P1-P5/P7 | 推理 |
| Ascend950 (A5) | - | Atlas 350 加速卡 |

## 调优策略建议

1. **先用 Default 采集全量 CSV**，从 OpBasicInfo 看总耗时
2. **开启 Roofline**，判断瓶颈类型（Compute/Memory/Latency Bound）
3. **如果是 Memory Bound** -> 分析 Memory.csv 各通路带宽，开启 MemoryDetail 看详情
4. **如果是 Compute Bound** -> 分析 ArithmeticUtilization.csv，看 Cube/Vector 利用率
5. **开启 Source 热点图**，定位具体瓶颈代码行
6. **核间不均衡** -> 开启 Occupancy 对比各核数据
7. **L2Cache 命中率低** -> 开启 MemoryDetail 看 Cache 热力图
8. **通算融合算子** -> 查看 trace.json 分析通信与计算的耗时掩盖
