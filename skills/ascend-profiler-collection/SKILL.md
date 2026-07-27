---
name: ascend-profiler-collection
description: 使用 `torch_npu.profiler` 在非 MindSpeed-LLM、非 MindSpeed-MM 的通用训练或推理场景中采集 Ascend NPU Profiling 数据。覆盖 `level0`、`level1`、`level2` 采集级别，支持 `.py`、`.sh`、推理服务框架、强化学习训练框架四类入口，适用于性能采集、性能分析、算子耗时排查和瓶颈定位等任务。
---

# Ascend NPU Profiling 采集指南

本 Skill 面向通用 Ascend Profiling 采集场景，核心目标是基于 `torch_npu.profiler` 为后续性能分析生成可用、可验证、可复现的 profiling 产物。

支持四类目标：

| 目标类型 | 适用场景 | 处理分支 |
|---------|----------|----------|
| 训练或推理脚本（`.py`） | 用户已有可直接运行的 Python 脚本 | 分支 A |
| Shell 启动脚本（`.sh`） | `deepspeed`、`torchrun`、多机拉起脚本等 | 分支 B |
| 推理服务框架 | 框架以服务形式运行，用户没有可直接编辑的训练或推理脚本 | 分支 C |
| 强化学习训练框架 | 框架包含 `trainer`、`actor`、`critic`、`rollout`、`ref` 等角色，训练链路与生成链路拆分 | 分支 D |

## 第 0 步：确认输入信息

开始采集前，先向用户收集以下信息：

> 开始 Profiling 采集前，请提供以下信息：
>
> 必要信息：
> - 训练或推理脚本路径：`.py` 或 `.sh`
> - 推理服务框架信息：框架名称 + 模型路径 + 启动命令
> - 强化学习训练框架信息：框架名称 + 训练入口 + rollout 后端信息（如有）
>
> 可选信息（均有默认值）：
> - 采集级别：`level0` / `level1` / `level2`，默认 `level1`
> - 采集步数 `active`：默认 `3`
> - 是否采集 CPU：默认开启
> - 是否采集内存：默认关闭
> - 是否记录 Tensor Shape：默认开启
> - 是否采集堆栈 `with_stack`：默认关闭
> - 输出目录：默认 `./profiling_result`
> - 采集方式：训练循环 / 单次推理 / 指定代码段

如果用户无法提供脚本路径、推理框架信息或强化学习框架信息，则直接停止并提示：

> Profiling 采集需要可运行的脚本，或足够明确的推理框架 / 强化学习框架信息。请先补齐这些信息，再继续采集。

### 用户输入映射

| 用户表述 | 映射结果 |
|---------|----------|
| “采 5 步” / `active=5` | `active=5` |
| `level0` / “只看算子” | `profiler_level=Level0` |
| `level1` | `profiler_level=Level1` |
| `level2` / “全量采集” | `profiler_level=Level2` |
| “采集 CPU” | `activities` 包含 CPU |
| “不要 CPU” / `npu only` | `activities` 仅包含 NPU |
| “采内存” / `memory` | `profile_memory=True` |
| “采 stack” / `with_stack` | `with_stack=True` |
| “不要 shape” | `record_shapes=False` |
| “输出到 XXX” | `OUTPUT_DIR=XXX` |
| “只有推理，没有训练循环” | 走单次推理模式 |
| “只采某个函数 / 某段代码” | 使用 `start/stop` 包裹指定代码段 |
| “服务框架 / 推理引擎 / serve” | 走分支 C |
| “强化学习框架” | 走分支 D |

## 第 1 步：向用户展示确认表

收集信息后，先展示确认表，待用户确认后再执行：

| 配置项 | 值 |
|--------|----|
| 脚本路径 / 框架名称 | 用户提供 |
| 目标类型 | `.py` / `.sh` / 推理服务框架 / 强化学习训练框架 |
| 采集级别 | `Level1`（默认） |
| 采集步数 `active` | `3`（默认） |
| 采集方式 | 训练循环 / 单次推理 / 指定代码段 / 服务推理 / 强化学习阶段采集 |
| CPU 采集 | 开启 |
| 内存采集 | 关闭 |
| 堆栈采集 | 关闭 |
| Tensor Shape | 开启 |
| 输出目录 | `./profiling_result` |

## 第 2 步：检查环境

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null || source /usr/local/Ascend/cann/set_env.sh 2>/dev/null
npu-smi info
python -c "import torch; import torch_npu; print(f'torch_npu: {torch_npu.__version__}'); print(f'NPU available: {torch.npu.is_available()}')"
```

检查失败时直接停止：

- 未检测到 NPU：提示用户切换到包含 Ascend NPU 的环境
- `torch_npu` 不可用：提示用户先安装 `torch_npu`

## 第 3 步：创建 Profiling 版本并执行

禁止直接修改用户原始脚本。对于 `.py` 和 `.sh`，必须创建带时间戳的新文件。

### 3.0 判断目标类型

```bash
TARGET="{USER_INPUT}"
if [[ "$TARGET" == *.py ]]; then
    TARGET_TYPE="py"
elif [[ "$TARGET" == *.sh ]]; then
    TARGET_TYPE="sh"
else
    TARGET_TYPE="framework"
fi
```

- `.py`：走分支 A
- `.sh`：走分支 B
- 推理服务框架：走分支 C
- 强化学习训练框架：走分支 D

如果 `TARGET_TYPE="framework"`，继续按框架特征区分：

```bash
if [[ "{FRAMEWORK_HINT}" =~ (PPO|GRPO|DPO|RLHF|trainer|actor|critic|rollout|ref) ]]; then
    FRAMEWORK_BRANCH="rl"
else
    FRAMEWORK_BRANCH="inference"
fi
```

---

## 公共 `torch_npu.profiler` 骨架

分支 A、B、C、D 统一复用同一套 `torch_npu.profiler` 构造骨架。

```python
import torch
import torch_npu

experimental_config = torch_npu.profiler._ExperimentalConfig(
    aic_metrics=torch_npu.profiler.AiCMetrics.AiCoreNone,
    profiler_level=torch_npu.profiler.ProfilerLevel.{LEVEL},
    mstx=False,
    l2_cache=False,
    op_attr=False,
    data_simplification=False,
    record_op_args=False,
    gc_detect_threshold=None,
    host_sys=[],
    sys_io=False,
    sys_interconnection=False,
)

prof = torch_npu.profiler.profile(
    activities=[{ACTIVITIES}],
    schedule=torch_npu.profiler.schedule(wait=0, warmup=0, active={ACTIVE}, repeat=1, skip_first=1),
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("{OUTPUT_DIR}"),
    record_shapes={RECORD_SHAPES},
    profile_memory={PROFILE_MEMORY},
    with_stack={WITH_STACK},
    with_modules=False,
    with_flops=False,
    experimental_config=experimental_config,
)
```

注意：

- 不要使用 `skip_first_wait`
- 默认统一使用 `skip_first`
- A/B 可直接在脚本中落地该骨架
- C/D 复用该骨架，但必须按真实执行路径决定注入点，以及 `start / step / stop` 的放置位置

## 分支 A：用户提供 `.py` 脚本

### A1. 注入模板

分支 A 直接复用上面的公共 `torch_npu.profiler` 骨架，在可编辑的 `.py` 脚本中实例化 profiler，不再单独维护第二份模板。

### A2. 三种采集方式

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

### A3. 执行

```bash
python {PROFILING_SCRIPT_PATH}
```

---

## 分支 B：用户提供 `.sh` 启动脚本

### B1. 复制 Shell 脚本

```bash
TIMESTAMP=$(date +%Y%m%d%H%M%S)
SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
SCRIPT_BASENAME=$(basename "$SCRIPT_PATH" .sh)

NEW_SH="${SCRIPT_DIR}/${SCRIPT_BASENAME}_profiling_${TIMESTAMP}.sh"
cp "$SCRIPT_PATH" "$NEW_SH"
```

### B2. 提取实际 Python 入口

优先级如下：

1. `--module`
2. `python` / `torchrun` / `deepspeed` 后的 `.py`

如果无法提取出有效的 Python 入口，则停止并报错。

### B3. 生成 Profiling 版 Python 脚本

复用上面的公共 `torch_npu.profiler` 骨架，生成新的 `.py` 文件。

### B4. 改写新的 Shell 脚本

把 `NEW_SH` 中原始 Python 入口替换为 Profiling 版 Python 入口。

### B5. 执行

```bash
bash "$NEW_SH"
```

---

## 分支 C：推理服务框架

分支 C 不在主 `SKILL.md` 中展开全部细节，以避免主文件过长。

处理这类任务时，按以下顺序执行：

1. 定位框架的真实模型执行路径，而不是只停留在启动命令、服务入口或调度层
2. 复用上面的公共 `torch_npu.profiler` 骨架，在真实设备计算边界或紧贴执行前后的最薄包装层注入 profiler
3. 确保 profiler 配置、环境变量或开关能够传递到真实执行请求的进程
4. 启动服务后发送真实推理请求或离线推理任务，触发目标执行路径
5. 通用的产物验证、故障排查和恢复流程，统一参考主文档第 4 至第 6 步

详细流程、定位方法、多进程环境变量传递、触发方式和恢复步骤，参考：

- [references/inference-framework-profiling-reference.md](references/inference-framework-profiling-reference.md)

---

## 分支 D：强化学习训练框架

分支 D 不在主 `SKILL.md` 中展开全部细节，以避免主文件过长。

处理这类任务时，按以下顺序执行：

1. 拆清训练主控层、采样或生成层、参数更新层，以及可选辅助模型或评估层的职责边界
2. 识别各阶段中的最小重复执行单元，例如训练 step、mini-batch、micro-step、采样轮次或解码步
3. 复用上面的公共 `torch_npu.profiler` 骨架，优先在真实计算边界注入 profiler，而不是只包外层控制循环
4. 如果采样或生成阶段复用了外部推理后端，优先做 profiler 配置桥接或透传
5. 通用的产物验证、故障排查和恢复流程，统一参考主文档第 4 至第 6 步
6. 采集完成后恢复原文件，或移除临时插桩

详细流程、注入位置、配置透传、阶段触发、验证与排障，参考：

- [references/rl-framework-profiler-integration-reference.md](references/rl-framework-profiler-integration-reference.md)

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
| 使用 `skip_first_wait` 报错 | 删除该参数，改用 `skip_first` |
| 推理服务没有产物 | 检查环境变量是否传到 worker、注入点是否真的被调用、真实请求是否进入执行进程 |
| 强化学习框架只有部分角色有产物 | 检查 profiler 是否只挂在控制层，确认 actor、critic、rollout、worker 是否都绑定到真实执行路径 |
| 只采到 warmup 没采到真实请求 | 增大 `skip_first`，并确认采集窗口覆盖真实执行阶段 |
| 修改后脚本或服务起不来 | 回滚备份文件，重新检查语法和注入位置 |
| 杀进程后显存不释放 | Ascend HBM 释放可能滞后，重新确认设备占用情况 |

## 第 6 步：恢复

采集完成后，必须恢复原文件或清理临时改动：

```bash
cp target.py.backup_<timestamp> target.py
```

恢复时重点检查：

- 移除临时注入的 profiler 代码
- 恢复被改写的 `.py`、`.sh` 或框架源码
- 如果通过 patch、plugin、hook 接入，恢复原始配置或关闭开关
- 再次启动服务或训练命令，确认恢复后可以正常运行

## 禁止事项

- 禁止直接修改用户原始脚本
- 禁止在循环内部反复调用 `prof.start()` / `prof.stop()`
- 禁止设置 `active=0`
- 禁止在无脚本、无框架信息时继续执行
- 禁止把 MindSpeed-LLM 的参数体系直接套用到本 Skill
- 禁止忘记恢复框架源码中的临时修改
- 禁止使用 `skip_first_wait`

## 参考文档

- [references/torch-npu-profiler-config-reference.md](references/torch-npu-profiler-config-reference.md)
- [references/inference-framework-profiling-reference.md](references/inference-framework-profiling-reference.md)
- [references/rl-framework-profiler-integration-reference.md](references/rl-framework-profiler-integration-reference.md)
- [Ascend PyTorch Profiler 官方文档](https://www.hiascend.com/document/detail/zh/mindstudio/830/T&ITools/Profiling/atlasprofiling_16_0121.html)
- [profiling-analysis 系列 Skill](../profiling-analysis/SKILL.md)
