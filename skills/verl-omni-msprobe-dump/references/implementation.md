# verl-omni msprobe 采集实现细节

本文档是 `verl-omni-msprobe-dump` 技能的详细实现参考，包含每个文件的改动清单、实际插入点与可直接照抄的代码模板。动手改代码前必读。

## 1. 背景：verl-omni 与 verl 主仓的差异

上游 msprobe 文档《异步架构 verl 训推一致性比对数据采集》针对的是 verl v0.8.0 的 LLM PPO 架构（`verl/workers/engine/fsdp/transformer_impl.py` 的 `FSDPEngine`、`verl/workers/rollout/llm_server.py` 的 `LLMServerClient`、`verl/trainer/ppo/ray_trainer.py`）。而 verl-omni 是 diffusion 训练框架，包名是 `verl_omni`，对应结构完全不同：

| 上游 verl（LLM PPO） | verl-omni（diffusion） |
| --- | --- |
| `FSDPEngine.forward_backward_batch`（micro_batch 循环） | `DiffusersFSDPEngine._run_forward_backward_batch`（micro_batch × timestep 双层循环） |
| `LLMServerClient.generate` 注入 `request_id` | `vLLMOmniHttpServer.generate` 接收 `request_id`，需注入 `extra_fields` |
| vLLM 调度 prefill/decode step（`execute_model`） | diffusion transformer 前向（denoise step） |
| `PROMPTS_ONLY` 裁剪 response（仅 prefill） | 无 prefill/decode 概念，改为 denoise step / selected step |

因此本技能不照搬上游的 LLM 插入点，而是适配到 verl-omni 的 diffusion 结构。上游的 `request_id` 贯穿思想、msprobe 配置文件格式、`start/stop/step` 用法保持一致。

## 2. 文件改动清单

| 文件 | 修改类型 | 说明 |
| --- | --- | --- |
| `verl_omni/utils/msprobe_dump.py` | **新增** | 采集 helper（惰性 import、request_id 提取、日志写入） |
| `verl_omni/workers/rollout/vllm_rollout/vllm_omni_async_server.py` | 修改 | `generate()` 注入 `request_id` 到 `extra_fields`（最关键） |
| `verl_omni/pipelines/*/vllm_omni_rollout_adapter.py` | 修改 | 生成侧：包裹 diffusion transformer 前向，写 `dispatch_log.jsonl` |
| `verl_omni/workers/engine/fsdp/diffusers_impl.py` | 修改 | 训练侧：包裹 `forward_step`，写 `update_actor_log.jsonl` |
| 启动脚本 / 命令行 | 修改 | 环境变量 + hydra 参数 + msprobe 配置文件 |

通常不需要改动：
- `verl_omni/agent_loop/diffusion_agent_loop.py`：`_postprocess()` 已把 `output.extra_fields` 转成 `DataProto.non_tensor_batch`，无需改。
- `verl_omni/agent_loop/single_turn_agent_loop.py`：已在第 105 行 `request_id=uuid4().hex` 生成并传给 server，无需改。
- `verl_omni/workers/rollout/diffusion_llm_server.py`：`DiffusionWholeSampleRetryLLMServerClient` 已透传 `output.extra_fields`，无需改。

## 3. 核心不变量：request_id 贯穿链路

```text
single_turn_agent_loop.run(): request_id = uuid4().hex
  → server_manager.generate(request_id=...)
  → vLLMOmniHttpServer.generate(request_id)          ← 步骤 4 在这里注入
  → DiffusionOutput.extra_fields["request_id"]
  → DiffusionAgentLoopOutput.extra_fields
  → DiffusionAgentLoopWorker._postprocess()
  → DataProto.non_tensor_batch["request_id"]
  → DiffusersFSDPEngine micro_batch (TensorDict)      ← 步骤 6 在这里读取
  → update_actor_log.jsonl
```

`request_id` 在 rollout 前创建，贯穿到训练侧 micro_batch，两侧日志靠它关联同一份样本。**漏掉步骤 4 的注入，两侧永远对不上，属于阻断级缺陷。**

## 4. request_id 注入（server 端）

**文件**：`verl_omni/workers/rollout/vllm_rollout/vllm_omni_async_server.py`

**方法**：`vLLMOmniHttpServer.generate()`（约第 373 行）

原代码（第 402-403 行）：

```python
        final_res = await self._run_generation(prompt, params, request_id, lora_request, priority)
        return self._process_output(final_res, params, sampling_params)
```

改为：

```python
        final_res = await self._run_generation(prompt, params, request_id, lora_request, priority)
        output = self._process_output(final_res, params, sampling_params)
        output.extra_fields["request_id"] = request_id
        return output
```

说明：
- `_process_output` 返回 `DiffusionOutput` 或 `TokenOutput`，两者都带可变 `extra_fields` dict，直接写字段即可。
- 在 server 端注入比改 client 端（`LLMServerClient`）更通用，AR 与 diffusion 模式都覆盖。
- AR 模式（`self._ar_mode`）与 diffusion 模式共用这个 `generate()` 入口，一次改动两处都生效。

## 5. 生成侧采集（pipeline adapter）

**文件**：当前激活的 `verl_omni/pipelines/*/vllm_omni_rollout_adapter.py`。不同模型/算法对应不同文件，例如：

- MiniMax H3 FlowGRPO：`verl_omni/pipelines/minimax_h3_flow_grpo/vllm_omni_rollout_adapter.py`（`MiniMaxH3PipelineWithLogProb`）
- Qwen Image FlowGRPO：`verl_omni/pipelines/qwen_image_flow_grpo/vllm_omni_rollout_adapter.py`
- LTX2 / SD3 / Wan / Bagel：各自的 `vllm_omni_rollout_adapter.py`

**目标**：在 diffusion transformer 前向（denoise step）前后包裹 `debugger.start/stop/step`，每 dump 一个 denoise step 写一行 `dispatch_log.jsonl`。

### 5.1 以 MiniMax H3 FlowGRPO 为例（最完整的参考）

#### (a) `__init__` 增加状态

在 `MiniMaxH3PipelineWithLogProb.__init__` 末尾加：

```python
        self._debugger = None
        self._generate_dump_logger_fp = None
        self._current_request_id = None
```

#### (b) `forward()` 记录当前 request_id

`forward()` 中已有 `req = request.requests[0]`，在其后加：

```python
        req = request.requests[0]
        self._current_request_id = str(req.request_id)
```

> 单请求 pipeline（`supports_request_batch = False`），用 `self._current_request_id` 缓存是安全的。若 pipeline 支持 batch（`supports_request_batch = True`），用 `request_ids = [str(r.request_id) for r in request.requests]`，并将列表传入 `diffuse` 路径或对应的日志记录点。

#### (c) 增加辅助方法（加在类末尾）

```python
    def _ensure_generate_debugger(self) -> None:
        if self._debugger is not None:
            return
        from verl_omni.utils.msprobe_dump import (
            create_precision_debugger,
            dump_enabled,
            dump_phase,
            open_step_logger,
            resolve_config_path,
        )

        if not dump_enabled():
            return
        if dump_phase() not in ("all", "log_prob", "generate"):
            return
        self._debugger = create_precision_debugger(resolve_config_path("generate"))
        if self._debugger is not None:
            self._generate_dump_logger_fp = open_step_logger(self._debugger, "dispatch_log.jsonl")

    def _should_dump_generate_step(self, is_selected: bool) -> bool:
        self._ensure_generate_debugger()
        if self._debugger is None:
            return False
        return int(os.environ.get("DUMP_ROLLOUT_ALL_STEPS", "0")) == 1 or is_selected

    def _log_generate_dump_step(self, *, request_ids, denoise_index, is_selected, extra=None) -> None:
        from verl_omni.utils.msprobe_dump import write_step_log

        if self._debugger is None:
            return
        try:
            current_iter = self._debugger.service.current_iter
        except Exception:
            current_iter = -1
        rank = torch.distributed.get_rank() if torch.distributed.is_initialized() else 0
        metadata = {
            "phase": "generate",
            "denoise_index": int(denoise_index),
            "sde_selected": bool(is_selected),
        }
        if extra:
            metadata.update(extra)
        write_step_log(
            self._generate_dump_logger_fp,
            source="generate_sequence",
            rank=rank,
            step=current_iter,
            request_ids=request_ids,
            extra=metadata,
        )
```

> 注意：`_ensure_generate_debugger` 等方法里用到了 `os`、`torch`，确保文件顶部已 import（minimax_h3 的 adapter 已 import `torch`，`os` 若无则补 `import os`）。

#### (d) 包裹 transformer 前向

`diffuse()` 中 denoise 循环（约第 264 行起）的原代码：

```python
                    model_inputs = branch.forward_kwargs(...)
                    video_velocity, audio_velocity = transformer(**model_inputs)
                    is_selected = step in selected
                    video_transition = sample_h3_transition(...)
```

改为：

```python
                    model_inputs = branch.forward_kwargs(...)
                    is_selected = step in selected
                    dump_this = self._should_dump_generate_step(is_selected)
                    if dump_this:
                        self._debugger.start(model=transformer)
                    video_velocity, audio_velocity = transformer(**model_inputs)
                    if dump_this:
                        if not self._debugger.service.should_stop_service:
                            self._log_generate_dump_step(
                                request_ids=[self._current_request_id],
                                denoise_index=step,
                                is_selected=is_selected,
                                extra={
                                    "timestep": float(video_t),
                                    "audio_timestep": float(audio_timestep),
                                },
                            )
                        self._debugger.stop()
                        self._debugger.step()
                    video_transition = sample_h3_transition(...)
```

说明：
- `is_selected = step in selected` 与 transformer 前向无关，提前计算不影响业务逻辑。
- `dump_this` 默认只对 selected（SDE）step 为真：FlowGRPO 训练侧只回放 selected step，dump 全部 denoise step 会产生大量无法对齐的冗余数据。
- `timestep` / `audio_timestep` 用于与训练侧 timestep 值交叉验证对齐。

### 5.2 其他 pipeline 的通用适配要点

- 找到真正执行 diffusion transformer 前向的调用（形如 `noise_pred = self.transformer(...)` 或 `transformer(**kwargs)`），在其前后包裹 debugger。
- 若有 classifier-free guidance（正/负两个分支），优先用**一个** msprobe step 覆盖同一逻辑 denoise step（若与 actor 前向对齐）；否则在 `extra` 中记录 `cfg_branch` 使关联显式化。
- request_id 来源：`request.requests[0].request_id`（单请求）或 `[r.request_id for r in request.requests]`（batch）。
- 若 pipeline 的 transformer 前向封装在独立 helper 中，优先在该 helper 内包裹，而非最外层 `forward()`，否则一个 msprobe step 会覆盖整段去噪过程，与训练侧 timestep 粒度对不上。

## 6. 训练侧采集（FSDP actor）

**文件**：`verl_omni/workers/engine/fsdp/diffusers_impl.py`

**类**：`DiffusersFSDPEngine`（基类，第 78 行）。子类 `PPODiffusersFSDPEngine`、`NFTDiffusersFSDPEngine` 复用基类的 `_run_forward_backward_batch`（第 773 行），`DPODiffusersFSDPEngine` 有独立 `forward_backward_batch`（第 1033 行），需单独处理。

### 6.1 `__init__` 增加状态

在 `DiffusersFSDPEngine.__init__` 中（`self.mode = None` 附近）加：

```python
        self._debugger = None
        self._actor_dump_logger_fp = None
```

### 6.2 增加辅助方法（加在 `_run_forward_backward_batch` 之前）

```python
    def _ensure_debugger(self):
        if self._debugger is not None:
            return
        if self.engine_config.forward_only:
            return
        from verl_omni.utils.msprobe_dump import (
            create_precision_debugger,
            dump_enabled,
            open_step_logger,
            resolve_config_path,
        )

        if not dump_enabled():
            return
        self._debugger = create_precision_debugger(resolve_config_path("actor"))
        if self._debugger is not None:
            self._actor_dump_logger_fp = open_step_logger(self._debugger, "update_actor_log.jsonl")

    def _should_dump_forward(self, forward_only: bool) -> bool:
        from verl_omni.utils.msprobe_dump import should_dump_phase

        self._ensure_debugger()
        if self._debugger is None:
            return False
        phase = "log_prob" if forward_only else "update_actor"
        return should_dump_phase(phase)

    def _log_actor_dump_step(self, micro_batch, *, phase, timestep_idx) -> None:
        from verl_omni.utils.msprobe_dump import extract_non_tensor, extract_request_ids, write_step_log

        if self._debugger is None:
            return
        try:
            current_iter = self._debugger.service.current_iter
        except Exception:
            current_iter = -1
        write_step_log(
            self._actor_dump_logger_fp,
            source=phase,
            rank=self.rank,
            step=current_iter,
            request_ids=extract_request_ids(micro_batch),
            extra={
                "phase": phase,
                "timestep_idx": int(timestep_idx),
                "uid": extract_non_tensor(micro_batch, "uid"),
            },
        )
```

> `self.rank` 已在 `__init__` 第 108 行定义；`self.engine_config.forward_only` 已存在。ref 引擎（forward_only=True）跳过采集，只有 actor 引擎创建 debugger。

### 6.3 包裹 `forward_step`（`_run_forward_backward_batch`）

原代码（第 798-806 行）：

```python
            with ctx:
                for step in range(num_timesteps):
                    loss, meta_info = self.forward_step(
                        micro_batch, loss_function=loss_function, forward_only=forward_only, step=step
                    )
                    if not forward_only:
                        loss.backward()
                    for key, val in meta_info.items():
                        meta_info_lst[key].append(val)
```

改为：

```python
            should_dump = self._should_dump_forward(forward_only)
            phase = "log_prob" if forward_only else "update_actor"
            with ctx:
                for step in range(num_timesteps):
                    if should_dump:
                        self._debugger.start(model=self.module)
                    loss, meta_info = self.forward_step(
                        micro_batch, loss_function=loss_function, forward_only=forward_only, step=step
                    )
                    if not forward_only:
                        loss.backward()
                    if should_dump:
                        if not self._debugger.service.should_stop_service:
                            self._log_actor_dump_step(micro_batch, phase=phase, timestep_idx=step)
                        self._debugger.stop()
                        self._debugger.step()
                    for key, val in meta_info.items():
                        meta_info_lst[key].append(val)
```

### 6.4 DPO 特例（`DPODiffusersFSDPEngine`）

`DPODiffusersFSDPEngine.forward_backward_batch`（第 1033 行）不调用 `_run_forward_backward_batch`，需在其 `forward_step` 调用前后单独加同样的 `should_dump` / `start` / `stop` / `step` / `_log_actor_dump_step`，并记录 `timestep_idx=-1`（DPO 单次前向，无 timestep 循环）。

> 具体插入点以实际代码为准：先定位 `DPODiffusersFSDPEngine.forward_backward_batch` 中唯一的 `forward_step(...)` 调用，用 `self._should_dump_forward(forward_only)` 与 `phase = "log_prob" if forward_only else "update_actor"` 包裹，日志 `timestep_idx=-1`。

## 7. helper 模块完整实现

**文件**：`verl_omni/utils/msprobe_dump.py`（新建）

```python
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def dump_enabled() -> bool:
    return int(os.environ.get("DUMP_ON", "0")) == 1


def dump_phase() -> str:
    return os.environ.get("DUMP_PHASE", "log_prob")


def should_dump_phase(phase: str) -> bool:
    configured = dump_phase()
    return configured == "all" or configured == phase


def resolve_config_path(side: str) -> str | None:
    """返回指定侧的 msprobe 配置文件路径，优先取环境变量。"""
    env_key = "MSPROBE_CONFIG_ACTOR" if side == "actor" else "MSPROBE_CONFIG_GENERATE"
    path = os.environ.get(env_key)
    if path:
        return path
    dump_root = os.environ.get("DUMP_PATH")
    if not dump_root:
        return None
    name = "config_actor.json" if side == "actor" else "config_generate.json"
    return str(Path(dump_root) / name)


def create_precision_debugger(config_path: str | None):
    """惰性创建 PrecisionDebugger；msprobe 缺失或配置缺失时返回 None 并告警。"""
    if not config_path:
        return None
    try:
        try:
            from msprobe.pytorch.debugger.precision_debugger import PrecisionDebugger
            from msprobe.pytorch.common.utils import seed_all
        except Exception:
            from msprobe.pytorch import PrecisionDebugger, seed_all
    except Exception as exc:
        logger.warning("msprobe unavailable, skip dump init: %s", exc)
        return None
    if not os.path.isfile(config_path):
        logger.warning("msprobe config missing: %s", config_path)
        return None
    seed_all(mode=True)
    debugger = PrecisionDebugger(config_path=config_path)
    try:
        pid_dump = os.path.join(str(debugger.config.dump_path), str(os.getpid()))
        debugger.config.dump_path = pid_dump
        if getattr(debugger, "service", None) is not None:
            debugger.service.config.dump_path = pid_dump
    except Exception:
        pass
    return debugger


def open_step_logger(debugger, filename: str):
    """在 debugger 的 pid 子目录下打开一条 jsonl 日志，返回文件句柄或 None。"""
    if debugger is None:
        return None
    try:
        dump_path = getattr(debugger.config, "dump_path", None)
        if dump_path is None and getattr(debugger, "service", None) is not None:
            dump_path = debugger.service.config.dump_path
        log_dir = Path(dump_path)
        log_dir.mkdir(parents=True, exist_ok=True)
        return open(log_dir / filename, "a", encoding="utf-8")
    except Exception as exc:
        logger.warning("failed to open msprobe metadata log %s: %s", filename, exc)
        return None


def normalize_request_id(value: Any) -> str:
    text = str(value)
    # 某些 vLLM 版本会给外部 request_id 追加 "-1234abcd" 后缀。
    suffix = text.rsplit("-", 1)
    if len(suffix) == 2 and len(suffix[1]) == 8:
        try:
            int(suffix[1], 16)
            return suffix[0]
        except ValueError:
            pass
    return text


def _to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [normalize_request_id(item) for item in value]
    try:
        import numpy as np

        if isinstance(value, np.ndarray):
            items = value.tolist()
            if not isinstance(items, list):
                return [normalize_request_id(items)]
            return [normalize_request_id(item) for item in items]
    except Exception:
        pass
    return [normalize_request_id(value)]


def extract_non_tensor(payload: Any, key: str) -> list[str]:
    value = None
    # DataProto 将 rollout 元数据存在 non_tensor_batch 中。
    if hasattr(payload, "non_tensor_batch"):
        try:
            value = payload.non_tensor_batch.get(key, None)
        except Exception:
            value = None
    # TensorDict micro_batch 将元数据存为 NonTensorData/NonTensorStack，tu.get 会正确解包。
    if value is None:
        try:
            from verl.utils import tensordict_utils as tu

            value = tu.get(payload, key=key, default=None)
        except Exception:
            value = None
    try:
        if value is None and hasattr(payload, "get"):
            value = payload.get(key, None)
    except Exception:
        value = None
    return _to_list(value)


def extract_request_ids(payload: Any) -> list[str]:
    for key in ("request_id", "uid", "sample_id"):
        values = extract_non_tensor(payload, key)
        if values:
            return values
    return []


def write_step_log(
    fp,
    *,
    source: str,
    rank: int,
    step: int,
    request_ids: Iterable[str],
    extra: dict[str, Any] | None = None,
) -> None:
    if fp is None:
        return
    ids = [str(item) for item in request_ids]
    record = {
        "source": source,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "pid": os.getpid(),
        "rank": int(rank),
        "step": int(step),
        "request_ids": ids,
        "num_requests": len(ids),
    }
    if extra:
        record.update(extra)
    fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    fp.flush()
```

> 不要默认记录原始 prompt；确需语句级排查时，仅在 `DUMP_LOG_RAW_PROMPT=1` 时追加。

## 8. msprobe 配置文件

两侧各一份，仅 `dump_path` 不同。通过环境变量 `MSPROBE_CONFIG_ACTOR` / `MSPROBE_CONFIG_GENERATE` 指定，或放 `DUMP_PATH` 下命名为 `config_actor.json` / `config_generate.json`。

```json
{
  "task": "statistics",
  "dump_path": "/path/to/msprobe_dump/update_actor",
  "rank": [],
  "step": [],
  "level": "L0",
  "async_dump": false,
  "statistics": {
    "scope": [],
    "list": [],
    "tensor_list": [],
    "data_mode": ["all"],
    "summary_mode": "statistics"
  }
}
```

生成侧同一结构，`dump_path` 换成 `.../generate_sequence`。

## 9. 启动配置

```bash
export DUMP_ON=1                    # 启用采集（默认关闭，DUMP_ON=0 行为不变）
export DUMP_PHASE=log_prob          # all | log_prob | update_actor | generate
export DUMP_PATH=/path/to/msprobe_dump
export MSPROBE_CONFIG_ACTOR=$DUMP_PATH/config_actor.json
export MSPROBE_CONFIG_GENERATE=$DUMP_PATH/config_generate.json
export DUMP_ROLLOUT_ALL_STEPS=0     # 默认只 dump FlowGRPO 的 selected step
```

推荐 hydra 覆盖（按 recipe 支持的参数调整）：

```bash
trainer.val_before_train=False
data.shuffle=False
actor_rollout_ref.rollout.enforce_eager=True
actor_rollout_ref.rollout.calculate_log_probs=True
```

短采集建议：`trainer.total_training_steps=1`、小 batch、关闭周期验证/保存/测试，减少干扰。

## 10. 输出结构

```text
{DUMP_PATH}/generate_sequence/{pid}/
  step_0/rank_*/dump.json
  step_1/rank_*/dump.json
  dispatch_log.jsonl

{DUMP_PATH}/update_actor/{pid}/
  step_0/rank_*/dump.json
  step_1/rank_*/dump.json
  update_actor_log.jsonl
```

`dispatch_log.jsonl` 字段：`source=generate_sequence`、`pid`、`rank`、`step`（msprobe current_iter）、`request_ids`、`phase=generate`、`denoise_index`、`sde_selected`、`timestep`/`audio_timestep`。

`update_actor_log.jsonl` 字段：`source=log_prob|update_actor`、`pid`、`rank`、`step`（msprobe current_iter）、`request_ids`、`phase`、`timestep_idx`、`uid`。

## 11. 数据关联步骤

1. **选生成侧记录**：在 `dispatch_log.jsonl` 中取一条 `phase=generate` 且 `sde_selected=true`、`num_requests=1` 的记录，记下 `request_id` 与 `denoise_index`。
2. **定位训练侧记录**：在 `update_actor_log.jsonl` 中搜索同一 `request_id`，得到对应 `step`、`rank`、`timestep_idx`。
3. **timestep 对齐**：
   - FlowGRPO：rollout 侧按 `denoise_index` 升序排列的 selected 记录，第 k 条对应 actor 侧 `timestep_idx=k`（训练侧 `all_timesteps` 只含 selected transition）。
   - 单步 DPO：actor 侧 `timestep_idx=-1`，仅用 `request_id` + timestep/noise 元数据对齐。
   - 有 `timestep` / `audio_timestep` 字段时，优先用 timestep 值交叉验证。
4. **读 dump**：根据两侧各自的 `step` 与 `rank` 读取 `step_{step}/rank_{rank}/dump.json`。
5. **交下游**：将两侧 `dump.json` 交给 [rl-consistency-analysis](../rl-consistency-analysis/SKILL.md) 做根因分析。

> 严禁仅凭文件顺序推断对齐：异步 rollout、多 worker、同 prompt 多次重试、验证、重试/抢占都会破坏顺序假设。

## 12. 验证清单

- [ ] `DUMP_ON=0` 时训练行为与改动前完全一致。
- [ ] msprobe 包缺失时打 warning，不 crash import 期测试。
- [ ] `vLLMOmniHttpServer.generate()` 返回时 `output.extra_fields["request_id"]` 存在。
- [ ] `_postprocess()` 后 `DataProto.non_tensor_batch["request_id"]` 存在。
- [ ] `dispatch_log.jsonl` 每 dump 一个生成侧 denoise step 写一行。
- [ ] `update_actor_log.jsonl` 每 dump 一个训练侧 micro-batch/timestep 写一行。
- [ ] 至少一个 `request_id` 同时出现在两条日志中。
- [ ] 两侧引用的 msprobe `step_*` 目录都存在。

低成本自检：

```bash
python -m py_compile \
  verl_omni/utils/msprobe_dump.py \
  verl_omni/workers/rollout/vllm_rollout/vllm_omni_async_server.py \
  verl_omni/workers/engine/fsdp/diffusers_impl.py
```
