---
name: ascend-profiler-collect-adaption
description: >
  Adapt torch_npu.profiler interface for AI frameworks and training/inference code on Ascend NPU.
  Use this skill whenever users need to integrate profiling, add performance data collection,
  or instrument PyTorch code with torch_npu.profiler on Huawei Ascend NPU platforms.
  Triggers on: "torch_npu profiler", "NPU profiling", "Ascend profiler", "profiler adaptation",
  "profile training", "profile inference", "add profiler to framework", "性能采集", "profiling 数据",
  "昇腾 profiler", and any request to add performance monitoring to Ascend NPU code.
---

# torch_npu.profiler 接口适配

将 `torch_npu.profiler` 采集接口适配到 AI 框架或训练/推理代码中，采集 NPU 算子、kernel 及内存等性能数据。按以下三步完成，每一步向用户确认后继续。

## 第一步：接口类型确认

先向用户确认本次适配目标是以下哪一种：

- `torch_npu.profiler` 接口适配
- `mstx` 接口适配
- `torch_npu.profiler` 与 `mstx` 两个接口同时适配

确认后再继续后续步骤，并按选择结果读取对应 reference：

- 适配 `torch_npu.profiler` 时，读取 [`references/api_reference.md`](references/api_reference.md)
- 适配 `mstx` 时，读取 [`references/mstx_reference.md`](references/mstx_reference.md)
- 两者同时适配时，上述两个 reference 都需要读取

若适配目标包含 `mstx`，应单独创建 `mstxProfiler`，通过 `range` 或 `mark` 方式在用户指定的类、函数或业务片段上打点，与原有 profiler 控制链路解耦。

`torch_npu.profiler` 用于采集 PyTorch 训练或在线推理场景中的 PyTorch 算子、CANN 算子、底层 NPU 算子及内存占用等性能数据；`mstx` 用于按阶段、算子段或业务流程进行标记打点，便于配合 profiler 做链路定位与分段分析。

## 第二步：定位注入点

如果本次适配目标包含 `mstx`，则必须先让用户明确提供以下信息后再继续：

- 需要注入 `mstx` 的类名
- 需要注入 `mstx` 的函数名或方法名
- 对应 `mstx` 打点名称

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

prof.start()
for _ in range(10):
    train_one_step()
    prof.step()
prof.stop()
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

### AI 任务拉起脚本/启动命令适配 profiler

在用户提供的 AI 任务拉起脚本、启动命令或对应封装入口中补充 `torch_npu.profiler` 相关配置参数，并确保这些参数能够透传到 `torch_npu.profiler` 的核心注入位置。

需要注意的是：
- 不能直接修改用户原始拉起脚本、启动命令或业务入口；应先复制一份脚本或明确新增独立配置，再在适配副本上完成 profiler 适配与验证。
- 强化学习场景中的 profiler 控制必须按阶段解耦。各阶段仅允许控制本阶段对应的采集流程，禁止跨阶段控制。
- 任务运行过程中需要特别关注 profiler 的 error 日志，必须解决所有 error 日志问题。

### profiler 交付件校验

告知用户运行完成后，按实际导出类型检查输出目录中的关键文件，text类型交付件与DB类型交付件不要求同时存在：

```text
{OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/
├── trace_view.json      # 文本类交付件：全量 trace
├── op_statistic.csv     # 文本类交付件：算子统计
├── kernel_details.csv   # 文本类交付件：kernel 详情
├── ascend_pytorch_profiler_*.db  # DB 类交付件
└── ...
```

text类型交付件校验示例：

```bash
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/trace_view.json
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/op_statistic.csv
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/kernel_details.csv
```

DB类型交付件校验示例：

```bash
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/ascend_pytorch_profiler_*.db
```

具体的 profiler 交付件完整性校验方法，可参考 [ascend-profiler-data-validation](https://gitcode.com/Ascend/msagent/blob/master/skills/ascend-profiler-data-validation/SKILL.md)。

## 规则

- **原有业务逻辑不可变更**：任何适配代码都绝对不能修改原先的业务逻辑，只能在现有代码基础上新增 `torch_npu.profiler` 或 `mstx` 接口及相关采集能力，禁止借适配之名变更原有功能行为。
- **封装优先**：必须通过统一封装层接入，禁止在业务代码中散落 `profile(...)` 调用。
- **单进程单实例**：一个进程只允许一个 profiler 实例。
- **同进程调用**：profiler 必须与被采集的训练/推理流程处于同一进程内。
- **Step 数约束**：`skip_first + (wait + warmup + active) * repeat` 必须小于总训练 step 数，否则采集不完整。
- **链路打通并进入验证输出阶段**：适配完成后，必须确认 profiler 流程已打通，需验证从外部参数传递、配置解析、封装层调用到核心采集代码执行的整条链路均生效，并进入“第三步：验证输出”阶段，对 profiler 采集结果和关键交付件进行检查。
- **MSTX 独立控制**：涉及 `mstx` 适配时，必须单独创建 `mstxProfiler`，必须通过 `range` 或 `mark` 方式打点，必须与原有 profiler 控制链路解耦。
- **范式锁定**：一旦确定为训练/推理/强化学习场景，所有接口默认值、采集控制方式均以该场景对应参考范例为准，禁止跨范式混用。
- **分步确认后继续**：第一步确认接口用法、参数项、默认值和场景范式；第二步确认注入点、封装方式、参数透传链路和脚本侧 profiler 配置已补齐且默认关闭；第三步确认是否开展验证，验证场景下需确认环境、启动方式、验证范围、输出目录和关键交付件结果；未经用户确认，不得进入下一步。

## Reference 说明

| reference 文件 | 主要内容 | 何时按需读取 |
|---|---|---|
| [`references/training_best_practices.md`](references/training_best_practices.md) | 训练场景适配范例，说明训练流程中的典型注入点、参数组织方式和调用链路。 | 当任务属于训练框架、训练脚本或单机/分布式训练适配时读取。 |
| [`references/inference_best_practices.md`](references/inference_best_practices.md) | 推理场景适配范例，说明服务化推理中的参数透传、封装层调用和控制链路。 | 当任务属于推理框架、在线服务或多进程推理适配时读取。 |
| [`references/rl_best_practices.md`](references/rl_best_practices.md) | 强化学习场景适配范例，说明分阶段解耦控制、阶段内参数配置和 Trainer / Rollout / Actor 协同方式。 | 当任务属于强化学习场景，或涉及 rollout、actor、ref 等阶段化 profiler 控制时读取。 |
| [`references/api_reference.md`](references/api_reference.md) | `torch_npu.profiler` 关键 API、常用参数、`_ExperimentalConfig` 字段及基础使用方式。 | 当需要确认接口定义、参数含义、默认值、`with` / `start-stop` 用法时读取。 |
| [`references/mstx_reference.md`](references/mstx_reference.md) | MSTX 打点接口、域过滤和最小使用示例。 | 当任务涉及 `mstx` 打点、`range_start/range_end`、域过滤或需要联动采集时读取。 |

> 按需读取原则：先根据任务场景选择对应 reference；仅在需要确认接口或参数细节时补充读取 `references/api_reference.md`；涉及 MSTX 打点时再单独读取 `references/mstx_reference.md`，避免无关 reference 混读。
