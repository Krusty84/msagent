# PTA Profiler 自动适配实践

本文介绍如何使用 `torch-npu-profiler-adaptation` Skill，为尚未提供 PTA profiler 入口的 Ascend
PyTorch 框架补齐采集能力，并验证模型运行、数据解析和可视化链路。

## 适用范围

目标进程必须使用 PyTorch + `torch_npu` 执行 NPU 计算。纯调度进程、远程客户端或只调用其他 worker
的 API 进程不能作为 profiler 注入点。适配代码默认关闭，不改变原业务的计算、数据、返回值和并行拓扑。

## 前置检查

```bash
python -c "import torch, torch_npu; print(torch.__version__, torch_npu.__version__)"
npu-smi info
```

记录目标框架原始启动命令、退出码和一个稳定的模型输出（例如 loss、生成 token 或结果 checksum），作为
关闭 profiler 路径的回归基线。

## 自动适配步骤

### 1. 定位执行循环

```bash
python skills/ascend-profiler-collect-adaption/scripts/discover_execution_loops.py \
  /path/to/framework --json
```

脚本会按 `backward`、`optimizer.step`、`execute_model`、`generate` 等调用对 Python 循环排序。结果是候选
列表，不替代调用链确认：注入点必须在实际执行 NPU 计算的进程中。

### 2. 生成独立封装

先预览，再生成；已有不同文件不会被覆盖。

```bash
python skills/ascend-profiler-collect-adaption/scripts/scaffold_adapter.py \
  /path/to/framework --destination framework/profiler_adapter.py --dry-run
python skills/ascend-profiler-collect-adaption/scripts/scaffold_adapter.py \
  /path/to/framework --destination framework/profiler_adapter.py
```

在配置层增加 `enabled`、`output_dir`、`start_step`、`wait`、`warmup`、`active`、`repeat`、`ranks`、
`level`、`export_type` 等字段，并保持 `enabled=false`。在执行单元初始化时创建一个 controller，循环前
`start()`、每个完成的业务 step 后 `step()`、`finally` 中 `stop()`。

### 3. 验证关闭路径

用原启动命令运行一次，确认退出码和基线输出不变；同时确认没有导入 `torch_npu` profiler、没有创建
profiling 目录，也没有额外 NPU 同步。

### 4. 验证开启路径

设置独立输出目录并运行足够的 step：

```text
总 step >= start_step + (wait + warmup + active) * repeat
```

多进程任务使用 rank/worker 子目录，避免覆盖。采集结束后运行：

```bash
python skills/ascend-profiler-collect-adaption/scripts/validate_profile_output.py \
  /path/to/profile-output --expect text
```

返回码必须为 0，且 JSON 报告中 `passed` 和 `visualizable` 都为 `true`。`trace_view.json` 可导入
MindStudio Insight 或兼容 Chrome Trace Event 的查看器；DB 导出可继续交给仓库内
`ascend-profiler-db-explorer`，完整性检查可使用 `ascend-profiler-data-validation`。

完整验收固定使用 `--expect text`，因为它同时检查 trace 和 PTA 解析生成的统计 CSV。`--expect db`
只校验 SQLite 文件结构，不能据此宣称可视化链路已经通过。

## 三框架真实 NPU 冒烟

Skill 附带无模型下载的最小脚本，覆盖三种此前不依赖该 Skill 的上层框架：Transformers、Accelerate、
Diffusers。脚本只使用随机初始化的微型模型，不修改框架源码。

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh

for framework in transformers accelerate diffusers; do
  python skills/ascend-profiler-collect-adaption/scripts/run_framework_npu_smoke.py \
    "$framework" --output-dir "/tmp/profiler-auto-adaptation/$framework"
  python skills/ascend-profiler-collect-adaption/scripts/validate_profile_output.py \
    "/tmp/profiler-auto-adaptation/$framework" --expect text
done
```

每个框架均需满足：模型命令返回 0、重复 step 输出 checksum 稳定、生成非空 `trace_view.json`、解析脚本
返回 0 且 `visualizable=true`。这组冒烟是硬件集成测试；普通 CI 中的 mock 测试不能替代它。

### 本次实测结果

2026-08-20 在 Ascend 910、CANN `9.1.0-beta.3`、PyTorch `2.10.0+cpu`、torch_npu
`2.10.0.post2` 环境按上述命令完成验证。三个框架均运行 2 个 step，PTA 在 `stop()` 后完成同步解析。

| 框架版本 | 模型/执行路径 | 关闭/开启 checksum | trace/duration events | kernel CSV | 校验结果 |
| --- | --- | ---: | ---: | --- | --- |
| Transformers 5.13.0 | 随机初始化的微型 BERT 前向 | -0.0000002384 / -0.0000002384 | 1069 / 649 | 9 列 | `passed=true`, `visualizable=true` |
| Accelerate 1.12.0 | `Accelerator.prepare` 的三层 MLP 前向 | 4.5366497040 / 4.5366497040 | 179 / 97 | 9 列 | `passed=true`, `visualizable=true` |
| Diffusers 0.38.0 | 随机初始化的微型 UNet 前向 | -10.9875478745 / -10.9875478745 | 3859 / 2243 | 9 列 | `passed=true`, `visualizable=true` |

实测仅产生预期的 no-warmup 提示，没有 profiler error。为避免把大体积硬件产物提交到源码仓库，
原始 `ASCEND_PROFILER_OUTPUT` 不提交到源码仓库，运行时可按需保留；仓库中的冒烟和校验脚本可在
同等环境重新生成证据。
版本、命令、产物大小、SHA-256 和校验结果见
[`evidence/profiler-auto-adaptation-20260820.json`](evidence/profiler-auto-adaptation-20260820.json)。

## 验收记录模板

| 框架 | 原业务基线 | profiler 关闭回归 | PTA 采集 | 解析 | 可视化 | 证据目录 |
| --- | --- | --- | --- | --- | --- | --- |
| framework-a | 通过/命令 | 通过/输出 | 通过 | 通过 | 通过 | 路径或制品链接 |
| framework-b | 通过/命令 | 通过/输出 | 通过 | 通过 | 通过 | 路径或制品链接 |
| framework-c | 通过/命令 | 通过/输出 | 通过 | 通过 | 通过 | 路径或制品链接 |

提交 PR 时附上软件版本、CANN/PTA 版本、NPU 型号、完整命令、校验 JSON 和失败限制。若环境没有真实
NPU，只能标记静态/模拟测试通过，不能宣称 PTA 采集验收通过。

## 常见问题

- 没有产物：先确认 profiler 与 NPU 计算在同一进程，再检查 step 数是否覆盖 schedule。
- 只有目录或空文件：检查任务是否在采集窗口内执行了真实 NPU 算子，并检查 profiler error 日志。
- 多进程文件相互覆盖：为每个 rank/worker 设置独立 `output_dir` 或 `worker_name`。
- 服务接口返回成功但无数据：启停请求可能停留在 API/调度进程，必须转发到执行 NPU 计算的 worker。
- 原任务性能异常：确认关闭路径没有导入 profiler、创建目录或增加同步；开启 profiling 本身会产生开销，
  性能基线比较应使用关闭路径。
