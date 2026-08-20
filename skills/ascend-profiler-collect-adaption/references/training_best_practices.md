# 训练场景 — torch_npu.profiler 适配最佳实践

> 代表框架：MindSpeed-LLM、MindSpeed-MM

下文是已适配框架的结构参考，不应整段复制到新框架。使用本 Skill 时统一生成
`assets/profiler_adapter.py`，由 Agent 只接入目标框架原有配置和真实 optimizer/global-step 边界。
`current_rank`/`rank` 必须来自当前 worker 的 global rank，不能在多进程中统一默认成 0。

## 采集控制方式：配置文件

涉及文件（以 `MindSpeed-LLM` 为例）：

| 文件 | 内容 |
|---|---|
| [`mindspeed_llm/fsdp2/utils/profiler.py`](https://gitcode.com/Ascend/MindSpeed-LLM/blob/master/mindspeed_llm/fsdp2/utils/profiler.py) | `ProfilerConfig` 配置类 + `ProfilerManager` 类（FSDP2 路径） |
| [`mindspeed_llm/fsdp2/train/trainer.py`](https://gitcode.com/Ascend/MindSpeed-LLM/blob/master/mindspeed_llm/fsdp2/train/trainer.py) | `Trainer` 类，在 `__init__` 中构建 `ProfilerConfig`，在 `train()` 中调用 `start/step/stop` |

MindSpeed-LLM 通过 YAML 配置文件控制 profiler，启动时解析为 `ProfilerConfig` 参数配置类。

YAML 配置文件方式：

```yaml
training:
  profile: true
  profile_step_start: 5
  profile_step_end: 15
  profile_ranks: [0]
  profile_level: level1
  profile_with_cpu: true
  profile_save_path: ./profiler_results
```

`ProfilerConfig` 的具体实现如下：

```python
@dataclass
class ProfilerConfig:
    enabled: bool = False
    profile_step_start: int = 0            # 采集起始 step（包含）
    profile_step_end: int = -1             # 采集结束 step（不包含），-1=直到训练结束
    profile_ranks: List[int] = None        # 采集的卡号，默认 None，__post_init__ 中自动转为 [-1]
    profile_level: str = "level1"          # level_none / level0 / level1 / level2
    profile_export_type: str = "db"      # text / db
    profile_data_simplification: bool = False
    profile_with_cpu: bool = False
    profile_with_stack: bool = False
    profile_with_memory: bool = False
    profile_record_shapes: bool = False
    profile_save_path: str = "./profile"
    current_rank: int = 0

    def __post_init__(self):
        if self.profile_ranks is None:
            self.profile_ranks = [-1]

    def is_profiling_rank(self) -> bool:
        if not self.enabled:
            return False
        if -1 in self.profile_ranks:
            return True
        return self.current_rank in self.profile_ranks
```

`ProfilerManager` 接收 `ProfilerConfig` 配置对象作为构造参数，在内部完成参数解析并透传给 `torch_npu.profiler` 底层接口。该类对外暴露 start()、step()、stop() 三个核心方法，供业务注入点在训练的关键路径上调用,具体实现如下：

```python
class ProfilerManager:
    def __init__(self, config: ProfilerConfig):
        self.config = config
        self.profiler = None
        self._started = False

        if not config.is_profiling_rank():   # ProfilerConfig 上的方法，内含 enabled 检查
            return

        Path(config.profile_save_path).mkdir(parents=True, exist_ok=True)

        # level / export_type 字符串 → torch_npu 枚举（dict 查表 + ValueError 校验）
        profiler_level = _str_to_level(config.profile_level)
        profile_export_type = _str_to_export_type(config.profile_export_type)

        # --- Schedule（根据 step 区间内部推导，用户无需关心）---
        if config.profile_step_end == -1:
            active = 1000000
        else:
            active = config.profile_step_end - config.profile_step_start
            if active <= 0:
                raise ValueError("profile_step_end must be > profile_step_start")
        skip_first = max(0, config.profile_step_start - 1)
        warmup = 0 if config.profile_step_start == 0 else 1

        activities = [npu_profiler.ProfilerActivity.NPU]
        if config.profile_with_cpu:
            activities.append(npu_profiler.ProfilerActivity.CPU)

        self.profiler = npu_profiler.profile(
            activities=activities,
            schedule=npu_profiler.schedule(
                wait=0, warmup=warmup, active=active, repeat=1, skip_first=skip_first
            ),
            on_trace_ready=npu_profiler.tensorboard_trace_handler(config.profile_save_path),
            record_shapes=config.profile_record_shapes,
            profile_memory=config.profile_with_memory,
            with_stack=config.profile_with_stack,
            experimental_config=npu_profiler._ExperimentalConfig(
                aic_metrics=npu_profiler.AiCMetrics.PipeUtilization,
                profiler_level=profiler_level,
                export_type=profile_export_type,
                data_simplification=config.profile_data_simplification,
            ),
        )
        # 添加分布式元数据（rank + world_size）
        self.profiler.add_metadata_json("distributed_args", json.dumps({...}))

    def start(self):
        if self.profiler is not None and not self._started:
            self.profiler.start()
            self._started = True
            logger.info_rank0(f"[RANK {self.config.current_rank}] Profiling started.")

    def step(self):
        if self.profiler is not None:
            self.profiler.step()

    def stop(self):
        if self.profiler is not None:
            self.profiler.stop()
            logger.info_rank0(f"[RANK {self.config.current_rank}] Profiling stopped. "
                              f"Trace saved to {self.config.profile_save_path}")
```

`Trainer` 类初始化的时初始化 `ProfilerManager`类，在训练循环入口处调用 `start()` 方法、在每一步训练结束后调用  `step()` 方法，在训练循环结束处调用 `stop()`方法结束profiling采集。

```python
class Trainer:
    def __init__(self, model, optimizer, lr_scheduler, train_dataloader,
                 args, parallel_args, optimization_args, data_args,
                 ckpt_manager, monitor, tokenizer=None):
        # ... 其他字段 ...
        current_rank = dist.get_rank() if dist.is_initialized() else 0
        prof_config = ProfilerConfig(
            enabled=args.profile,              # 及 profile_step_start/end, ranks,
            profile_level=args.profile_level,  # level, export_type, data_simplification,
            # ...                          # with_cpu/stack/memory, record_shapes,
            profile_save_path=args.profile_save_path,  # save_path, current_rank
            current_rank=current_rank,         # ——共 13 个字段从 TrainingArguments 映射
        )
        self.profiler_manager = ProfilerManager(prof_config)

    def train(self, resume_from_checkpoint=None):
        # ... 前置逻辑 ...
        if self.profiler_manager.profiler is not None:
            self.profiler_manager.start()

        for epoch in range(epochs_trained, int(args.num_train_epochs)):
            for update_step in range(start_update_step, total_updates):
                # ... forward/backward/optimizer ...
                self.global_step += 1

                if self.profiler_manager.profiler is not None:
                    self.profiler_manager.step()

        if self.profiler_manager.profiler is not None:
            self.profiler_manager.stop()
```

调用链：YAML配置文件 → `fsdp2_parse_args` → `TrainingArguments` → `Trainer.__init__` 内部构建 `ProfilerConfig` → `ProfilerManager`→ `Trainer.train()` 调用 `start/step/stop`

## 与通用适配模板的字段映射

| 训练范例字段 | 通用模板字段 | 说明 |
| --- | --- | --- |
| `profile` / `enabled` | `enabled` | 必须默认关闭 |
| `profile_save_path` | `output_dir` | 多 rank 使用独立目录或 `worker_name` |
| `profile_step_start` | `start_step` | 调用 `step()` 前跳过的完整业务 step 数 |
| `profile_step_end - profile_step_start` | `active` | 采集窗口长度，必须大于 0 |
| `profile_ranks` | `ranks` | `-1` 表示全部 rank |
| `current_rank` | `rank` | 当前 worker 的 global rank；启用多进程采集时必需 |
| `profile_level` | `level` | `level_none` / `level0` / `level1` / `level2` |
| `profile_export_type` | `export_type` | 完整可视化验收使用 `text` |

例如 `start_step=2, wait=0, warmup=1, active=2, repeat=1` 至少需要调用
`2 + 0 + (1 + 2) * 1 = 5` 次 `step()`。在接线前用模板中的 `validate_step_budget` 检查总 step 数。

## callback 的异常安全边界

Trainer/Engine 的 `on_step_end` 很适合映射 optimizer/global step，但不能默认认为 `on_train_end` 会在
训练异常时执行。优先由调用原生 `train()` 的边界持有 controller：

```python
controller = ProfilerController(config)
callback = ProfilerStepCallback(controller)
trainer.add_callback(callback)
with controller:
    result = trainer.train()
```

callback 只调用 `controller.step()`。若框架无法在 `train()` 外包裹生命周期，则必须先从源码或测试证明
异常路径也会调用结束 hook，才能让开始/结束 callback 单独负责 `start/stop`。上面的 MindSpeed 代码仅用于
理解既有字段和注入点，不是异常安全范式；新适配应使用通用 adapter 的 context manager。
