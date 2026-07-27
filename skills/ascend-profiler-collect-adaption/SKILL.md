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

## 第一步：`torch_npu.profiler` 先验知识

`torch_npu.profiler` 是昇腾 NPU 平台上的性能分析工具，用于采集 PyTorch 训练或在线推理场景中的 PyTorch 算子、CANN 算子、底层 NPU 算子及内存占用等性能数据。

> 参数说明、使用方式（`with` 语句 / `start/stop`）详见 [`references/api_reference.md`](references/api_reference.md)，关键约束见下方"规则"。


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

## 第三步：验证输出（可选阶段）

先向用户确认是否需要验证 `torch_npu.profiler` 接口适配后的实际采集效果。

- 如果用户需要验证，则让用户提供训练环境、训练脚本以及训练方式说明，并在此基础上开展验证。
- 如果用户不需要验证，则直接结束本阶段流程。

### 训练脚本适配 profiler

在用户提供的训练脚本基础上补充 `torch_npu.profiler` 相关配置参数，并确保这些参数能够透传到 `torch_npu.profiler` 的核心注入位置。

需要注意的是：
- 不能直接修改用户原始训练脚本，应先复制一份脚本，再在复制后的脚本上完成 profiler 适配与验证。
- 强化学习场景中的 profiler 控制必须按阶段解耦。各阶段仅允许控制本阶段对应的采集流程，禁止跨阶段控制。
- 训练过程中需要特别关注profiler的warning和error日志，需要解决所有的error日志问题。

### profiler 交付件校验

告知用户运行完成后，检查输出目录中的关键文件：

```
{OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/
├── trace_view.json      # 全量 trace
├── op_statistic.csv     # 算子统计
├── kernel_details.csv   # kernel 详情
├── ascend_pytorch_profiler_*.db  # DB 格式交付件
└── ...
```

```bash
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/trace_view.json
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/op_statistic.csv
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/kernel_details.csv
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_*.db
```

具体的 profiler 交付件完整性校验方法，可参考 [ascend-profiler-data-validation](https://gitcode.com/Ascend/msagent/blob/master/skills/ascend-profiler-data-validation/SKILL.md)。

## 规则

- **原有业务逻辑不可变更**：任何适配代码都绝对不能修改原先的业务逻辑，只能在现有代码基础上新增 `torch_npu.profiler` 接口及相关采集能力，禁止借适配之名变更原有功能行为
- **封装优先**：必须通过统一封装层接入，禁止在业务代码中散落 `profile(...)` 调用
- **单进程单实例**：一个进程只允许一个 profiler 实例
- **同进程调用**：profiler 必须与被采集的训练/推理流程处于同一进程内
- **Step 数**：`skip_first + (wait + warmup + active) * repeat` 必须小于总训练 step 数，否则采集不完整
- **训练脚本补充 profiler 参数**：训练/推理框架适配时，需要在对应模型训练的 `sh` 脚本中补充 profiler 相关参数配置，并默认关闭，由用户按需开启
- **链路打通验证**：适配完成后，必须确认 profiler 流程已打通，需验证从外部参数传递、配置解析、封装层调用到核心采集代码执行的整条链路均生效
- **范式锁定**：一旦确定为训练/推理/强化学习场景，所有接口默认值、采集控制方式均以该场景对应参考范例为准，禁止跨范式混用
- **参数确认、验证通过**：每一步向用户确认后继续
- 训练适配范例见 [`references/training_best_practices.md`](references/training_best_practices.md)，推理适配范例见 [`references/inference_best_practices.md`](references/inference_best_practices.md)，强化学习适配范例见 [`references/rl_best_practices.md`](references/rl_best_practices.md)，API 参数详情见 [`references/api_reference.md`](references/api_reference.md)
