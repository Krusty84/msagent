---
name: ascend-profiler-collect-adaption
description: >
  Automatically adapt torch_npu.profiler collection to an Ascend PyTorch training, inference,
  or reinforcement-learning framework while preserving its behavior. Use when a framework has no
  usable PTA profiler entry point and the result must be collected, parsed, and visualizable.
---

# torch_npu.profiler 自动适配

为目标框架补齐默认关闭、可配置、可验证的 PTA profiler 能力。完成标准不是“代码中出现
`profile()`”，而是原业务仍能运行，启用后能够生成可解析且可视化的 profiling 产物。

## 自动适配的定义

本 Skill 被 msAgent 加载后，Agent 必须实际读取目标仓库、定位 NPU 执行单元、生成并应用最小接线改动、
运行目标框架原生入口并验证产物。只给出代码片段、只复制 controller、或用统一外层 harness 包裹几个库的
workload，都不算完成自动适配。

发现脚本提供候选和证据，不替代 Agent 对调用链、进程边界及业务 step 语义的确认。自动修改前记录基线和
目标文件 SHA；修改后保留 diff。无法确认真实 NPU worker 或正确 step 边界时停止接线并报告证据缺口，不能
猜测注入点。

## 工作流

1. 建立基线：记录原启动命令、退出码和关键模型输出。不要修改用户原始启动脚本；创建独立
   profiler 启动入口或新增默认关闭的配置。
2. 发现执行单元：运行 `python scripts/discover_execution_loops.py <repo> --json`，结合源码调用链确认真正
   执行 NPU forward/backward/generate 的进程和循环。优先使用 `confidence=high` 且 evidence 对应业务
   step 的候选。调度进程不是注入点。候选为空时继续检查框架 callback/hook、worker execute 或 generate
   入口，不能因为启发式搜索为空就随意选择文件。
3. 选择场景并只读取对应参考：
   - 训练或 Trainer/Engine 循环：`references/training_best_practices.md`
   - 服务化推理或 Worker 执行：`references/inference_best_practices.md`
   - Actor/Rollout/Ref 多阶段：`references/rl_best_practices.md`
   - API 参数不确定时：`references/api_reference.md`
4. 生成通用封装：先运行
   `python scripts/scaffold_adapter.py <repo> --destination <package>/profiler_adapter.py --dry-run`，
   检查目标后再移除 `--dry-run`。脚本拒绝覆盖已有不同内容。
5. 接线：按目标框架选择一种明确模式：
   - 显式有限循环：使用 `with controller:` 包裹循环，每个完整业务 step 后调用 `step()`。
   - Trainer/Engine callback：在 optimizer/global step 完成的 callback 中调用 `step()`；生命周期由包裹
     原生 `train()` 的 `with controller:` 或等价 `try/finally` 管理。只有框架明确保证异常退出也调用结束
     callback 时，才可仅用开始/结束 callback 管理 `start/stop`。不能按 dataloader micro-batch 或 epoch
     错位计步。
   - 长期服务 worker：API/RPC 仅转发启停请求，由执行 NPU 的 worker 调用可重复的 `start/stop`；同一
     worker 同时只能有一个活动会话。
   - RL 多阶段：每个实际执行 NPU 的 role/stage 独立持有 controller 和输出目录，阶段结束即 stop。

   配置必须从原入口完整透传，默认 `enabled=false`。CLI/YAML/环境变量值统一交给
   `ProfilerConfig.from_mapping` 严格解析；多进程启用时必须提供当前 global rank 或标准 rank 环境变量。
6. 双路径回归：分别运行 profiler 关闭和开启路径。关闭路径必须与基线等价；开启路径必须使用
   足够的 step，并检查 error 日志。每次回归使用新的唯一输出目录，先确认它不存在；不得用
   `rm -rf`、递归删除或覆盖旧 profiler 产物来准备测试。
7. 验收产物：运行
   `python scripts/validate_profile_output.py <output-dir> --expect text --expected-sessions <N>`。多 worker/rank
   任务同时传入 `--expected-workers` 或 `--expected-ranks`。校验器按每个独立
   `ASCEND_PROFILER_OUTPUT` 会话验证 trace 与 kernel CSV，禁止跨会话拼接。完整验收必须使用 Text 导出；
   只有命令返回 0，且报告 `parsed_output_ready` 和 `visualizable` 均为 `true`，才可宣称采集、解析
   和可视化链路通过。DB-only 校验只能证明数据库结构可供后续工具读取。
8. 保存证据：至少保存目标框架版本和 commit、msAgent 模型、适配 prompt、基线与开启命令、修改 diff、
   关闭/开启关键输出、真实产物校验 JSON，以及 trace/kernel CSV 的 SHA-256。任何 mock 证据必须单独标记。

## 接线范式

```python
from .profiler_adapter import ProfilerConfig, ProfilerController

controller = ProfilerController(ProfilerConfig.from_mapping(config.profiler))
with controller:
    for batch in batches:
        result = run_one_step(batch)  # 原业务调用不改写
        controller.step()
```

多进程框架应在每个实际执行 NPU 计算的 worker 内各建一个实例，并用 `ranks` 过滤采集范围。
服务 API 只能把启停请求转发到 worker；不能在 API/调度进程内创建 profiler。异常路径也必须执行
`stop()`，但 profiler 的关闭异常不得吞掉原业务异常。

## 不变量

- 不改变 forward/backward/generate、优化器、数据顺序、返回值或并行拓扑。
- 不删除、清空或覆盖用户代码、日志和旧采集目录；需要新产物时创建唯一的新目录。
- 默认关闭；关闭时不导入 `torch_npu`、不创建目录、不增加 NPU 同步点。
- 同一进程最多一个活动 profiler 实例；并发、重复 `start/stop` 应安全且可诊断。一次会话 stop 后允许
  同一 controller 开启下一次服务或 RL 阶段会话。
- `start_step + (wait + warmup + active) * repeat` 不得超过实际调用 `step()` 的次数。
- 封装集中在独立模块；禁止在业务文件中散落 profiler 参数和 `profile(...)` 调用。
- 采集目录必须按 rank/worker 隔离，避免多进程相互覆盖。未显式设置 `worker_name` 时通用封装自动使用
  `rank_<global-rank>_pid_<pid>`。
- Text 产物至少包含可解析的 `trace_view.json` 和解析生成的有效统计 CSV；DB 产物必须是有效 SQLite
  数据库。仅有目录或空文件不算通过，DB-only 也不能替代完整可视化验收。
- 没有真实 NPU 环境时，可以完成静态和模拟测试，但必须把硬件采集、解析、可视化标为未验证，
  不得用 mock 结果代替验收证据。

## 交付报告

按框架逐项给出：基线命令与结果、修改文件、注入进程/循环、关闭路径结果、开启路径命令、输出目录、
校验脚本结果、可视化入口和已知限制。覆盖 3 个以上框架时使用矩阵，明确哪些是实际运行证据，哪些仅为
模拟或静态检查。三个框架必须分别运行其原生入口并分别产生 diff 和独立 profiling 会话；共享 smoke
harness 只能作为底层兼容性检查，不能替代自动适配验收。
