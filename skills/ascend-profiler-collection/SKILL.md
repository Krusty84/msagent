---
name: ascend-profiler-collection
description: 使用 `torch_npu.profiler` 在通用训练或推理场景中采集 Ascend NPU Profiling 数据。Skill 不按训练或推理拉起方式区分，而是按框架是否已适配 `torch.profiler` 分成两条路径：已适配则整体切换到 `torch_npu.profiler`，未适配则定位真实执行路径并注入 `torch_npu.profiler`。适用于性能采集、性能分析、算子耗时排查和瓶颈定位等任务。
---

# Ascend NPU Profiling 采集指南

本 Skill 面向通用 Ascend Profiling 采集场景，核心目标是帮助用户把框架中的 profiler 适配切换或补齐到 `torch_npu.profiler`，为后续性能分析生成可用、可验证、可复现的 profiling 产物。

本 Skill 只关注两件事：

1. 框架里是否已经适配过 `torch.profiler`
2. 如果没有，应该把 `torch_npu.profiler` 注入到哪个真实执行路径

支持两类目标：

| 目标类型 | 适用场景 | 处理分支 |
|---------|----------|----------|
| 已适配 `torch.profiler` | 框架中已经存在 `torch.profiler.profile`、`schedule`、`trace_handler` 或自定义 profiler 封装 | 分支 A |
| 未适配 `torch.profiler` | 框架中还没有 profiler 适配，需要新找注入点并补齐生命周期 | 分支 B |

## 第 0 步：确认输入信息

开始采集前，先向用户收集以下信息：

> 开始 Profiling 采集前，请提供以下信息：
>
> 必要信息：
> - 框架名称、仓库路径或核心入口文件
> - 当前训练、推理或服务的执行入口

如果用户无法提供框架信息或真实执行路径，则直接停止并提示。

## 第 1 步：向用户展示确认表

收集信息后，先展示确认表，待用户确认后再执行：

| 配置项 | 值 |
|--------|----|
| 框架名称 / 入口路径 | 用户提供 |
| `torch.profiler` 搜索结果 | Skill 在代码中搜索后填写 |
| 是否已适配 `torch.profiler` | Skill 判定为 是 / 否 |
| 处理分支 | Skill 判定为 分支 A / 分支 B |
| 已有 profiler 适配点 | Skill 通过代码搜索定位 |
| 真实执行路径 | 用户提供，或通过调用链定位 |

如果分支 B 准备直接使用 Skill 预设的 `torch_npu.profiler` 默认参数，必须先展示给用户并得到明确确认后，才能写入代码。

## 第 2 步：检查环境

```bash
npu-smi info
python -c "import torch; import torch_npu; print(f'torch_npu: {torch_npu.__version__}'); print(f'NPU available: {torch.npu.is_available()}')"
```

检查失败时直接停止：

- 未检测到 NPU：提示用户切换到包含 Ascend NPU 的环境
- `torch_npu` 不可用：提示用户先安装 `torch_npu`
- 如果 `torch_npu` 已安装但环境变量未生效

## 第 3 步：创建 Profiling 并执行

禁止直接修改用户原始脚本或框架源码。应优先通过复制文件、patch、hook、plugin 或可回滚配置方式完成适配。

### 3.0 判断分支

必须先在框架代码中搜索 `torch.profiler` 相关调用，再决定走哪一条分支。

```bash
if rg -n "torch\.profiler|from torch\.profiler import|profile\(" "{REPO_OR_ENTRY}" >/dev/null 2>&1; then
    PROFILER_BRANCH="existing_torch_profiler"
else
    PROFILER_BRANCH="new_integration"
fi
```

判定线索：

- 已存在 `import torch.profiler`
- 已存在 `torch.profiler.profile(...)`、`schedule(...)`、`tensorboard_trace_handler(...)`
- 已存在 profiler wrapper、callback、plugin 或统一 profiling 开关
- 已存在按 step/request 推进 profiler 生命周期的封装

只要框架里已经有一套稳定的 `torch.profiler` 生命周期和接入点，就走分支 A；否则走分支 B。

## 分支 A：框架已适配 `torch.profiler`

### A1. 定位已有适配点

优先定位以下位置：

1. `import torch.profiler` 或 `from torch.profiler import ...`
2. `torch.profiler.profile(...)`、`schedule(...)`、`tensorboard_trace_handler(...)`
3. profiler wrapper、callback、context manager、plugin 或统一 profiling 开关
4. 与 `prof.start()`、`prof.step()`、`prof.stop()` 语义等价的封装层

如果已经有统一 profiler 抽象层，优先在抽象层内完成切换，而不是把 `torch_npu.profiler` API 散落到业务代码里。

### A2. 切换原则

把原先所有 `torch.profiler` 相关接口整体切换成 `torch_npu.profiler`：

| 原接口或概念 | 切换后 |
|--------------|--------|
| `torch.profiler.profile` | `torch_npu.profiler.profile` |
| `torch.profiler.schedule` | `torch_npu.profiler.schedule` |
| `torch.profiler.ProfilerActivity.CPU` | `torch_npu.profiler.ProfilerActivity.CPU` |
| `torch.profiler.ProfilerActivity.CUDA` | `torch_npu.profiler.ProfilerActivity.NPU` |
| `torch.profiler.tensorboard_trace_handler` | `torch_npu.profiler.tensorboard_trace_handler` |
| 原 `profile(...)` 参数 | 尽量保持原参数不变，只补齐 `experimental_config` 并校正 activities |

同时补齐：

- `experimental_config = torch_npu.profiler._ExperimentalConfig(...)`
- Ascend 侧必需参数与枚举
- Ascend 侧默认验证口径

### A3. 保留原生命周期，不重造流程

分支 A 的重点不是重新设计训练或推理流程，而是复用框架原有 profiler 生命周期：

- 原来在哪里创建 profiler，就继续在那里创建
- 原来在哪里 `start / step / stop`，就继续保持同样的语义位置
- 原来如果已经区分 step、request、micro-step、rollout iteration，也继续沿用原语义
- 原来如果已经通过配置、callback、wrapper 控制 profiler 开关，也继续复用原控制面

常见生命周期示例：

训练循环：

```python
prof.start()
for step in range(total_steps):
    # 原始训练逻辑
    prof.step()
prof.stop()
```

单次推理：

```python
prof.start()
# 原始推理逻辑
prof.stop()
```

指定代码段：

```python
prof.start()
# 目标代码段
prof.stop()
```

### A4. 执行

保持原有启动方式不变。Skill 只负责把原有 `torch.profiler` 适配切换成 `torch_npu.profiler`，不负责改造训练或推理拉起方式。

---

## 分支 B：框架未适配 `torch.profiler`

### B1. `torch_npu.profiler` 默认骨架

分支 B 需要由 Skill 提供一套默认 `torch_npu.profiler` 骨架，再结合框架真实执行路径完成注入。

```python
import torch
import torch_npu

experimental_config = torch_npu.profiler._ExperimentalConfig(
    aic_metrics=torch_npu.profiler.AiCMetrics.AiCoreNone,
    profiler_level=torch_npu.profiler.ProfilerLevel.Level0,
)

prof = torch_npu.profiler.profile(
    activities=[
        torch_npu.profiler.ProfilerActivity.CPU,
        torch_npu.profiler.ProfilerActivity.NPU
        ],
    schedule=torch_npu.profiler.schedule(wait=0, warmup=0, active=1, repeat=1, skip_first=1),
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("./result"),
    record_shapes=False,
    profile_memory=False,
    with_stack=False,
    with_modules=False,
    with_flops=False,
    experimental_config=experimental_config
)
```

注意：

- 这套骨架只用于分支 B
- 上述默认参数只能在用户确认后使用
- `on_trace_ready` 默认统一使用 `torch_npu.profiler.tensorboard_trace_handler("./result")`
- 不要照搬 CUDA 专用配置或通用 `torch.profiler` 假设，必须显式切换到 `torch_npu.profiler`

### B2. 先给出默认参数提案，并等待用户确认

在分支 B 中，如果用户没有给出明确参数，先向用户展示默认参数提案：

| 配置项 | 默认值                  |
|--------|----------------------|
| 采集级别 | `Level1`             |
| `activities` | `CPU + NPU`          |
| `wait` | `0`                  |
| `warmup` | `0`                  |
| `active` | `1`                  |
| `repeat` | `1`                  |
| `skip_first` | `1`                  |
| `record_shapes` | `False`               |
| `profile_memory` | `False`              |
| `with_stack` | `False`              |
| 输出目录 | `./result` |

只有用户明确确认后，才能把这些默认参数落到代码中。

### B3. 统一定位注入点

注入点定位直接参考对应场景的参考文档：

- 推理服务框架场景：参考 [references/inference-framework-profiling-reference.md](references/inference-framework-profiling-reference.md)
- 强化学习训练场景：参考 [references/rl-framework-profiler-integration-reference.md](references/rl-framework-profiler-integration-reference.md)

### B4. 按执行语义选择落点

如果是标准训练循环，优先把 `step()` 对齐到稳定的训练 step、mini-batch 或 micro-step。

如果是单次推理、服务请求、离线 generate 或评测样本推理，优先使用单次请求级 `start/stop` 包裹真实执行路径。

如果是强化学习、rollout、actor/critic、worker/executor 场景：

- 主控层适合控制 `start/stop`
- 执行层适合推进 `step()`
- 不要把 profiler 只挂在控制层，导致真实执行角色没有产物

### B5. 创建 Profiling

分支 B 的典型做法：

1. 复制原始可编辑文件，或通过 patch、plugin、hook、wrapper 注入 `torch_npu.profiler`
2. 把 `start / step / stop` 放到前面确定好的真实执行位置
3. 保持原启动命令和原入口不变，只让执行流指向新的 profiling 代码

如果只有启动脚本可改，启动脚本只负责导向 profiling 版本，不作为分支判断依据。

### B6. 执行

```bash
{ORIGINAL_LAUNCH_COMMAND}
```

---

## 第 4 步：产物验证

```bash
ls {OUTPUT_DIR}/*/ASCEND_PROFILER_OUTPUT/trace_view.json 2>/dev/null
ls {OUTPUT_DIR}/*/ASCEND_PROFILER_OUTPUT/op_statistic.csv 2>/dev/null
ls {OUTPUT_DIR}/*/ASCEND_PROFILER_OUTPUT/kernel_details.csv 2>/dev/null
```

典型输出结构：

```text
{OUTPUT_DIR}/{container_id}_{pid}_{timestamp}_ascend_pt/
├── ASCEND_PROFILER_OUTPUT/
│   ├── trace_view.json
│   ├── op_statistic.csv
│   ├── operator_details.csv
│   ├── kernel_details.csv
│   ├── step_trace_time.csv
│   └── ascend_pytorch_profiler_0.db
├── FRAMEWORK/
└── PROF_*/
```

### 4.1 检查是否成功落盘

- 确认输出目录已生成
- 确认关键文件存在，且时间对应本次采集
- 多进程或多卡场景下，确认输出文件数量与预期角色、rank 数量基本一致

### 4.2 检查采集区间是否正确

- 确认采到的是目标训练或推理阶段，而不是只采到初始化、加载、保存或 warmup
- 确认 `start` 和 `stop` 已包住真实执行区间
- 确认 `step` 推进次数与预期训练步数、mini-batch 数或请求次数大致一致

### 4.3 检查数据是否可分析

- 确认后续分析工具可以正常打开产物
- 确认 trace 中能看到目标阶段的算子、通信或时间线活动
- 确认文件未损坏、未截断，也不是只生成了空目录

### 4.4 向用户汇报

> Profiling 采集完成  
> 输出目录：`{OUTPUT_DIR}/xxx_ascend_pt/`  
> 采集配置：`Level{LEVEL}`，`{ACTIVE}` steps，`CPU={CPU}`，`Memory={MEMORY}`  
> 建议下一步：进入 profiling-analysis 类 Skill 做性能分析

## 第 5 步：故障排查

| 症状 | 处理方式 |
|------|----------|
| 没有生成 trace 文件 | 检查 profiler 是否启用、`prof.stop()` 是否执行、路径是否可写、NPU 是否可用 |
| 有目录但没有有效产物 | 检查注入点是否真的发生设备计算，检查真实请求或真实训练是否已触发 |
| `active` 太小 | 提示用户增加步数 |
| `NPU out of memory` | 降低 batch size，或更换设备 |
| `Profiler already started` | 检查是否重复调用 `prof.start()` |
| 推理服务没有产物 | 检查环境变量是否传到 worker、注入点是否真的被调用、真实请求是否进入执行进程 |
| 强化学习框架只有部分角色有产物 | 检查 profiler 是否只挂在控制层，确认 actor、critic、rollout、worker 是否都绑定到真实执行路径 |
| 只采到 warmup 没采到真实请求 | 增大 `skip_first`，并确认采集窗口覆盖真实执行阶段 |
| 修改后脚本或服务起不来 | 回滚备份文件，重新检查语法和注入位置 |
| 杀进程后显存不释放 | Ascend HBM 释放可能滞后，重新确认设备占用情况 |

## 第 6 步：恢复

这一节用于恢复 profiling 适配过程中引入的临时改动：

```bash
cp target.py.backup_<timestamp> target.py
```

恢复改动时重点检查：

- 移除临时注入的 profiler 代码
- 恢复被改写的 `.py`、`.sh` 或框架源码
- 如果通过 patch、plugin、hook 接入，恢复原始配置或关闭开关
- 再次启动服务或训练命令，确认恢复后可以正常运行

## 禁止事项

- 禁止直接修改用户原始脚本
- 禁止在循环内部反复调用 `prof.start()` / `prof.stop()`
- 禁止设置 `active=0`
- 禁止在无脚本、无框架信息时继续执行

## 参考文档

- [references/torch-npu-profiler-config-reference.md](references/torch-npu-profiler-config-reference.md)
- [references/inference-framework-profiling-reference.md](references/inference-framework-profiling-reference.md)
- [references/rl-framework-profiler-integration-reference.md](references/rl-framework-profiler-integration-reference.md)
- [Ascend PyTorch Profiler 官方文档](https://www.hiascend.com/document/detail/zh/mindstudio/830/T&ITools/Profiling/atlasprofiling_16_0121.html)
- [profiling-analysis 系列 Skill](../profiling-analysis/SKILL.md)
