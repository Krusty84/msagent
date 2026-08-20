# 推理场景 — torch_npu.profiler 适配最佳实践

> 代表框架：vLLM-Ascend、SGLang

下文用于识别服务的配置/RPC/worker 边界。新框架适配应使用通用 `ProfilerController`，API 层只转发请求，
每个执行 NPU 的 worker 独立持有 controller。controller 在每次 `start()` 时创建新的底层 PTA profiler，
`stop()` 后可再次启动下一会话；不能尝试重启已经 stop 的同一个原始 `torch_npu.profiler.profile` 对象。

## 采集控制方式：运行时 HTTP API（curl 命令）

涉及文件（以 `vLLM-Ascend` 为例）：

| 文件 | 内容 |
|---|---|
| [`vllm_ascend/worker/profiler.py`](https://github.com/vllm-project/vllm-ascend/blob/main/vllm_ascend/worker/profiler.py) | `TorchNPUProfilerWrapper` 类，封装 `torch_npu.profiler.profile` 的创建与启停 |
| [`vllm_ascend/worker/worker.py`](https://github.com/vllm-project/vllm-ascend/blob/main/vllm_ascend/worker/worker.py) | `Worker.profile()` 方法，按 `is_start` 参数控制 profiler 生命周期 |
| [`vllm_ascend/entrypoints/api_server.py`](https://github.com/vllm-project/vllm-ascend/blob/main/vllm_ascend/entrypoints/api_server.py) | HTTP 路由 `/start_profile` / `/stop_profile`，转发到 engine → executor → worker |

vLLM 服务启动时通过 `--profiler-config` 命令行参数传入 Profiler 相关配置，该参数在启动阶段被解析并存储于 `vllm_config.profiler_config` 中。`vLLM-Ascend` 的 `NPUWorker` 在初始化时接收完整的 `vllm_config` 对象，从中提取 `profiler_config` 并缓存为成员变量 `self.profiler_config`。至此，Profiler 配置参数完成了从 CLI 到 Worker 实例的完整透传链路，实现了外部对采集行为的灵活控制。

```python
# vLLM服务启动时 profiler 配置示例
python3 -m vllm.entrypoints.openai.api_server ... --profiler-config '{"profiler": "torch", "torch_profiler_dir": "./vllm_profile", "torch_profiler_with_stack": false}'
```

vLLM 对外提供 `/start_profile` 和 `/stop_profile` 两个 HTTP 接口，用于接收来自 curl 命令的 Profiler 启停请求。请求首先由上游 `vLLM` 的 HTTP 路由层拦截并转发至引擎层，引擎层通过 `collective_rpc("profile", ...)` 将控制指令广播至所有 `NPUWorker` 实例。每个 Worker 在接收到 profile RPC 请求后，根据 `is_start` 参数的值执行相应操作：若 `is_start=True`，则调用 `self.profiler.start()` 启动采集；若 `is_start=False`，则调用 `self.profiler.stop()` 停止采集并落盘。该机制实现了通过外部 curl 命令远程控制 Profiler 启停的能力，无需重启服务即可灵活进行性能数据的按需采集。

```bash
# 启动采集
curl -X POST http://localhost:8000/start_profile

# 停止采集
curl -X POST http://localhost:8000/stop_profile
```

`TorchNPUProfilerWrapper` 继承自 `WorkerProfiler` 基类，负责封装 `torch_npu.profiler.profile` 的创建、启动与停止逻辑。基类 `WorkerProfiler` 已对外暴露 `start()`、`stop()`、`step()` 三个标准接口，其内部实现采用模板方法模式，分别调用派生类需实现的 `_start()`、`_stop()`、`_profiler_step()` 钩子方法。因此，`TorchNPUProfilerWrapper` 只需重写这三个私有钩子方法即可完成具体采集行为的注入，外部调用方统一通过基类的 `start()`/`stop()`/`step()` 接口与 `Profiler` 交互，无需感知底层实现差异：

```python
class TorchNPUProfilerWrapper(WorkerProfiler):
    def __init__(self, profiler_config, trace_name):
        super().__init__(profiler_config)
        self.profiler = self._create_profiler(profiler_config, trace_name)

    def _create_profiler(self, profiler_config, trace_name):
        experimental_config = _ExperimentalConfig(
            export_type=ExportType.Text,
            profiler_level=ProfilerLevel.Level1,
            aic_metrics=AiCMetrics.PipeUtilization,
            data_simplification=True,
        )
        return torch_npu.profiler.profile(
            activities=[ProfilerActivity.CPU, ProfilerActivity.NPU],
            profile_memory=profiler_config.torch_profiler_with_memory,
            with_modules=profiler_config.torch_profiler_with_stack,
            experimental_config=experimental_config,
            on_trace_ready=tensorboard_trace_handler(
                profiler_config.torch_profiler_dir, worker_name=trace_name,
            ),
        )

    def _start(self): self.profiler.start()
    def _stop(self): self.profiler.stop()
    def _profiler_step(self): return True
```

`Worker.profile()` 按 `is_start` 参数控制 profiler 生命周期，`execute_model` 中调用 `step()`：

```python
class Worker:
    def __init__(self, ...):
        self.profiler = None
        self.profiler_config = profiler_config   # 启动时注入的配置

    def profile(self, is_start=True, profile_prefix=None):
        if profiling_not_enabled(self.profiler_config):
            raise RuntimeError("profiling not enabled")

        if is_start:
            trace_name = build_trace_name(profile_prefix, self.rank)
            if self.profiler is None:
                self.profiler = TorchNPUProfilerWrapper(self.profiler_config, trace_name)
            self.profiler.start()
        else:
            if self.profiler is None: return
            self.profiler.stop()

    def execute_model(self, scheduler_output, ...):
        if self.profiler is not None:
            self.profiler.step()
        return self.model_runner.execute_model(scheduler_output, ...)
```

HTTP API 调用链：`POST /start_profile` → `engine.start_profile()` → `executor.profile(True)` → `worker.profile(True)`

适配时还必须验证：重复 start/stop 不重复调用底层生命周期；并发请求不创建两个活动实例；worker 名至少
包含 global rank 和 PID；验收器的 `--expected-workers` 或 `--expected-ranks` 能发现缺失会话。
