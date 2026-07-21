# 模式 A：采集并解析 MFU

> 当用户已有包含 Ascend PyTorch Profiler 采集的脚本时，按此流程：前置检查 → 修改脚本补齐配置 → 运行程序采集数据 → 用 msprof-analyze 解析 MFU。

## 原理：算子 MFU 的采集与分析流程

整个方案分为**采集**和**分析**两段：采集时计算 FLOPs 并通过 mstx 打点落盘到 Profiling 数据，分析阶段将框架侧的打点和 Device 上运行的算子关联起来，拿到对应算子耗时，进而计算出 MFU。

### 采集阶段

由 `torch_npu.profiler` 完成。用户在打开 `with_flops=True` 及相关配置后，torch\_npu 在启动时会安装 Python 层的 FLOPs hook，将已注册公式的目标 API 包装起来。当这些算子被调用时，hook 在真正执行前根据当前入参算出 FLOPs，再通过 MSTX 接口将结果打点到 `mfu_flops` 域中，最终落盘到 profile 数据并导出为 `ascend_pytorch_profiler_{rank_id}.db` 文件，相关信息记录在 `MSTX_EVENTS` 表中。

### 分析阶段

由 `msprof-analyze` 工具承载。执行 `msprof-analyze -m operator_mfu` 后，工具会从 DB 中读取 `MSTX_EVENTS`，解析出每条记录对应的 FLOPs 和算子名称。同时，通过框架 API 到 Device Kernel 的关联关系，拿到对应的 Device kernel 的执行耗时、输入数据类型。芯片理论峰值则从 device 信息中读取 `ai_core_num` 和 `aic_frequency`，再结合数据类型估算得出。最后，将 FLOPs range 时间窗内的 kernel 与对应的 FLOPs 关联，逐个计算出 MFU。

## 前置检查：确认脚本已集成 Profiler

在继续之前，先确认用户的脚本中**是否已包含 Ascend PyTorch Profiler**：

- **已集成 Profiler**：继续第一步，修改脚本补齐采集配置。
- **未集成 Profiler**：告知用户这是该模式的前置条件，需要先在脚本中添加 `torch_npu.profiler.profile()` 集成 Profiler 采集，修改完成后回来继续下面的步骤。

## 第一步：修改脚本，补齐采集配置

在用户已有的 Profiler 脚本中，**必须确保以下关键配置已正确设置**。

**先逐项检查用户的脚本**，而不是直接建议添加。注意：这些配置可能以多种形式出现：

- **硬编码**：直接写在 `torch_npu.profiler._ExperimentalConfig(...)` 或 `profile(...)` 中
- **Python 侧参数化**：通过 `argparse`、环境变量、配置文件等传入，如 `profiler_level=args.profiler_level`、`export_type=config.export_type`
- **Bash 脚本驱动**：通过 bash 脚本拉起的训练任务，Profiler 参数在命令行传入，如 `--profile-level level1 --profile-export-type db`，Python 脚本通过 argparse 接收后配置给 Profiler

检查时逐一确认以下 4 项，已存在的直接标注"已有"，缺失的说明需要添加。对已参数化的配置（无论 Python 侧还是 bash 脚本驱动），说明只需确保传入的参数值正确即可，不需要改脚本结构。如果是 bash 脚本传参，还需提醒用户检查 launch 脚本中的对应参数值。

| 必须确保 | 配置项                         | 说明                                                                         |
| ---- | --------------------------- | -------------------------------------------------------------------------- |
| ✅    | `with_flops=True`           | 开启采集侧 FLOPs 计算。加到 `torch_npu.profiler.profile()` 中。                        |
| ✅    | `mstx=True`                 | 开启 msTX 事件采集。加到 `_ExperimentalConfig` 中。                                   |
| ✅    | `export_type` 包含 `Db`       | 解析侧需要读取 DB 中的 `MSTX_EVENTS`、`PYTORCH_API`、`COMPUTE_TASK_INFO` 和 `TASK` 等表。 |
| ✅    | `profiler_level` ≥ `Level1` | 确保采集 MFU 计算所需的 kernel 信息。                                                  |

修改完成后，**必须向用户列出每一项的检查结果**（已有 / 已修改 / 已新增），格式如：

```text
脚本检查结果：
  - with_flops: 已有（torch_npu.profiler.profile() 中已设置）
  - mstx: 已有（_ExperimentalConfig 中已设置）
  - export_type 含 Db: 已有（通过 args.export_type 传入，确保实际传参包含 Db）
  - profiler_level ≥ Level1: 缺失，已新增
```

> [!NOTE]
>
> - 如果用户脚本配置了 `mstx_domain_include`，需要确保 FLOPs 相关 msTX 事件未被过滤；如果需要 module 级 MFU 聚合，需要配置 `with_modules=True`。
> - 其余配置项（如 `activities`、`schedule`、`aic_metrics` 等）保持用户脚本原有设置，不需强制修改。

以下为参考示例，**仅用于对照，不要求用户脚本完全一致**：

```python
import torch
import torch_npu

def train_one_step():
    # 你的算子或者模型
    pass

experimental_config = torch_npu.profiler._ExperimentalConfig(
    profiler_level=torch_npu.profiler.ProfilerLevel.Level1,
    aic_metrics=torch_npu.profiler.AiCMetrics.PipeUtilization,
    mstx=True,
    data_simplification=True,
    export_type=[
        torch_npu.profiler.ExportType.Text,
        torch_npu.profiler.ExportType.Db,
    ],
)

prof = torch_npu.profiler.profile(
    activities=[
        torch_npu.profiler.ProfilerActivity.CPU,
        torch_npu.profiler.ProfilerActivity.NPU,
    ],
    schedule=torch_npu.profiler.schedule(wait=0, warmup=1, active=1, repeat=1, skip_first=1),
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("./result"),
    profile_memory=False,
    with_stack=False,
    with_flops=True,
    with_modules=True,
    experimental_config=experimental_config,
)
prof.start()    # 启动性能数据采集
for step in range(3):
    train_one_step()
    prof.step()    # 与schedule配套使用
prof.stop()    # 结束性能数据采集
```

## 第二步：运行程序，采集 Profiling 数据

脚本修改完成后，直接运行用户程序。程序执行过程中，`torch_npu.profiler.profile` 会自动采集 Profiling 数据，并通过 `on_trace_ready` 输出到指定目录。

> **关键**：`on_trace_ready` 中配置的输出目录（如 `./result`）就是 Profiling 数据目录，后续将作为 msprof-analyze 的 `-d` 参数输入。
>
> **注意**：如果输出目录中已有之前采集的数据，msprof-analyze 结果可能不准确或直接报错。运行前**先询问用户是否需要清空该目录**，用户同意则删除目录下所有文件再运行。

**注意**：运行过程中如果出现明显的 ERROR 日志或直接抛出异常，**立即告知用户并停止当前流程**，不要继续往下执行。等用户排查修复后再重新运行。

### （可选）添加模型层级 msTX 打点

如果只需要 kernel 级 MFU 明细，可以跳过此步。如果需要生成 module 级 MFU 统计，需要在模型代码中调用 `torch_npu.npu.mstx.range_start/range_end`：

```python
original_call = nn.Module.__call__

def custom_call(self, *args, **kwargs):
    module_name = self.__class__.__name__
    mstx_id = torch_npu.npu.mstx.range_start(module_name, domain="Module")
    result = original_call(self, *args, **kwargs)
    torch_npu.npu.mstx.range_end(mstx_id, domain="Module")
    return result

nn.Module.__call__ = custom_call
```

## 第三步：运行 msprof-analyze 解析 MFU

程序运行完毕后，使用 msprof-analyze 命令行工具解析 Profiling 数据。

### 3.1 命令格式

```bash
msprof-analyze --agent -m operator_mfu -d <profiling_path> [-o <output_path>] [--export_type <export_type>]
```

**特别注意**：`<profiling_path>` **必须**是第二步 `on_trace_ready` 输出的 Profiling 数据目录，不能填其他目录（如用户的工作目录、模型代码目录等）。如果填错目录，msprof-analyze 会报错或输出错误结果。

### 3.2 参数说明

| 参数             | 可选/必选 | 说明                                                      |
| -------------- | ----- | ------------------------------------------------------- |
| -m             | 必选    | 设置为 `operator_mfu`，启动算子 MFU 分析。                         |
| -d             | 必选    | 第二步 `on_trace_ready` 输出的 Profiling 数据目录路径，**不是其他任意目录**。 |
| -o             | 可选    | 分析结果输出路径，默认输出在 `-d` 参数指定的目录下。                           |
| --export\_type | 可选    | 输出文件类型，可选 `db` 或 `text`，默认为 `db` 。                      |

### 3.3 版本检查：确认 msprof-analyze 支持 operator\_mfu

执行以下命令检查当前版本是否支持 `operator_mfu` 模块：

```bash
msprof-analyze cluster --help
```

如果输出中 `-m` 的可选参数**包含** **`operator_mfu`**，则直接跳到 3.4 使用即可。

如果**不包含** **`operator_mfu`**，说明版本较老，需要源码安装最新版：

> **注意**：源码安装需要先询问用户是否同意执行，得到确认后再操作。

```bash
# 1. 安装 wheel
pip3 install wheel

# 2. 克隆源码
git clone https://gitcode.com/Ascend/msprof-analyze

# 3. 编译 whl 包
cd msprof-analyze
pip3 install -r requirements.txt && python3 setup.py bdist_wheel

# 4. 安装（--force-reinstall 覆盖旧版本）
cd dist
pip3 install --force-reinstall ./msprof_analyze-{version}-py3-none-any.whl
```

安装完成后，再次执行 `msprof-analyze cluster --help` 确认 `-m` 的可选参数中已包含 `operator_mfu`。

### 3.4 使用示例

```bash
# -d 必须填第二步 on_trace_ready 输出的 profiling 目录
msprof-analyze --agent -m operator_mfu -d ./result --export_type text
```

## 第四步：读取并解读输出

### 4.1 输出文件

msprof-analyze 会在 `-o` 参数指定路径下生成 `cluster_analysis_output` 文件夹：

| export\_type | 输出文件                                                                                     |
| ------------ | ---------------------------------------------------------------------------------------- |
| `text`       | `OperatorMfu/operator_mfu_kernel_{rank_id}.xlsx`：kernel 级 MFU 明细                         |
| `text`       | `OperatorMfu/operator_mfu_module_{rank_id}.xlsx`：module 级 MFU 统计（需 `Module` domain msTX） |
| `db`         | `cluster_analysis.db`，含 `OperatorMFU` 和 `ModuleMFU` 表                                    |

### 4.2 OperatorMFU 字段说明

| 字段名称                 | 说明                                   |
| -------------------- | ------------------------------------ |
| rank\_id             | Rank ID。                             |
| op\_name             | 框架侧算子名称。                             |
| kernel\_name         | Device 侧 kernel 名称。                  |
| kernel\_start(ns)    | kernel 开始时间，单位ns。                    |
| kernel\_end(ns)      | kernel 结束时间，单位ns。                    |
| kernel\_duration(ns) | kernel 执行时长，单位ns。                    |
| mfu                  | MFU 比值。                              |
| actual\_tflops       | 按当前 kernel 时长计算的实际 TFLOPS。           |
| chip\_peak\_tflops   | 按 kernel 输入数据类型匹配到的芯片理论峰值，单位 TFLOPS。 |
| flops                | 采集侧记录的算子 FLOPs。                      |
| flops\_op\_name      | 采集侧记录 FLOPs 时对应的算子名称。                |
| input\_shapes        | kernel 输入 shape。                     |
| output\_shapes       | kernel 输出 shape。                     |

### 4.3 ModuleMFU 字段说明

| 字段名称                        | 说明                                   |
| --------------------------- | ------------------------------------ |
| rank\_id                    | Rank ID。                             |
| parent\_module              | 上层 Module 名称。                        |
| module                      | 最底层 Module 名称。                       |
| op\_name                    | 框架侧算子名称。                             |
| kernel\_list                | 框架侧算子下发到 Device 侧执行的 kernel 序列。      |
| total\_kernel\_duration(ns) | 框架侧算子对应 Device 侧 kernel 运行总时间，单位ns。  |
| avg\_kernel\_duration(ns)   | 框架侧算子对应 Device 侧 kernel 平均运行时间，单位ns。 |
| op\_count                   | 框架侧算子在采集周期内运行的次数。                    |
| avg\_mfu                    | 按同一 kernel 位置聚合得到的平均 MFU，百分比格式。      |

## 第五步：MFU 结果分析

### 5.1 计算逻辑回顾

```text
actual_tflops = FLOPs / (kernelDuration(ns) * 1e-9) / 1e12
mfu = FLOPs / (kernelDuration(ns) * 1e-9) / chipPeakFLOPS
```

其中 `chipPeakFLOPS` 为当前芯片、当前数据类型对应的理论峰值。解析侧使用同一 FLOPs 记录时间范围内首个 kernel 的输入数据类型匹配峰值；如果无法解析输入类型，默认按 FP16 处理。

### 5.2 MFU 解读

| MFU 范围  | 评估                                             |
| ------- | ---------------------------------------------- |
| < 20%   | 算子远未吃满算力，可能受内存带宽、launch overhead、shape 不规则等影响。 |
| 30%–60% | 中等偏上水平，许多通用工作负载大致在此区间。                         |
| > 70%   | 算子形状、并行度和实现都比较接近设备上限。                          |

### 5.3 回答格式

当用户要求解读 MFU 结果时：

1. 说明分析基于 msprof-analyze 的 `operator_mfu` 模块。
2. 列出 MFU 最低 / 最高的 Top-N 算子及其关键信息（op\_name、kernel\_duration、actual\_tflops、mfu）。
3. 给出整体评估：是否存在明显的 MFU 瓶颈算子，以及优化方向。
4. 如果信息不全，明确列出还缺哪些信息。

