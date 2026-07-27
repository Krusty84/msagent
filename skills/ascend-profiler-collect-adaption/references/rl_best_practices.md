# 强化学习场景 — torch_npu.profiler 适配最佳实践

> 代表框架：verl

## 采集控制方式：配置文件 + step 条件触发 + HTTP API（混合）

涉及文件（以 `verl` 为例）：

| 文件 | 内容 |
|---|---|
| [`verl/utils/profiler/npu_profiler.py`](https://github.com/verl-project/verl/blob/main/verl/utils/profiler/npu_profiler.py) | `get_npu_profiler()` 工厂函数 + `NPUProfiler` 类（NPU 采集实现） |
| [`verl/utils/profiler/dist_profiler.py`](https://github.com/verl-project/verl/blob/main/verl/utils/profiler/dist_profiler.py) | `DistProfiler` 统一调度层，根据 `config.tool` 分发到对应实现 |
| [`verl/workers/rollout/vllm_http_server.py`](https://github.com/verl-project/verl/blob/main/verl/workers/rollout/vllm_http_server.py) | `VLLMHttpServer` 服务层，暴露 HTTP `start_profile()`/`stop_profile()` 接口 |
| [`verl/trainer/ppo/ray_trainer.py`](https://github.com/verl-project/verl/blob/main/verl/trainer/ppo/ray_trainer.py) | `RayPPOTrainer` 训练层，按 `global_profiler.steps` 条件触发采集 |

verl 实现了三层控制：

1. **配置文件**：通过 `config.tool = "npu"` 选择 profiler 工具，`tool_config` 配置采集参数（level、contents、discrete 模式等）
2. **Step 条件触发**：通过 `global_profiler.steps` 指定在哪些 step 触发采集，`RayPPOTrainer` 在每个 step 检查是否命中
3. **HTTP API**：`VLLMHttpServer` 暴露 `start_profile()`/`stop_profile()` 接口，在命中 step 时由 Trainer 调用

```yaml
# verl profiler 配置示例
profiler:
  enable: True
  all_ranks: False
  ranks: [0]
  tool_config:
    npu:
      discrete: false            # false=连续模式（start→采集→stop），true=每阶段独立数据库
      level: "level1"            # level_none / level0 / level1 / level2
      contents: ["npu", "cpu", "memory"]  # npu / cpu / memory / shapes / module / stack
      analysis: true
```

## 实现

verl 分五层，逐级委托。

`get_npu_profiler()` 工厂函数，将 `contents` 列表映射为 `torch_npu.profiler.profile` 的参数并创建 profiler 实例（[`verl/utils/profiler/npu_profiler.py`](https://github.com/verl-project/verl/blob/main/verl/utils/profiler/npu_profiler.py)）：

```python
def get_npu_profiler(contents, profile_level, profile_save_path, analysis,
                     role=None, profile_step=None):
    experimental_config = torch_npu.profiler._ExperimentalConfig(...)
    return torch_npu.profiler.profile(
        with_modules=..., with_stack=..., record_shapes=..., profile_memory=...,
        activities=[ProfilerActivity.CPU, ProfilerActivity.NPU],
        on_trace_ready=torch_npu.profiler.tensorboard_trace_handler(profile_save_path),
        experimental_config=experimental_config,
    )
```

`NPUProfiler` 具体的 NPU 采集实现，根据 `discrete` 模式决定连续采集还是分阶段存储（[`verl/utils/profiler/npu_profiler.py`](https://github.com/verl-project/verl/blob/main/verl/utils/profiler/npu_profiler.py)）：

```python
class NPUProfiler(DistProfiler):
    def __init__(self, rank, config, tool_config, **kwargs):
        self.discrete = tool_config.discrete          # 连续/离散模式
        self.profile_npu = None
        self.profile_contents = tool_config.contents
        self.profile_level = tool_config.level
        self.profile_save_path = config.save_path
        self.analysis = tool_config.analysis

    def start(self, **kwargs):
        role = kwargs.get("role", None)
        if not self.discrete and NPUProfiler._define_count == 0:
            self.profile_npu = get_npu_profiler(
                contents=self.profile_contents, profile_level=self.profile_level,
                profile_save_path=self.profile_save_path, analysis=self.analysis,
                role=role,
            )
            self.profile_npu.start()

    def step(self):
        return

    def stop(self):
        if not self.discrete and NPUProfiler._define_count == 1:
            self.profile_npu.step()
            self.profile_npu.stop()
```

`DistProfiler` 统一调度层，根据 `config.tool` 分发到 `NPUProfiler`，并通过 `check_enable()` / `check_this_rank()` 做 rank 级过滤（[`verl/utils/profiler/dist_profiler.py`](https://github.com/verl-project/verl/blob/main/verl/utils/profiler/dist_profiler.py)）：

```python
class DistProfiler:
    def __init__(self, rank, config=None, tool_config=None):
        self._tool = config.tool
        if self._tool == "npu":
            self._impl = NPUProfiler(rank=rank, config=config, tool_config=tool_config)

    def start(self, **kwargs):
        if self.check_enable() and self.check_this_rank():
            return self._impl.start(**kwargs)
    def step(self):
        if self.check_enable() and self.check_this_rank():
            return self._impl.step()
    def stop(self):
        if self.check_enable() and self.check_this_rank():
            return self._impl.stop()
```

`VLLMHttpServer` 服务层，从 `self.config.profiler` 提取 `tool_config` 构建 `DistProfiler`，暴露 HTTP 接口（[`verl/workers/rollout/vllm_http_server.py`](https://github.com/verl-project/verl/blob/main/verl/workers/rollout/vllm_http_server.py)）：

```python
class VLLMHttpServer:
    def __init__(self, ...):
        profiler_config = self.config.profiler
        tool_config = omega_conf_to_dataclass(
            (profiler_config.tool_config or {}).get(profiler_config.tool)
        )
        self.profiler_controller = DistProfiler(
            self.replica_rank, config=profiler_config, tool_config=tool_config,
        )

    async def start_profile(self, **kwargs):
        if self.profiler_controller.check_enable() and \
           self.profiler_controller.is_discrete_mode():
            await self.engine.start_profile(**kwargs)

    async def stop_profile(self):
        if self.profiler_controller.check_enable() and \
           self.profiler_controller.is_discrete_mode():
            await self.engine.stop_profile()
```

`RayPPOTrainer` 训练层，在每个 `train_step` 中按 `global_profiler.steps` 判断是否触发当前步的采集（[`verl/trainer/ppo/ray_trainer.py`](https://github.com/verl-project/verl/blob/main/verl/trainer/ppo/ray_trainer.py)）：

```python
class RayPPOTrainer:
    def train_step(self):
        curr_step_profile = self.global_steps in global_profiler.steps
        with timer("gen"):
            if curr_step_profile:
                self.llm_server_manager.start_profile()
            combined_gen_output = self.async_rollout_manager.generate_sequences(
                combined_gen_batch
            )
            if curr_step_profile:
                self.llm_server_manager.stop_profile()
```

调用链：`RayPPOTrainer.train_step()` → `VLLMHttpServer.start_profile()` → `DistProfiler.start()` → `NPUProfiler.start()` → `get_npu_profiler()`。
需要特别注意的是：如果推理是服务化的方式拉起（与训练worker分进程），需要调用推理引擎的profiler方法，可参考[vllm_async_server.py](https://github.com/verl-project/verl/blob/main/verl/workers/rollout/vllm_rollout/vllm_async_server.py)/[async_sglang_server.py](https://github.com/verl-project/verl/blob/main/verl/workers/rollout/sglang_rollout/async_sglang_server.py)