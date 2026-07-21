---
name: msprof-analyze-mfu-calculator
description: 基于 msprof-analyze 工具的昇腾 NPU 算子 MFU 分析技能。支持三种场景：(1) 用户已有 Ascend PyTorch Profiler 采集脚本，直接修改脚本补齐采集配置并用 msprof-analyze 解析 MFU；(2) 用户只关心公式表中已有的某个算子，直接根据算子维度和硬件峰值算力计算 MFU；(3) 用户需要为未注册的新算子扩展 FLOPs 公式。触发场景：算子性能分析、MFU 瓶颈定位、模型计算效率评估。
---

# 算子 MFU 分析

> **本 skill 包含三种模式：模式 A 会直接修改用户的 Profiler 脚本；模式 B 仅做计算，不修改代码；模式 C 会修改 `_flops_formulas.py` 注册新算子。**

***

## 适用范围

- **硬件平台**：昇腾 Atlas A2 / A3 系列（Ascend 910B 系列）
- **分析对象**：已注册 FLOPs 公式的 PyTorch 算子（matmul、attention、norm 等）
- **分析模式**：
  - **模式 A**：用户已有 Ascend PyTorch Profiler 采集脚本 → 直接修改脚本补齐采集配置，使用 msprof-analyze 解析
  - **模式 B**：用户只关心公式表中已有的某个算子 → 根据维度参数直接计算 MFU
  - **模式 C**：用户需要为未注册的新算子扩展 FLOPs 公式 → 查 op-plugin、注册到 `_flops_formulas.py`、采集验证

***

## 前置判断：选择分析模式

在看到用户的具体需求后，首先判断用户属于哪种场景：

```
用户需求？
├─ 用户已有包含 Ascend PyTorch Profiler 采集的脚本
│  → 模式 A：采集并解析 MFU
│
├─ 用户询问某个具体算子的 MFU，且该算子**已在公式表中**
│  → 模式 B：直接计算该算子的 MFU
│
├─ 用户明确要扩展新算子、注册 FLOPs 公式
│  → 模式 C：扩展新算子（注册 FLOPs 公式 + 采集验证）
│
└─ 用户只问了算子名和维度，但该算子**不在公式表中**（无法判断意图）
   → 询问用户：是要快速手动估算 MFU（模式 B），还是注册 FLOPs 公式并采集验证（模式 C）？
```

> **关键**：不要混淆三种模式。模式 A 面向已有 Profiler 脚本的用户，直接修改其脚本补齐 `with_flops`、`mstx` 等采集配置，再通过 msprof-analyze 解析出 MFU。模式 B 面向公式表中已有、只需快速估算 MFU 的算子，无需 Profiling 数据，直接根据维度参数手动计算。**模式 C 面向需要注册新算子 FLOPs 公式的场景**，需要查 op-plugin、注册到 `_flops_formulas.py`、采集验证。如果根据用户提问无法确定是模式 B 还是模式 C，**直接询问用户**，不要自行假设。
>
> **重要：无论选择哪种模式，都先按以下步骤与用户确认**：
> 1. **先简要列出三种模式**：让用户知道有哪些选项。例如"本 skill 有三种分析模式：模式 A 采集 profiling 数据后解析 MFU，适合已有 Profiler 脚本的场景；模式 B 手动估算 MFU，适合公式表中已有的算子；模式 C 注册新算子 FLOPs 公式并采集验证。"
> 2. **再说明你的判断**：根据用户需求，你认为适合走哪个模式及原因。
> 3. **最后请用户确认**：得到确认后再按对应模式执行。

***

## 模式 A：采集并解析 MFU

> 当用户已有包含 Ascend PyTorch Profiler 采集的脚本时，按此流程：前置检查 → 修改脚本补齐配置 → 运行程序采集数据 → 用 msprof-analyze 解析 MFU。

### 前置检查：确认脚本已集成 Profiler

在继续之前，先确认用户的脚本中**是否已包含 Ascend PyTorch Profiler**：

- **已集成 Profiler**：继续第一步，修改脚本补齐采集配置。
- **未集成 Profiler**：告知用户这是该模式的前置条件，需要先在脚本中添加 `torch_npu.profiler.profile()` 集成 Profiler 采集，修改完成后回来继续下面的步骤。

### 第一步：修改脚本，补齐采集配置

在用户已有的 Profiler 脚本中，**必须确保以下关键配置**，其余参数保留用户脚本原有设置即可。

修改完成后，**必须向用户列出每一项改动**（新增了什么、改了什么），格式如：

```text
脚本改动说明：
  - 在 torch_npu.profiler.profile() 中新增：with_flops=True
  - 在 _ExperimentalConfig 中新增：mstx=True
  - 在 _ExperimentalConfig 中新增/修改：export_type 包含 Db
  - 在 _ExperimentalConfig 中新增/修改：profiler_level 设为 Level1
```

| 必须确保 | 配置项                         | 说明                                                                         |
| ---- | --------------------------- | -------------------------------------------------------------------------- |
| ✅    | `with_flops=True`           | 开启采集侧 FLOPs 计算。加到 `torch_npu.profiler.profile()` 中。                        |
| ✅    | `mstx=True`                 | 开启 msTX 事件采集。加到 `_ExperimentalConfig` 中。                                   |
| ✅    | `export_type` 包含 `Db`       | 解析侧需要读取 DB 中的 `MSTX_EVENTS`、`PYTORCH_API`、`COMPUTE_TASK_INFO` 和 `TASK` 等表。 |
| ✅    | `profiler_level` ≥ `Level1` | 确保采集 MFU 计算所需的 kernel 信息。                                                  |

> \[!NOTE]
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

### 第二步：运行程序，采集 Profiling 数据

脚本修改完成后，直接运行用户程序。程序执行过程中，`torch_npu.profiler.profile` 会自动采集 Profiling 数据，并通过 `on_trace_ready` 输出到指定目录。

> **关键**：`on_trace_ready` 中配置的输出目录（如 `./result`）就是 Profiling 数据目录，后续将作为 msprof-analyze 的 `-d` 参数输入。
>
> **注意**：如果输出目录中已有之前采集的数据，msprof-analyze 结果可能不准确或直接报错。运行前**先询问用户是否需要清空该目录**，用户同意则删除目录下所有文件再运行。

**注意**：运行过程中如果出现明显的 ERROR 日志或直接抛出异常，**立即告知用户并停止当前流程**，不要继续往下执行。等用户排查修复后再重新运行。

#### （可选）添加模型层级 msTX 打点

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

### 第三步：运行 msprof-analyze 解析 MFU

程序运行完毕后，使用 msprof-analyze 命令行工具解析 Profiling 数据。

#### 3.1 命令格式

```bash
msprof-analyze --agent -m operator_mfu -d <profiling_path> [-o <output_path>] [--export_type <export_type>]
```

**特别注意**：`<profiling_path>` **必须**是第二步 `on_trace_ready` 输出的 Profiling 数据目录，不能填其他目录（如用户的工作目录、模型代码目录等）。如果填错目录，msprof-analyze 会报错或输出错误结果。

#### 3.2 参数说明

| 参数             | 可选/必选 | 说明                                 |
| -------------- | ----- | ---------------------------------- |
| -m             | 必选    | 设置为 `operator_mfu`，启动算子 MFU 分析。    |
| -d             | 必选    | 第二步 `on_trace_ready` 输出的 Profiling 数据目录路径，**不是其他任意目录**。 |
| -o             | 可选    | 分析结果输出路径，默认输出在 `-d` 参数指定的目录下。      |
| --export\_type | 可选    | 输出文件类型，可选 `db` 或 `text`，默认为 `db` 。 |

#### 3.3 使用示例

```bash
# -d 必须填第二步 on_trace_ready 输出的 profiling 目录，不能填其他目录
msprof-analyze --agent -m operator_mfu -d ./result --export_type text
```

### 第四步：读取并解读输出

#### 4.1 输出文件

msprof-analyze 会在 `-o` 参数指定路径下生成 `cluster_analysis_output` 文件夹：

| export\_type | 输出文件                                                                                     |
| ------------ | ---------------------------------------------------------------------------------------- |
| `text`       | `OperatorMfu/operator_mfu_kernel_{rank_id}.xlsx`：kernel 级 MFU 明细                         |
| `text`       | `OperatorMfu/operator_mfu_module_{rank_id}.xlsx`：module 级 MFU 统计（需 `Module` domain msTX） |
| `db`         | `cluster_analysis.db`，含 `OperatorMFU` 和 `ModuleMFU` 表                                    |

#### 4.2 OperatorMFU 字段说明

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

#### 4.3 ModuleMFU 字段说明

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

### 第五步：MFU 结果分析

#### 5.1 计算逻辑回顾

```text
actual_tflops = FLOPs / (kernelDuration(ns) * 1e-9) / 1e12
mfu = FLOPs / (kernelDuration(ns) * 1e-9) / chipPeakFLOPS
```

其中 `chipPeakFLOPS` 为当前芯片、当前数据类型对应的理论峰值。解析侧使用同一 FLOPs 记录时间范围内首个 kernel 的输入数据类型匹配峰值；如果无法解析输入类型，默认按 FP16 处理。

#### 5.2 MFU 解读

| MFU 范围  | 评估                                             |
| ------- | ---------------------------------------------- |
| < 20%   | 算子远未吃满算力，可能受内存带宽、launch overhead、shape 不规则等影响。 |
| 30%–60% | 中等偏上水平，许多通用工作负载大致在此区间。                         |
| > 70%   | 算子形状、并行度和实现都比较接近设备上限。                          |

#### 5.3 回答格式

当用户要求解读 MFU 结果时：

1. 说明分析基于 msprof-analyze 的 `operator_mfu` 模块。
2. 列出 MFU 最低 / 最高的 Top-N 算子及其关键信息（op\_name、kernel\_duration、actual\_tflops、mfu）。
3. 给出整体评估：是否存在明显的 MFU 瓶颈算子，以及优化方向。
4. 如果信息不全，明确列出还缺哪些信息。

***

## 模式 A 完成标志

- [ ] 确认用户已有 Ascend PyTorch Profiler 采集脚本
- [ ] 已直接修改脚本补齐采集配置，并已向用户列出每一项改动
- [ ] 已运行程序，`on_trace_ready` 输出目录已生成 Profiling 数据
- [ ] 已运行 `msprof-analyze --agent -m operator_mfu -d <on_trace_ready输出目录>` 命令（`-d` 参数对应第二步 profiling 采集产出的目录，**不是其他路径**）
- [ ] 已读取并解读输出结果（kernel 级 / module 级 MFU）
- [ ] 已给出 MFU 瓶颈分析和优化建议

***

## 模式 B：单算子 FLOPs / MFU 计算

> 当用户只关心某个具体算子、不涉及 Profiling 数据时，按此模式处理。

### 前置判断

根据用户提供的信息，走不同分支：

| 用户提供的信息 | 处理方式 |
| -------------- | -------- |
| **没有给出耗时** | 只计算 FLOPs，给出公式和计算结果，不计算 MFU |
| **给出了耗时 + 维度** | 计算 FLOPs → 计算 Achieved TFLOPs/s → 计算 MFU |

### 基本概念

- **MFU 定义**
  ```
  MFU = 实际计算产生的 FLOPs / 同时间内硬件理论可执行的 FLOPs
     = Achieved FLOPs / Peak FLOPs
  ```
- **单位约定**
  - FLOPs：浮点运算次数
  - TFLOPs/s：每秒万亿次浮点运算
  - 实际 FLOPs / 执行时间 = Achieved FLOPs/s
  - Achieved TFLOPs/s = Achieved FLOPs/s / 1e12

### 算子查找与 FLOPs 获取

根据用户给出的算子名，判断该算子属于以下哪种情况，按优先级依次处理：

1. **对应公式表**：如果算子名能明确对应到下方"算子 FLOPs 公式表"中的某个条目（例如用户说"matmul"即对应 `torch.matmul`），直接使用表中对应的 FLOPs 计算公式。

2. **属于 GEMM 或 Attention 大类**：如果算子名不在公式表中，但明显属于矩阵乘（GEMM）或 Attention 类别（如 `aclnnMatmul_MatMulV3Common_MatMulV3` 显然是 GEMM，`aclnnFlashAttentionScore` 显然是 Attention），按下方公式推导。

3. **无法归类**：前两步都无法确定时，去 `https://gitcode.com/Ascend/op-plugin` 检索算子源码，从实现中提取 FLOPs 计算方式。如果 op-plugin 也无法检索到有效代码，直接告知用户当前无法确定该算子的 FLOPs 公式。

#### Matmul / GEMM FLOPs 计算

当用户提到矩阵乘/线性层/attention 中的 matmul 时，按如下规则估算 FLOPs：

- **标准矩阵乘 (GEMM)**：形状 `(M, K)` 与 `(K, N)`
  ```
  FLOPs = 2 x M x N x K
  ```
  这里的 2 来自「一次乘法 + 一次加法」。

- **带 batch 维度的 matmul**：形状 `(B, M, K)` 与 `(B, K, N)`
  ```
  FLOPs = 2 x B x M x N x K
  ```

- **常见情形举例**（可直接类比）：
  - 线性层：输入 `(B, L, D_in)`，权重 `(D_in, D_out)` → `M = B x L, K = D_in, N = D_out`
  - Attention QK^T：`Q=(B, H, L_q, D_h), K=(B, H, L_k, D_h)` → `B' = B x H, M = L_q, N = L_k, K = D_h`

#### FlashAttention FLOPs 计算

当用户提到 FlashAttention 算子时，根据输入布局（layout）和稀疏模式（sparse_mode）计算 FLOPs。Attention FLOPs 只统计 `Q @ K^T` 和 `P @ V` 两次矩阵乘，不统计 softmax、mask、dropout 等后处理。

**输入布局**：统一转换为 `(B, N, S, D)` 格式：
- **BNSD**：`(B, N, S, D)` → 直接使用
- **BSND**：`(B, S, N, D)` → 转换为 `(B, N, S, D)`
- **BSH**：`(B, S, D)` → 转换为 `(B, 1, S, D)`（单头）
- **SBH**：`(S, B, D)` → 转换为 `(B, 1, S, D)`（单头）
- **TND**：`(T, N, D)` → varlen 场景，需要实际序列长度信息

**TND Layout**（需要 `actual_seq_qlen` 和 `actual_seq_kvlen`）：
1. 解析序列长度：`q_lens[i] = actual_seq_qlen[i] - actual_seq_qlen[i-1]`（去末尾 0）
2. 序列工作量：`acl_seq_workload = sum_i (q_lens[i] x kv_lens[i])`
3. FLOPs：`2 x N x (D_q + D_k) x acl_seq_workload`

**Common Layout**（BNSD/BSND/BSH/SBH，需要 `sparse_mode`）：
- 基础：`full_attention = 2 x q_b x q_n x q_s x k_s x (q_d + k_d)`
- sparse_mode == 0：`FLOPs = full_attention`
- sparse_mode == 2/3，q_s == k_s（causal）：`FLOPs = full_attention x 0.5`
- sparse_mode == 2，q_s > k_s：`FLOPs = full_attention x (q_s x k_s - k_s^2 / 2) / (k_s^2)`
- sparse_mode == 2，q_s < k_s：`FLOPs = full_attention x (q_s^2 / 2) / (q_s x k_s)`
- sparse_mode == 3，q_d > k_d：`FLOPs = full_attention x (k_s^2 / 2) / (q_s x k_s)`
- sparse_mode == 3，q_d < k_d：`FLOPs = full_attention x (q_s x k_s - q_s^2 / 2) / (q_s x k_s)`

### 计算 MFU 的标准步骤

当用户希望你计算某个算子的 MFU 时，严格按照以下步骤：

1. **确认信息是否充分**
   向用户要齐以下信息（如果缺失就明确提出）：
   - 算子类型（例如 matmul / GEMM / FlashAttention 等）。
   - 参与运算的张量维度（包含 batch / head / sequence 等关键维度）。
   - 单次算子执行的耗时（例如毫秒 ms）。
   - 硬件单卡的理论峰值算力（例如 312 TFLOPs/s，注明是 FP16/BF16 还是 FP8 等）。

2. **计算算子 FLOPs**
   - 根据算子类型和维度，用上面的公式算出 **单次调用的 FLOPs**。
   - 如果用户给了「每迭代包含多少次该算子」或「多个相同算子」，先计算单次，然后乘以调用次数。

3. **计算 Achieved FLOPs/s**
   - 先换算执行时间到秒，例如：`t_s = time_ms / 1000`。
   - Achieved FLOPs/s = FLOPs / t_s。
   - 再换算到 TFLOPs/s：Achieved TFLOPs/s = Achieved FLOPs/s / 1e12。

4. **计算 MFU**
   - MFU = Achieved TFLOPs/s / Peak TFLOPs/s。
   - 最终给出百分比形式，例如 0.42 → 42%。

5. **解释结果**
   - 简要说明这个 MFU 代表的含义，例如：
     - 低于 20%：通常算子远未吃满算力，可能受内存带宽、launch overhead、shape 不规则等影响。
     - 30%–60%：中等偏上水平，许多通用工作负载大致在这个区间。
     - 高于 70%：算子形状、并行度和实现都比较接近设备上限。

### 常见芯片理论峰值算力

| 芯片型号 | 精度 | 峰值算力 |
| -------- | ---- | -------- |
| Ascend 910B1 | FP16/BF16 | ≈ 378.88 TFLOPs/s |
| Ascend 910B2 | FP16/BF16 | ≈ 353.89 TFLOPs/s |
| Ascend 910B3 | FP16/BF16 | ≈ 294.91 TFLOPs/s |
| Ascend 910B4 | FP16/BF16 | ≈ 270 TFLOPs/s |

如果用户没有给出确切的峰值算力，先询问具体型号和精度模式，或使用上表典型近似值并明确声明。

### 算子 FLOPs 公式表

统一口径：
- 矩阵乘按 multiply-add 计为两次操作，即 `2 * M * K * N`。
- 融合算子默认只统计核心矩阵乘或 Attention 主体。
- 通信、数据重排、transpose、bias、scale、mask、Softmax、dropout、量化/反量化和激活等融合后处理不额外计入 FLOPs。

| 算子 | FLOPs 计算逻辑 |
| ---- | -------------- |
| `torch.mm` | `2 * M * K * N`。 |
| `torch.bmm` | `2 * B * M * K * N`。 |
| `torch.matmul` | 根据向量、矩阵和 broadcast batch 维度解析后计算；通用矩阵场景为 `2 * prod(batch_shape) * M * K * N`。 |
| `torch.nn.functional.linear` | `2 * prod(input.shape[:-1]) * out_features * in_features`。 |
| `torch.addmm` | `2 * M * K * N`，只统计 `mat1 @ mat2`。 |
| `torch_npu.npu_all_gather_base_mm` | `2 * (m_local * world_size) * K * N`，只统计 AllGather 后的 GEMM。 |
| `torch_npu.npu_transpose_batchmatmul` | 先按 `perm_x1/perm_x2` 解析参与 GEMM 的 shape，再按矩阵乘计算；三维 Batch GEMM 场景为 `2 * B * M * K * N`。 |
| `torch_npu.npu_grouped_matmul` | 如果 `x` 和 `weight` 分组一一对应，计算 `sum_i(2 * M_i * K_i * N_i)`；如果一个 `x` 对应多个 `weight`，按 `group_list` 拆分 token 后累加各组 GEMM。 |
| `torch_npu.npu_quant_matmul_gelu` | `2 * total_m * K * N`，只统计量化矩阵乘主体。 |
| `torch_npu.npu_grouped_matmul_swiglu_quant_v2` | `2 * M * K * N`，只统计 Grouped GEMM 主体。 |
| `torch_npu.npu_alltoallv_gmm` | 路由专家 GMM 为 `2 * T_route * H1 * N1`；如果传入共享专家 `mm_x/mm_weight`，额外加 `2 * BS * H2 * N2`。 |
| `torch_npu.npu_gmm_alltoallv` | 路由专家 GMM 为 `2 * T_route * H1 * N1`；如果传入共享专家 `mm_x/mm_weight`，额外加 `2 * BS * H2 * N2`。 |
| `torch_npu.npu_fusion_attention` | 只统计 `Q @ K^T` 和 `P @ V`：`2 * score_elems * q_dim + 2 * score_elems * value_dim`。普通 layout 按 `input_layout` 解析 batch、head、seq 和 head_dim；`TND` layout 使用 `actual_seq_qlen/actual_seq_kvlen` 计算有效序列长度。 |
| `torch_npu.npu_fused_infer_attention_score` | 与 `npu_fusion_attention` 同一口径，支持 `num_heads` 和 `num_key_value_heads`。 |
| `torch_npu.npu_block_sparse_attention` | 只统计有效 block pair 中的 `Q @ K^T` 和 `P @ V`：`2 * score_elems * q_dim + 2 * score_elems * value_dim`，其中 `score_elems` 按 `block_sparse_mask` 中有效块的 `q_tokens * kv_tokens` 累加。 |

Attention 中 `score_elems` 表示实际参与 QK/PV 计算的 attention score 元素数量，已包含 batch 和 head。稠密场景为 `batch * head * q_seq * kv_seq`；因果或稀疏场景会按 `sparse_mode` 或 block mask 减少有效 score 元素数。

### 回答格式要求

按如下结构作答：

1. 开头说明：（本回答基于 msprof-analyze-mfu-calculator Skill 的 MFU 计算规范）
2. **先复述输入信息**（算子类型、张量维度、时间、峰值算力）
3. **列出关键公式**（FLOPs、Achieved TFLOPs/s、MFU），代入具体数字展示中间计算过程
4. **给出最终 MFU 数值**（保留 2–3 位有效数字，百分比形式）
5. **简单分析**产生这个 MFU 的可能原因或优化方向

如果信息不全，不要瞎猜，而是明确列出还缺哪些数字，并给出如何从 profiler / 日志中拿到这些信息的建议。

***

## 模式 B 完成标志

**无耗时场景（仅 FLOPs）**：

- [ ] 已确认算子类型和维度信息
- [ ] 已在公式表中查找（找不到则去 op-plugin 检索）
- [ ] 已给出 FLOPs 计算公式和最终数值

**有耗时场景（MFU）**：

- [ ] 已确认算子类型、张量维度、执行耗时、硬件峰值算力
- [ ] 已计算 FLOPs、Achieved TFLOPs/s、MFU
- [ ] 已给出结果分析和优化建议

***

## 模式 C：扩展新的算子（注册 FLOPs 公式）

> 当用户明确要计算某个**未在算子 FLOPs 公式表中覆盖**的算子 MFU，或需要为新算子注册 FLOPs 公式以便 msprof-analyze 能识别时，按此流程处理。

按以下流程处理：先走模式 B 的算子查找流程确定 FLOPs 公式（查公式表 → GEMM/Attention 公式推导 → 无法归类时去 op-plugin 检索） → 注册到 `_flops_formulas.py` → 采集 Profiling 数据 → 确认打点落盘 → 用 msprof-analyze 解析验证。

如果该算子**已**在 `_flops_formulas.py` 中注册（即有现成的 FLOPs 打点），则跳过注册步骤，直接进入采集 Profiling 数据 → 确认打点落盘 → 用 msprof-analyze 解析验证即可。

> 文件路径：当前 Python 环境的 `<torch_npu_module_path>/profiler/_flops_formulas.py`
>
> 可通过 `python -c "import torch_npu; print(torch_npu.__file__)"` 查看具体路径。`msprof-analyze` 解析侧不需要改动。

### 第一步：确认目标 API

在 `_flops_formulas.py` 中注册，格式为：

```python
@register_npu_flop(target="模块路径:属性名", is_default=True)
```

`target` 参数指向对应的 Python 对象，会被替换为带 FLOPs 计算的 wrapper。例如：

- `torch:mm`
- `torch.nn.functional:linear`
- `torch_npu:npu_fusion_attention`

### 第二步：写公式函数

新增公式函数，入参签名尽量贴近真实 API：

```python
@register_npu_flop(target="torch_npu:my_new_op", is_default=True)
def my_new_op_flops(x, weight, *, transpose=False, group_list=None, **kwargs):
    m, k = x.shape[-2], x.shape[-1]
    n = weight.shape[-1]
    return 2 * m * k * n
```

注意事项：
- 公式函数只做 FLOPs 计算，不要有副作用
- 用 `**kwargs` 兜底可选参数，避免版本差异导致 wrapper 失败
- 遇到不合法 shape 可以直接抛异常，hook 层会捕获并跳过该次打点
- 写公式前先确认口径：统计主计算还是包含 bias/activation/quant 等融合部分？稀疏场景下算理论满量还是有效计算量？变长场景下真实工作量如何恢复？

### 第三步：验证落盘

代码添加完成后，**询问用户是否要按以下步骤验证**。如果用户同意，按流程操作：

> **注意**：修改完成后，**必须先展示修改的文件路径和修改内容**，让用户确认是否正确。格式如：
>
> ```text
> 修改文件：<torch_npu_module_path>/profiler/_flops_formulas.py
>
> 修改点：
>   - 新增 @register_npu_flop(target="torch_npu:xxx")
>   - 新增 def xxx_flops(...) 公式函数
> ```

#### 3.1 走模式 A 流程采集 Profiling 数据

按模式 A 流程采集前，先确认用户**是否有调用该算子的程序**：

- **已有程序**：直接按上方"模式 A"的步骤，修改脚本补齐采集配置，运行采集。
- **没有程序**：询问用户是否需要帮你写一个调用该算子的测试脚本，用于验证落盘。如果用户需要，按照上方模式 A 第一步中的参考示例（Profiler 配置模板 + 算子调用），写一个 Python 测试脚本。脚本写好之后，**询问用户是否现在立即执行**；如果用户同意，再按模式 A 流程运行采集。

#### 3.2 确认打点落盘

采集完成后，在 `on_trace_ready` 输出目录中找到 `ascend_pytorch_profiler.db` 或 `ascend_pytorch_profiler_*.db` 文件，运行以下 SQL 查询确认新算子的 FLOPs 是否正确打点：

```sql
SELECT
    me.ROWID,
    si_domain.value AS domain,
    si_msg.value AS message
FROM MSTX_EVENTS me
LEFT JOIN STRING_IDS si_domain ON me.domainId = si_domain.id
LEFT JOIN STRING_IDS si_msg ON me.message = si_msg.id
WHERE si_domain.value = 'mfu_flops'
ORDER BY me.ROWID;
```

期望结果：
- `domain` 列为 `mfu_flops`
- `message` 格式为 `<正整数FLOPs>-<op_name>`，例如 `"137438953472-torch::mm"`
- 新算子的记录出现在结果中

#### 3.3 确认结果正确

确认打点落盘后，运行 msprof-analyze 解析：

```bash
# -d 必须填 on_trace_ready 输出的 profiling 目录
msprof-analyze --agent -m operator_mfu -d <on_trace_ready输出目录>
```

核对输出结果中该算子的 `flops` 字段，是否与用公式手动计算出的 FLOPs 值一致（可选取简单场景，如固定维度的 `torch.mm`，代入公式算出 FLOPs 再与 `flops` 字段比对）。

## 模式 C 完成标志

- [ ] 已确认该算子未在 `_flops_formulas.py` 中注册，需要扩展
- [ ] 已确定 FLOPs 公式（通过 skill 内置公式计算或 op-plugin 检索）并注册到 `_flops_formulas.py`
- [ ] 已展示修改的文件路径和修改内容
- [ ] 已运行程序采集 Profiling 数据
- [ ] 已通过 SQL 查询确认新算子的 FLOPs 打点落盘
- [ ] 已运行 msprof-analyze 并比对 `flops` 字段与手动计算结果一致

