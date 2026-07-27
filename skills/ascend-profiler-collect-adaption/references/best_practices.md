# torch_npu.profiler 适配最佳实践

核心思路：通过统一封装层将 `torch_npu.profiler.profile(...)` 接入框架，在训练/推理关键路径上用 `start()/step()/stop()` 控制采集。

---

## 模式一：ProfilerManager（训练框架 — MindSpeed-LLM 风格）

适合大多数训练框架，在 Trainer 层控制生命周期。

```python
class ProfilerManager:
    def __init__(self, config):
        self.profiler = None
        if not need_profile(config):
            return

        level = map_level(config.profile_level)
        activities = [ProfilerActivity.NPU]
        if config.profile_with_cpu:
            activities.append(ProfilerActivity.CPU)

        self.profiler = torch_npu.profiler.profile(
            activities=activities,
            schedule=torch_npu.profiler.schedule(wait=0, warmup=0, active=1, repeat=1, skip_first=1),
            on_trace_ready=torch_npu.profiler.tensorboard_trace_handler(config.profile_save_path),
            record_shapes=config.profile_record_shapes,
            profile_memory=config.profile_with_memory,
            with_stack=config.profile_with_stack,
            experimental_config=_ExperimentalConfig(
                profiler_level=level,
                export_type=config.profile_export_type,
                data_simplification=config.profile_data_simplification,
            ),
        )

    def start(self):
        if self.profiler: self.profiler.start()
    def step(self):
        if self.profiler: self.profiler.step()
    def stop(self):
        if self.profiler: self.profiler.stop()


class Trainer:
    def __init__(self, args):
        self.profiler_manager = ProfilerManager(args)

    def train(self):
        self.profiler_manager.start()
        for step in training_loop:
            run_one_step()  # forward/backward/optimizer
            self.profiler_manager.step()
        self.profiler_manager.stop()
```

---

## 模式二：DistProfiler 多工具分发（verl 风格）

适合需要支持多种 profiler 工具的统一调度场景。

```python
def get_npu_profiler(contents, profile_level, profile_save_path, analysis, role=None):
    return torch_npu.profiler.profile(
        with_modules=..., with_stack=..., record_shapes=..., profile_memory=...,
        activities=[ProfilerActivity.CPU, ProfilerActivity.NPU],
        on_trace_ready=torch_npu.profiler.tensorboard_trace_handler(profile_save_path),
        experimental_config=torch_npu.profiler._ExperimentalConfig(...),
    )


class NPUProfiler(DistProfiler):
    _define_count = 0

    def __init__(self, rank, config, tool_config, **kwargs):
        self.discrete = tool_config.discrete
        self.profile_npu = None

    def start(self, **kwargs):
        if not self.discrete and NPUProfiler._define_count == 0:
            self.profile_npu = get_npu_profiler(...)
            self.profile_npu.start()
            NPUProfiler._define_count += 1

    def stop(self):
        if not self.discrete and NPUProfiler._define_count == 1:
            self.profile_npu.step()
            self.profile_npu.stop()


class DistProfiler:
    def __init__(self, rank, config=None, tool_config=None):
        if config.tool == "npu":
            self._impl = NPUProfiler(rank=rank, config=config, tool_config=tool_config)

    def start(self, **kwargs):
        if self.check_enable(): return self._impl.start(**kwargs)
    def step(self):
        if self.check_enable(): return self._impl.step()
    def stop(self):
        if self.check_enable(): return self._impl.stop()
```

训练阶段按 step 条件触发：
```python
class RayPPOTrainer:
    def train_step(self):
        if self.global_steps in global_profiler.steps:
            self.llm_server_manager.start_profile()
        combined_gen_output = self.async_rollout_manager.generate_sequences(...)
        if self.global_steps in global_profiler.steps:
            self.llm_server_manager.stop_profile()
```

---

## 模式三：推理 Worker 封装（vLLM-Ascend 风格）

适合推理引擎，在 Worker 层封装，通过 HTTP API 触发。

```python
class TorchNPUProfilerWrapper(WorkerProfiler):
    def __init__(self, profiler_config, trace_name):
        self.profiler = torch_npu.profiler.profile(
            activities=[ProfilerActivity.CPU, ProfilerActivity.NPU],
            profile_memory=profiler_config.torch_profiler_with_memory,
            with_modules=profiler_config.torch_profiler_with_stack,
            experimental_config=_ExperimentalConfig(
                export_type=ExportType.Text, profiler_level=ProfilerLevel.Level1,
                aic_metrics=AiCMetrics.PipeUtilization, data_simplification=True,
            ),
            on_trace_ready=tensorboard_trace_handler(
                profiler_config.torch_profiler_dir, worker_name=trace_name,
            ),
        )

    def _start(self): self.profiler.start()
    def _stop(self): self.profiler.stop()
    def _profiler_step(self): return True


class Worker:
    def __init__(self, ...):
        self.profiler = None

    def profile(self, is_start=True, profile_prefix=None):
        if is_start:
            self.profiler = TorchNPUProfilerWrapper(self.profiler_config, build_trace_name(...))
            self.profiler.start()
        else:
            if self.profiler: self.profiler.stop()

    def execute_model(self, scheduler_output, ...):
        if self.profiler: self.profiler.step()
        return self.model_runner.execute_model(scheduler_output, ...)
```

调用链：`POST /start_profile` → `engine.start_profile()` → `executor.profile(True)` → `worker.profile(True)`

---

## 单卡训练简单示例

```python
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

---

## 适配参考项目

| 项目 | 场景 |
|---|---|
| [MindSpeed-MM](https://gitcode.com/Ascend/MindSpeed-MM) | 多模态训练 |
| [MindSpeed-LLM](https://gitcode.com/Ascend/MindSpeed-LLM) | 大语言模型训练 |
| [verl](https://github.com/verl-project/verl) | 强化学习训练 |
| [vLLM-Ascend](https://github.com/vllm-project/vllm-ascend) | LLM 推理 |
| [SGLang](https://github.com/sgl-project/sglang) | LLM 推理 |
