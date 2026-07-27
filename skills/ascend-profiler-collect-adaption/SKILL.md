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

将 `torch_npu.profiler` 采集接口适配到 AI 框架或训练/推理代码中，采集 NPU 算子、kernel 及内存等性能数据。按以下五步完成，每步向用户确认后继续。

## 第一步：分析代码结构

通读用户代码，识别框架类型并定位注入点：

- **训练场景**：追踪 forward → backward → optimizer 调用链，最接近 NPU 计算的位置即注入点
- **推理场景**：追踪请求入口到 NPU 执行的路径；若主控进程不执行 NPU 计算，不能在此注入

向用户报告：框架类型、推荐注入点、建议的适配模式（参考 `references/best_practices.md` 中四种模式）。

## 第二步：确认配置参数

向用户展示并确认以下参数（默认值适用于大多数场景）：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `activities` | `[CPU, NPU]` | 采集范围 |
| `schedule` | `wait=0,warmup=0,active=1,repeat=1,skip_first=1` | 采集调度 |
| `on_trace_ready` | `tensorboard_trace_handler("./result")` | 结果输出 |
| `profiler_level` | `Level1` | 采集级别 |
| `profile_memory` | `False` | 内存采集 |

可选：`aic_metrics`、`with_stack`、`record_shapes`、`mstx`，按需询问用户。API 详情见 `references/api_reference.md`。

## 第三步：实现封装类

**核心原则：必须通过统一封装层接入，禁止在业务代码中散落 `profile(...)` 调用。**

封装类职责：初始化 profile 对象、暴露可配参数、管理 `start()/step()/stop()` 生命周期、通过开关控制启用。

关键约束：
- **单进程单实例**：用标志位或单例模式保证
- **空操作安全**：profiler 未启用时所有方法必须是无副作用空操作

从 `references/best_practices.md` 选择匹配的设计模式：
- 模式一（ProfilerManager）— 训练框架，Trainer 层控制
- 模式二（DistProfiler）— 多工具统一调度
- 模式四（Worker 封装）— 推理引擎

## 第四步：注入调用点

**训练**：循环前 `start()` → 每 step 后 `step()` → 循环后 `stop()`

**推理**：请求前 `start()` → `execute_model` 中 `step()` → 完成后 `stop()`

将 profiler 参数纳入框架配置体系，通过开关控制。

## 第五步：验证输出

告知用户运行后检查输出目录中的关键文件：

```bash
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/trace_view.json
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/op_statistic.csv
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/kernel_details.csv
```

## 默认配置模板

```python
experimental_config = torch_npu.profiler._ExperimentalConfig(
    export_type=[torch_npu.profiler.ExportType.Text],
    profiler_level=torch_npu.profiler.ProfilerLevel.Level1,
    aic_metrics=torch_npu.profiler.AiCMetrics.AiCoreNone,
)
prof = torch_npu.profiler.profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.NPU],
    schedule=torch_npu.profiler.schedule(wait=0, warmup=0, active=1, repeat=1, skip_first=1),
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("./result"),
    experimental_config=experimental_config,
)
```

## 规则

- 封装优先、单实例约束、参数确认、验证通过
- 更多实现细节见 `references/best_practices.md`，API 参考见 `references/api_reference.md`
