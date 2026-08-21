# MSTX 接口使用

参考：[Ascend PyTorch Profiler - MSTX 接口说明](https://www.hiascend.com/document/detail/zh/canncommercial/850/devaids/Profiling/atlasprofiling_16_0033.html#ZH-CN_TOPIC_0000002534478481__section5699454151510)

## 第一部分：MSTX 接口说明

### 1. 开启 MSTX

在 `torch_npu.profiler._ExperimentalConfig` 中打开 `mstx`，并按实际采集需求设置 profiler 参数。

```python
experimental_config = torch_npu.profiler._ExperimentalConfig(
    profiler_level=torch_npu.profiler.ProfilerLevel.Level_none,
    mstx=True,
    export_type=[torch_npu.profiler.ExportType.Db],
)
```

### 2. 常用接口

- `torch_npu.npu.mstx.mark(name, domain="default")`
- `torch_npu.npu.mstx.range_start(name, stream=None, domain="default")`
- `torch_npu.npu.mstx.range_end(range_id, domain="default")`
- `torch_npu.npu.mstx.mstx_range(...)`

### 3. 域过滤

可通过 `mstx_domain_include` 或 `mstx_domain_exclude` 控制采集范围，两者不要同时配置。

```python
experimental_config = torch_npu.profiler._ExperimentalConfig(
    mstx=True,
    mstx_domain_include=["default", "domain1"],
)
```

### 4. 最小示例

```python
import torch
import torch_npu

x = torch.randn(1024, 1024, device="npu")
y = torch.randn(1024, 1024, device="npu")

experimental_config = torch_npu.profiler._ExperimentalConfig(
    profiler_level=torch_npu.profiler.ProfilerLevel.Level_none,
    mstx=True,
    export_type=[torch_npu.profiler.ExportType.Db],
)

mstx_prof = torch_npu.profiler.profile(
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("./result"),
    experimental_config=experimental_config,
)
mstx_prof.start()
stream = torch_npu.npu.current_stream()
range_id = torch_npu.npu.mstx.range_start(
    "matmul",
    stream,
)  # 第二个入参传入有效的 stream 时，可同时记录 Host 侧 range 耗时和 Device 侧对应的 range 耗时
z = torch.matmul(x, y)
torch_npu.npu.mstx.range_end(range_id)
mstx_prof.stop()
```

### 5. 补充说明

- `range_start` 传入有效 `stream` 时，可同时记录 Host 侧和 Device 侧耗时。
- 常见场景包括通信算子、dataloader、checkpoint 等打点。

## 第二部分：MSTX 适配参考

### 适配思路

MSTX 适配通常按以下方式组织：

1. 创建独立的 `mstxProfiler` 工厂函数或封装类，统一负责构造 profiler 实例。
2. 在该封装类中提供 `start()` 和 `stop()` 接口，负责控制采集生命周期。
3. 在用户指定的类、函数或业务片段上，通过 `range_start/range_end`、`mark` 或装饰器方式注入 MSTX 打点。
4. 将 MSTX 控制链路与原有 profiler 控制链路解耦，避免混用同一套阶段控制逻辑。

### verl 适配方式参考

`verl` 中已经提供了相对完整的 MSTX 适配路径，可直接作为参考。

涉及文件：

| 文件 | 内容 |
|---|---|
| [`verl/utils/profiler/mstx_profile.py`](https://github.com/verl-project/verl/blob/main/verl/utils/profiler/mstx_profile.py) | MSTX 核心实现，包含 `get_npu_profiler()`、`NPUProfiler.start()`、`NPUProfiler.stop()`、`annotate()`、`mark_start_range()`、`mark_end_range()`。 |
| [`verl/utils/profiler/profile.py`](https://github.com/verl-project/verl/blob/main/verl/utils/profiler/profile.py) | `DistProfiler` 统一分发层，根据 `config.tool == "npu"` 分发到 `mstx_profile.NPUProfiler`。 |
| [`verl/utils/profiler/__init__.py`](https://github.com/verl-project/verl/blob/main/verl/utils/profiler/__init__.py) | 对外导出 `mark_annotate`、`mark_start_range`、`mark_end_range`、`marked_timer` 等接口。 |
| [`verl/workers/engine_workers.py`](https://github.com/verl-project/verl/blob/main/verl/workers/engine_workers.py) | Worker 侧接入点，通过 `DistProfilerExtension` 挂接 `start_profile()` / `stop_profile()`，并在 `@DistProfiler.annotate(...)` 中对具体阶段打点。 |
| [`verl/trainer/ppo/ray_trainer.py`](https://github.com/verl-project/verl/blob/main/verl/trainer/ppo/ray_trainer.py) | Trainer 侧控制入口，在目标 step 命中时调用各角色的 `start_profile()` / `stop_profile()`。 |
| [`tests/utils/test_special_mstx_profile.py`](https://github.com/verl-project/verl/blob/main/tests/utils/test_special_mstx_profile.py) | MSTX 相关单测，可用于查看 `start` / `stop` / `annotate` 的预期行为。 |

### verl 的关键适配链路

`verl` 的 MSTX 适配可以概括为五层：

1. **工厂层**：(`verl/utils/profiler/mstx_profile.py`) 中的 `get_npu_profiler()` 统一构造 profiler 实例，并设置 `mstx` 相关参数。
2. **控制层**：同文件中的 `NPUProfiler` 封装 `start()` / `stop()` / `annotate()`，用于管理连续模式或离散模式下的采集流程。
3. **分发层**：(`verl/utils/profiler/profile.py`) 中的 `DistProfiler` 根据 `tool` 类型将调用路由到 `NPUProfiler`。
4. **Worker 层**：(`verl/workers/engine_workers.py`) 通过 `DistProfilerExtension` 对外暴露 `start_profile()` / `stop_profile()`，并在 `train_batch`、`actor_update`、`actor_compute_log_prob`、`ref_compute_log_prob` 等阶段使用 `@DistProfiler.annotate(...)` 做注入。
5. **Trainer 层**：(`verl/trainer/ppo/ray_trainer.py`) 在命中目标 step 时统一触发各角色的 `start_profile()` / `stop_profile()`。

调用链可参考：

`RayPPOTrainer` → `Worker.start_profile()/stop_profile()` → `DistProfiler.start()/stop()` → `mstx_profile.NPUProfiler.start()/stop()`

阶段打点链路可参考：

`@DistProfiler.annotate(...)` → `mstx_profile.NPUProfiler.annotate()` → `mark_start_range()` / `mark_end_range()`

### 适配时重点参考的位置

- 如果你要参考“如何创建独立 `mstxProfiler` 工厂类”，重点看 (`verl/utils/profiler/mstx_profile.py`)。
- 如果你要参考“如何把 `mstxProfiler` 接入统一 profiler 分发框架”，重点看 (`verl/utils/profiler/profile.py`)。
- 如果你要参考“如何把 `start/stop` 接到 worker 生命周期”，重点看 (`verl/workers/engine_workers.py`)。
- 如果你要参考“如何在训练阶段函数上打 MSTX 标记”，重点看 (`verl/workers/engine_workers.py`) 中的 `@DistProfiler.annotate(...)`。
- 如果你要参考“如何由 trainer 在目标 step 触发采集”，重点看 (`verl/trainer/ppo/ray_trainer.py`)。
