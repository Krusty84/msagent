# 使用 msAgent 自动适配 PTA Profiler

本文介绍如何让 msAgent 加载 `torch-npu-profiler-adaptation` Skill，自动分析尚未提供 PTA profiler
入口的 Ascend PyTorch 训推工程，生成最小接线改动，并在真实 NPU 上验证模型、采集、解析和可视化链路。

msAgent 的交互命令和 Skill 加载方式见[《msAgent 使用指南》](../user_guide/usemap.md)与
[`skills/README.md`](../../../skills/README.md)。

## 完成标准

“自动适配”要求 msAgent 实际完成以下工作：

1. 读取目标仓库并记录未修改基线；
2. 定位实际执行 NPU forward、backward 或 generate 的 worker/循环；
3. 生成独立 `profiler_adapter.py`，修改目标框架配置和执行入口；
4. 运行 profiler 关闭和开启路径，确认模型结果等价；
5. 对每个独立采集会话验证 NPU trace 和解析后的 kernel CSV；
6. 保存 msAgent prompt、源码 diff、命令和校验结果。

只输出建议、只复制封装、或用统一外层 harness 包裹多个库的 workload，不能算框架自动适配。

## 环境准备

确认 NPU 和 PTA 环境：

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
python -c "import torch, torch_npu; print(torch.__version__, torch_npu.__version__)"
npu-smi info
```

在 msAgent 工作目录的 `.msagent/config.llms.yml` 配置模型，API Key 只通过环境变量提供，不能写入 Skill、
日志、Git diff 或长期记忆。本次真实测试使用：

```text
Agent: Hermes
Model: DeepSeek-V4-Flash-0731
Provider: Gitee AI OpenAI-compatible API
```

## 安装并调用 Skill

进入 msAgent 交互会话，安装当前仓库中的 Skill：

```text
/add-skill /path/to/msagent/skills/ascend-profiler-collect-adaption
/skills torch-npu-profiler-adaptation
```

也可以在已启用该 Skill 的 Agent 下使用 one-shot 模式：

```bash
msagent "调用 torch-npu-profiler-adaptation skill，自动适配 /path/to/framework，运行关闭/开启回归并验证真实 PTA 产物" \
  -w /path/to/msagent-working-dir -a Hermes -m default --stream
```

推荐 prompt 明确给出目标仓库、原生启动命令、Python 环境、期望配置入口和禁止修改的目录。例如：

```text
调用 torch-npu-profiler-adaptation skill，自动适配 <project>。
必须实际读取和修改项目，先记录基线，再运行 discover 和 scaffold。
profiler 默认关闭；关闭和开启路径必须运行同一个原生入口并比较关键结果。
开启路径使用真实 Ascend NPU，Text 导出，最后运行严格 validator。
只修改 <project>，不要修改其他仓库。
```

## Skill 的适配流程

### 1. 建立基线

记录原始 commit、启动命令、退出码和稳定结果，如 loss、生成 token 或 checksum。目标工程最好放在独立 Git
工作树中，便于保存 msAgent 生成的精确 diff。

Profiler 关闭路径必须满足：

- 不导入 `torch_npu.profiler`；
- 不创建 profiler 输出目录；
- 不增加 NPU 同步点；
- 业务结果与未修改基线一致。

### 2. 定位执行单元

```bash
python skills/ascend-profiler-collect-adaption/scripts/discover_execution_loops.py \
  /path/to/framework --json
```

输出包含 `confidence` 和 evidence。Agent 仍需检查调用链和进程边界：

- 显式训练/推理循环：在完整业务 step 结束后调用 `controller.step()`；
- Trainer/Engine：优先使用 optimizer/global-step callback，不能按 epoch 或 micro-batch 错位计步；
- vLLM/SGLang 类服务：HTTP/API 层只转发启停请求，controller 必须位于真正执行 NPU 的 worker；
- RL：actor、rollout、ref 等 role/stage 分开控制和落盘。

发现结果为空时，Agent 应继续检查 callback、hook、`execute_model` 或 `generate` 入口，而不是猜测注入点。

### 3. 生成通用封装

```bash
python skills/ascend-profiler-collect-adaption/scripts/scaffold_adapter.py \
  /path/to/framework --destination framework/profiler_adapter.py --dry-run
python skills/ascend-profiler-collect-adaption/scripts/scaffold_adapter.py \
  /path/to/framework --destination framework/profiler_adapter.py
```

脚本不会覆盖内容不同的已有文件。封装支持：

- 严格解析 YAML、CLI、环境变量传入的 bool/int/rank；
- 默认关闭和 `torch_npu` 延迟导入；
- 线程安全、进程内单活动会话；
- 同一 controller 多次 start/stop，适用于服务和 RL 阶段；
- 从标准分布式环境变量推断 global rank，多进程 rank 不明确时拒绝启动；
- 默认 worker 名 `rank_<rank>_pid_<pid>`，避免多 worker 覆盖。

### 4. 接线与 step 预算

显式循环范式：

```python
config = ProfilerConfig.from_mapping(profiler_options)
validate_step_budget(config, total_steps)
controller = ProfilerController(config)

with controller:
    for batch in batches:
        result = run_one_business_step(batch)
        controller.step()
```

总 step 必须满足：

```text
total_steps >= start_step + (wait + warmup + active) * repeat
```

训练 callback、服务 worker 和 RL 分阶段模式分别参见 Skill 的 `references/`。配置必须从原入口完整透传，
不能把 profiler 参数散落到业务文件中。

每次回归使用新的唯一输出目录，并先确认它尚不存在。不使用 `rm -rf`或其他递归删除命令清理
旧产物，也不覆盖旧采集目录。

### 5. 严格验收产物

```bash
python skills/ascend-profiler-collect-adaption/scripts/validate_profile_output.py \
  /path/to/profile-root --expect text --expected-sessions 1 --expected-ranks 0
```

校验器按每个 `ASCEND_PROFILER_OUTPUT` 会话独立检查，禁止把不同 worker 的文件拼成一次成功。Text 验收要求：

- `trace_view.json` 是有效 Chrome Trace Event 数据；
- trace 中存在真实 NPU/CANN duration 或配对 async 事件；
- 同一会话存在符合 PTA schema 的 `kernel_details.csv`；
- kernel 行包含名称、设备 ID 和合法 duration；
- `passed`、`parsed_output_ready`、`visualizable` 均为 `true`。

多 worker/rank 任务增加 `--expected-workers` 或 `--expected-ranks`。`--expect db` 只能证明数据库可读取，
不能替代完整可视化验收。

生成的 `trace_view.json` 可导入 MindStudio Insight 或兼容 Chrome Trace Event 的查看器。可视化检查应确认
时间线存在 NPU kernel，并能与 `kernel_details.csv` 中的设备、名称和耗时对应。

## 三框架真实自动适配

仓库提供三个未预埋 profiler 的独立原生入口：

```text
tests/skills/ascend_profiler_collect_adaption/fixtures/unadapted_frameworks/
├── transformers_trainer/run.py
├── accelerate_training/run.py
├── diffusers_inference/run.py
└── multiprocess_inference_service/run.py
```

测试不是共享外层 harness：msAgent 分别加载 Skill、运行发现和脚手架、修改三个独立 Git 工程，然后分别
运行其原生入口：

- Transformers：`Trainer.train()` 两步训练，通过 `TrainerCallback.on_optimizer_step` 计步；
- Accelerate：`Accelerator.prepare/backward` 与显式 optimizer 循环；
- Diffusers：`UNet2DModel` + `DDPMScheduler` 两步 denoising 推理。

2026-08-20 在 Ascend 910、CANN `9.1.0-beta.3`、torch_npu `2.10.0.post2` 上的结果：

| 框架 | 原生路径 | 基线/关闭/开启结果 | NPU async pairs | Kernel rows | 严格校验 |
| --- | --- | --- | ---: | ---: | --- |
| Transformers 5.13.0 | Trainer optimizer callback | 完全一致 | 42 | 45 | 通过 |
| Accelerate 1.12.0 | backward/optimizer loop | 完全一致 | 15 | 15 | 通过 |
| Diffusers 0.38.0 | scheduler denoising loop | 完全一致 | 163 | 296 | 通过 |

每个关闭路径均未创建 profiler 目录；每个开启路径均完成 PTA 同步解析，并得到独立
`ASCEND_PROFILER_OUTPUT`。msAgent 生成的三个源码 patch、产物大小、SHA-256 和 validator 结果见
[`evidence/profiler-auto-adaptation-20260820.json`](evidence/profiler-auto-adaptation-20260820.json)。

为避免用“指定注入点”的 prompt 代替自动决策，另外从干净仓库使用上文的一句通用 prompt 做了独立
前向测试。msAgent 在 Transformers 工程中自行判断本地无训练循环、转而检查 Trainer callback；它完成
真实采集后又通过 msprof MCP 解析 168 个 kernel，并在 trace 中定位到 `aclnnApplyAdamWV2` 时间线事件。
在 Accelerate 干净仓库中，msAgent 通过 discover 自行定位 backward/optimizer 内循环，生成默认关闭的
CLI 配置和 controller 接线；安全开启复测得到 15 对 NPU async event 和 15 行 kernel，三路业务结果完全一致。
在 Diffusers 干净仓库中，msAgent 自行识别 `scheduler.timesteps` 有限推理循环，保留
`torch.no_grad()` 并在 `scheduler.step()` 后推进 profiler；安全复测得到 163 对 NPU async event 和 294 行 kernel。
同一通用 prompt 还适配了一个 `multiprocessing` 推理服务：controller 位于真正持有 NPU 模型的子进程，
父进程只跨队列传递配置和推理请求；两次请求产生 2 对 NPU async event 和 2 行 kernel 数据，严格校验
全部通过，且基线、关闭、开启输出完全一致。完整决策和数值记录在同一 evidence JSON 中。

在 adapter 的 rank、停止失败恢复和 validator 规则加固后，三个框架均使用新 adapter 重新运行；每个框架
再次满足 `baseline == disabled == enabled`，且新 validator 对新产物返回
`passed=true, parsed_output_ready=true, visualizable=true`。

## 完整 vLLM/vLLM-Ascend 真实工程验证

2026-08-22 另外在完整 vLLM 与 vLLM-Ascend 源码、真实 `vllm.LLM` EngineCore 上执行了
Qwen3-0.6B 离线推理。该测试使用仓库中的
[`full_frameworks/vllm/run_vllm_profile.py`](../../../tests/skills/ascend_profiler_collect_adaption/full_frameworks/vllm/run_vllm_profile.py)
可复现入口，没有修改 vLLM、vLLM-Ascend 或 vllm-omni 源码。

源码与环境：

- vLLM commit `e5588e49bc2642670116664a7fc4096e27adb179`；
- vLLM-Ascend commit `ccc0a3f1c9c6cc36b5ac38274bebf8e82019be05`；
- Ascend 910、CANN `9.1.0-beta.3`、torch_npu `2.10.0.post2`；
- Qwen3-0.6B snapshot `c1899de289a04d12100db370d81485cdf75e47ca`，权重 SHA-256
  `f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b`。

复现时为 baseline 和 profiled 路径分别创建新目录，不删除任何旧产物：

```bash
ASCEND_RT_VISIBLE_DEVICES=0 VLLM_PLUGINS=ascend <venv>/bin/python \
  tests/skills/ascend_profiler_collect_adaption/full_frameworks/vllm/run_vllm_profile.py \
  --model <Qwen3-0.6B> --result <new-baseline-dir>/result.json

ASCEND_RT_VISIBLE_DEVICES=0 VLLM_PLUGINS=ascend <venv>/bin/python \
  tests/skills/ascend_profiler_collect_adaption/full_frameworks/vllm/run_vllm_profile.py \
  --model <Qwen3-0.6B> --result <new-profiled-dir>/result.json \
  --profile-dir <new-profiled-dir>/profile

python skills/ascend-profiler-collect-adaption/scripts/validate_profile_output.py \
  <new-profiled-dir>/profile --expect text --expected-sessions 1 --expected-ranks 0
```

两条路径都先完成一次 warmup，再以相同 seed 和确定性采样执行两条请求。生成文本、32 个
token ID 与 finish reason 逐字节一致。开启路径生成一个 rank 0 PTA 会话：

| 指标 | 结果 |
| --- | ---: |
| Trace events | 203470 |
| Duration events | 154630 |
| NPU async pairs | 8266 |
| Valid kernel rows | 7493 |
| Trace size | 40867915 bytes |
| Profiler DB tables | 21 |

严格校验返回 `passed=true`、`text_ready=true`、`db_ready=true`、
`parsed_output_ready=true`、`visualizable=true`。完整环境、模型、命令、产物大小和 SHA-256 见同一
[evidence JSON](evidence/profiler-auto-adaptation-20260820.json)。

这项证据验证了完整生产级框架的模型加载、调度、推理、PTA 采集、同步解析和可视化产物链路。
但该 vLLM-Ascend 版本已自带 PTA profiler wrapper，因此它只记为“完整框架集成验证”，不冒充前文
三个“未适配框架的自动改码”证据。本次未声称在线服务、多 rank、长时稳定性或 profiler 性能开销已验证。

最终脚本的首次 profiled 尝试在 profiler 开始前的 EngineCore KV-cache 初始化阶段失败，报错为
`No available memory for the cache blocks`。该失败目录与日志被保留，不计为 profiler 成功。确认
`npu-smi` 无残留 NPU 进程后，在新目录以完全相同命令和配置重试通过。这说明单次成功不能替代
长时稳定性验收，后续若将它升级为生产稳定性证据，必须增加多轮启停和 soak 测试。

附带的 `run_framework_npu_smoke.py` 只用于底层库兼容性快速检查，不能替代上述 msAgent 自动适配验收。

## 常见问题

- 无候选：检查框架 callback/hook、服务 worker 或 generate/execute_model 调用链。
- 有 trace 但校验失败：检查 trace 是否包含 NPU/CANN category，并确认同一会话生成了有效 kernel CSV。
- 多 worker 缺产物：使用 `--expected-workers`/`--expected-ranks`，并检查启停请求是否到达 NPU worker。
- rank 配置失败：显式传 global rank，或提供 `RANK`、OpenMPI、PMI、SLURM 等标准环境变量。
- 服务第二次启动失败：确保使用当前可重复会话 controller，并在上一次 stop 完成后再 start。
- 原任务变慢：只比较 profiler 关闭路径的性能；开启 profiler 本身会产生采集和解析开销。
