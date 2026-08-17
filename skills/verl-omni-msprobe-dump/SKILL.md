---
name: verl-omni-msprobe-dump
description: >
  在 verl-omni 仓库中注入 msprobe PrecisionDebugger 采集代码，为训推一致性比对采集
  推理侧（vLLM-Omni rollout）与训练侧（DiffusersFSDPEngine actor）两侧 tensor dump 数据，
  并保证 request_id 贯穿两侧、可据此关联同一份样本。使用场景：verl-omni 扩散模型
  （FlowGRPO/DPO 等）做训推一致性比对前需要先采集两侧 dump；或用户提到 "verl-omni 采集"、
  "msprobe dump"、"训推一致性数据采集"、"给 verl-omni 加 dump 代码"、"PrecisionDebugger"、
  "request_id 贯穿"、"训推不一致"、"精度比对数据采集" 等。
---

# verl-omni 训推一致性数据采集

## 概述

本技能在 `verl-omni` 仓库中注入 msprobe `PrecisionDebugger` 采集代码，为训推一致性比对采集两侧数据：

- **推理侧（生成侧）**：vLLM-Omni rollout 的 diffusion transformer 前向（denoise step）
- **训练侧（Actor 侧）**：`DiffusersFSDPEngine` 的 forward/backward（micro-batch × timestep）

产物为两侧的 `step_N/rank_M/dump.json` + 两条元数据日志（`dispatch_log.jsonl` / `update_actor_log.jsonl`），二者通过贯穿全链路的 `request_id` 关联，供下游 [rl-consistency-analysis](../rl-consistency-analysis/SKILL.md) 做根因分析。

> 本技能只负责**采集**；比对/根因分析请用 `rl-consistency-analysis`。
> 不考虑 Megatron 后端，只覆盖 verl-omni 的 FSDP/FSDP2（`DiffusersFSDPEngine`）。

## 核心不变量（成败关键）

`request_id` 在 rollout 前创建，注入 `DiffusionOutput.extra_fields`，随数据流贯穿到训练侧 micro_batch，并在两侧日志中记录。

```text
vLLMOmniHttpServer.generate(request_id)
  → DiffusionOutput.extra_fields["request_id"]          ← 必须注入
  → DiffusionAgentLoopOutput.extra_fields
  → DiffusionAgentLoopWorker._postprocess()
  → DataProto.non_tensor_batch["request_id"]
  → DiffusersFSDPEngine micro_batch (TensorDict)
  → update_actor_log.jsonl
```

- 漏掉 server 端注入，两侧日志永远无法关联 → **阻断级缺陷**。
- 两侧的 msprobe `step` 序号各自独立，**不能只用 step 序号对齐**，必须用 `request_id` + timestep 元数据（`denoise_index` / `timestep_idx` / `sde_selected`）对齐。

## 工作流程

按以下步骤执行；具体文件、插入点与代码模板见 [references/implementation.md](references/implementation.md)。

1. **读实现细节**：先读 [references/implementation.md](references/implementation.md)，确认文件清单、类名、方法与插入点。动手前用 `git status` / `git diff` 确认从干净基线开始，勿在已有 msprobe 半成品上叠加。
2. **新增 helper 模块**：创建 `verl_omni/utils/msprobe_dump.py`（惰性 import msprobe，`DUMP_ON=0` 时零开销）。
3. **注入 request_id**：修改 `vllm_omni_async_server.py` 的 `vLLMOmniHttpServer.generate()`，把 `request_id` 写入返回输出的 `extra_fields`。这是最关键的一步。
4. **生成侧采集**：修改当前 pipeline 的 `vllm_omni_rollout_adapter.py`，在 diffusion transformer 前向（denoise step）前后包裹 `debugger.start/stop/step`，写 `dispatch_log.jsonl`（含 request_id、denoise_index、sde_selected、timestep）。
5. **训练侧采集**：修改 `diffusers_impl.py` 的 `DiffusersFSDPEngine`，在 micro-batch × timestep 的 `forward_step` 前后包裹 debugger，写 `update_actor_log.jsonl`（含 request_id、timestep_idx、phase）。`DPODiffusersFSDPEngine` 走独立路径，单独处理。
6. **配置与启动**：准备 `config_generate.json` / `config_actor.json`，设置 `DUMP_ON=1` 等环境变量与 hydra 参数，关闭 `val_before_train`。
7. **验证数据能对上**：跑一轮短采集后，从 `dispatch_log.jsonl` 选一个 request_id，在 `update_actor_log.jsonl` 找到同一 request_id，核对两侧引用的 `step_*` 目录都存在。未通过关联校验前不得视为完成。

## 规则

- **原有业务逻辑不可变更**：只在现有代码上新增采集能力，禁止改动 diffusion/训练/采样逻辑。
- **惰性 import**：msprobe 只在 `DUMP_ON=1` 时导入，保证 CPU/单测/非采集运行不受影响；msprobe 缺失时打 warning 而非崩溃。
- **路径不可硬编码**：`/home/config_actor.json` 等示例路径必须用环境变量（`MSPROBE_CONFIG_ACTOR` / `MSPROBE_CONFIG_GENERATE` / `DUMP_PATH`）覆盖。
- **request_id 贯穿是硬约束**：缺失即阻断。
- **FlowGRPO 默认只 dump selected step**：训练侧只回放 selected（SDE）step，dump 全部 denoise step 会产生大量无法对齐的冗余数据；确需全量才设 `DUMP_ROLLOUT_ALL_STEPS=1`。
- **关闭 val_before_train**：避免训练前验证 rollout 污染 dump。
- **不能用文件顺序推断对齐**：异步 rollout、多 worker、同 prompt 多次重试、验证、重试/抢占都会破坏顺序假设。

## Reference 说明

| reference 文件 | 主要内容 | 何时读取 |
| --- | --- | --- |
| [`references/implementation.md`](references/implementation.md) | 文件改动清单、各插入点的实际代码与 diff、helper 模块完整实现、msprobe 配置、启动参数、输出结构与关联步骤 | 动手改代码前必读 |

## 交付校验清单

- [ ] `DUMP_ON=0` 时训练行为与改动前完全一致
- [ ] `output.extra_fields["request_id"]` 在 `vLLMOmniHttpServer.generate()` 返回时存在
- [ ] `DataProto.non_tensor_batch["request_id"]` 在 `_postprocess()` 后存在
- [ ] 两侧 `step_N/rank_M/dump.json` 生成
- [ ] 至少一个 request_id 同时出现在 `dispatch_log.jsonl` 与 `update_actor_log.jsonl`
