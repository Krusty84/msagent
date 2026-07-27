---
name: torch-npu-profiler-adaptation
description: >
  Adapt torch_npu.profiler interface for AI frameworks and training/inference code on Ascend NPU.
  Use this skill whenever users need to integrate profiling, add performance data collection,
  or instrument PyTorch code with torch_npu.profiler on Huawei Ascend NPU platforms.
  Triggers on: "torch_npu profiler", "NPU profiling", "Ascend profiler", "profiler adaptation",
  "profile training", "profile inference", "add profiler to framework", "性能采集", "profiling 数据",
  "昇腾 profiler", and any request to add performance monitoring to Ascend NPU code.
---

# torch_npu.profiler 接口适配

将 `torch_npu.profiler` 采集接口适配到 AI 框架或训练/推理代码中，采集 NPU 算子、kernel 及内存等性能数据。按以下三步完成，每步向用户确认后继续。

> API 参数详情见 [`references/api_reference.md`](references/api_reference.md)。各框架适配范例：训练 → [`references/training_best_practices.md`](references/training_best_practices.md)、推理 → [`references/inference_best_practices.md`](references/inference_best_practices.md)、强化学习 → [`references/rl_best_practices.md`](references/rl_best_practices.md)。

## 第一步：`torch_npu.profiler` 先验知识与注意事项

### 接口介绍

`torch_npu.profiler` 是昇腾 NPU 平台上的性能分析工具，可用于采集 PyTorch 训练或在线推理场景中的性能数据，主要包括：

- PyTorch 层算子信息
- CANN 层算子信息
- 底层 NPU 算子信息
- 算子内存占用信息

该工具用于全方位分析 PyTorch 训练/推理时的性能状态。

### 常用参数说明

| 参数 | 类型/说明 | 关键作用 |
|---|---|---|
| `activities` | CPU / NPU 事件采集列表，`Enum` 类型。常见取值包括 `ProfilerActivity.CPU` 和 `ProfilerActivity.NPU`。 | 决定性能数据的采集来源范围。 |
| `schedule` | `Callable`，一般由 `torch_npu.profiler.schedule()` 构造。 | 控制采集调度，包括等待、预热、采集轮数、重复次数等。 |
| `on_trace_ready` | `Callable`，采集结束时触发的结果处理器。常见配置为 `torch_npu.profiler.tensorboard_trace_handler("./result")`。 | 负责采集结果的自动解析与落盘。 |
| `profile_memory` | `bool` 类型。 | 控制是否采集内存占用信息，用于分析内存瓶颈。 |
| `experimental_config` | 扩展配置对象。常用于配置 `aic_metrics`、`mstx`、`host_sys` 等高级采集项。 | 配置更细粒度的采集行为。 |

### 使用方式

#### 方式一：`with` 语句

适合自动管理生命周期的场景。

```python
import torch
import torch_npu

experimental_config = torch_npu.profiler._ExperimentalConfig(
    export_type=[torch_npu.profiler.ExportType.Text],
    profiler_level=torch_npu.profiler.ProfilerLevel.Level0,
    mstx=False,
)

with torch_npu.profiler.profile(
    activities=[
        torch_npu.profiler.ProfilerActivity.CPU,
        torch_npu.profiler.ProfilerActivity.NPU,
    ],
    schedule=torch_npu.profiler.schedule(
        wait=0, warmup=0, active=1, repeat=1, skip_first=1
    ),
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("./result"),
    experimental_config=experimental_config,
) as prof:
    for step in range(steps):
        train_one_step()
        prof.step()
```

#### 方式二：`start/step/stop`

适合需要灵活控制采集起止位置的场景。

```python
prof = torch_npu.profiler.profile(
    activities=[...],
    schedule=torch_npu.profiler.schedule(...),
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("./result"),
)

prof.start()
for step in range(steps):
    train_one_step()
    prof.step()
prof.stop()
```

### 注意事项

- 单进程单实例：一个业务进程内只允许创建一个 `torch_npu.profiler` 采集实例。
- 同进程调用：`torch_npu.profiler` 的调用必须与被采集的训练/推理流程处于同一进程内。
- Step 数限制：建议 `skip_first + (wait + warmup + active) * repeat` 小于总训练 step 数。


## 第二步：定位注入点

### 普通单卡场景

无封装层，适合快速验证或单机调试。配置直接写在脚本中，运行即采集，无外部控制机制。

```python
import torch_npu

experimental_config = torch_npu.profiler._ExperimentalConfig(
    export_type=[torch_npu.profiler.ExportType.Text],
    profiler_level=torch_npu.profiler.ProfilerLevel.Level0,
    aic_metrics=torch_npu.profiler.AiCMetrics.AiCoreNone,
)

prof = torch_npu.profiler.profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.NPU],
    schedule=torch_npu.profiler.schedule(wait=0, warmup=0, active=1, repeat=1, skip_first=1),
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("./result"),
    experimental_config=experimental_config,
)

for _ in range(10):
    train_one_step()
    prof.step()
```

### 训练场景

- 沿业务调用链找到真正执行前向、反向和优化器更新的位置，最接近 NPU 计算的执行单元即最佳注入点。
- 完整封装范例见 [`references/training_best_practices.md`](references/training_best_practices.md)（MindSpeed-LLM：`ProfilerConfig` → `ProfilerManager` → `Trainer`）。

### 推理场景

- 沿调用链找到接近 NPU 实际执行的位置。注意：如果主控/调度进程内不执行实际 NPU 计算，则不能在该进程中注入 profiler。
- 完整封装范例见 [`references/inference_best_practices.md`](references/inference_best_practices.md)（vLLM-Ascend：`TorchNPUProfilerWrapper` → `Worker.profile()` → HTTP API）。

### 强化学习场景

- 混合控制：配置文件 + step 条件触发 + HTTP API，适合多角色（Actor / Rollout / Ref）的复杂训练拓扑。
- 完整封装范例见 [`references/rl_best_practices.md`](references/rl_best_practices.md)（verl：`DistProfiler` → `NPUProfiler` → `VLLMHttpServer` → `RayPPOTrainer`）。

## 第三步：验证输出

告知用户运行后检查输出目录中的关键文件：

```bash
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/trace_view.json
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/op_statistic.csv
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/kernel_details.csv
```

## 规则

- 封装优先、单实例约束、参数确认、验证通过
- 训练适配范例见 [`references/training_best_practices.md`](references/training_best_practices.md)，推理适配范例见 [`references/inference_best_practices.md`](references/inference_best_practices.md)，强化学习适配范例见 [`references/rl_best_practices.md`](references/rl_best_practices.md)，API 参数详情见 [`references/api_reference.md`](references/api_reference.md)
