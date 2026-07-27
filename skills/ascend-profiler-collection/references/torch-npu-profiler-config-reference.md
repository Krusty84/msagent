# `torch_npu.profiler` 配置参考

## Profiler Level 对比

| Level | 采集内容 | 数据量 | 推荐场景 |
|-------|----------|--------|----------|
| `Level_none` | 不采集由 Level 控制的附加数据 | 极小 | 自定义打点、只验证基础链路 |
| `Level0` | 上层应用数据、底层 NPU 数据、算子信息 | 大 | 深入算子分析 |
| `Level1` | `Level0` + CANN 层 AscendCL + AI Core 性能指标 + 通信算子 | 较大 | 常规通信和计算分析，默认推荐 |
| `Level2` | `Level1` + CANN 层 Runtime + AI CPU 数据 | 大 | 全量 CANN 层分析 |

## 三种采集方式对比

| 方式 | 适用场景 | 优点 | 缺点 |
|------|----------|------|------|
| 方式 A（推荐） | 标准训练循环 | 自动对齐循环并推进 `step()` | 需要能准确识别训练循环位置 |
| 方式 B | 推理脚本或无循环场景 | 最简单，不依赖完整 schedule | 没有 step 维度数据 |
| 方式 C | 指定代码段 | 可精确控制采集范围 | 需要用户明确指定起止位置 |

## Profiling 输出产物结构

```text
profiling_result/
├── worker_0_20260101_120000/         # 单个 worker 的采集目录
│   ├── trace_1.json                  # Chrome trace，可拖入 chrome://tracing 查看
│   ├── memory_1.json                 # 内存数据
│   └── ...
├── worker_0_20260101_120000.pt.json  # 汇总 trace，供分析工具使用
└── profiler_metadata.json            # 元信息
```

## 常见配置项

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `activities` | list | 采集的活动类型，可选 `torch_npu.profiler.ProfilerActivity.CPU` 和 `ProfilerActivity.NPU` |
| `schedule.wait` | int | 采集开始前等待的 step 数 |
| `schedule.warmup` | int | 预热 step 数，预热阶段不采集 |
| `schedule.active` | int | 实际采集的 step 数 |
| `schedule.repeat` | int | 采集重复次数 |
| `schedule.skip_first` | int | 采集启动前一次性跳过的 step 数，动态 Shape 场景建议不小于 `10` |
| `schedule.skip_first_wait` | int | 是否跳过第一次循环的 `wait` 阶段，`1` 表示跳过，`0` 表示不跳过 |
| `record_shapes` | bool | 是否记录 tensor 维度信息 |
| `profile_memory` | bool | 是否采集内存数据 |
| `with_stack` | bool | 是否记录调用栈 |
| `with_modules` | bool | 是否记录模块层级 |
| `with_flops` | bool | 是否记录 FLOPs |
| `experimental_config.aic_metrics` | enum | AI Core 指标类型 |
| `experimental_config.profiler_level` | enum | 采集级别，取值通常为 `Level0`、`Level1`、`Level2` |
| `experimental_config.mstx` | bool | 是否启用 MSTX 打点 |
| `experimental_config.l2_cache` | bool | 是否采集 L2 Cache 数据 |
| `experimental_config.op_attr` | bool | 是否采集算子属性 |
| `experimental_config.data_simplification` | bool | 是否启用数据简化 |
| `experimental_config.record_op_args` | bool | 是否记录算子参数 |
| `experimental_config.host_sys` | list | Host 侧系统事件采集列表 |
| `experimental_config.sys_io` | bool | 是否采集系统 IO |
| `experimental_config.sys_interconnection` | bool | 是否采集系统互联 |

## `skip_first` 与 `skip_first_wait` 的区别

| 参数 | 生效时机 | 典型用途 |
|------|----------|----------|
| `skip_first` | 整个 schedule 启动前 | 跳过训练初期的抖动，或跳过动态 Shape 尚未稳定的 step |
| `skip_first_wait` | 第一次循环的 `wait` 阶段 | 让第一次采集更快开始，避免 `skip_first` 之后还要额外等待一个完整 `wait` 周期 |

示例：`wait=20, skip_first=10`

- `skip_first_wait=0`：第一次预热前需要等待 `skip_first + wait = 30` 步
- `skip_first_wait=1`：第一次预热前只需等待 `skip_first = 10` 步，后续循环间仍按 `wait=20` 等待

配置公式：

```text
step 总数 >= skip_first + (wait + warmup + active) * repeat
```

注意：

- 本 Skill 的默认做法是不使用 `skip_first_wait`
- 如果用户环境或框架对该参数支持不稳定，优先只保留 `skip_first`

## 参考链接

- [昇腾 PyTorch Profiler 官方文档](https://www.hiascend.com/document/detail/zh/mindstudio/830/T&ITools/Profiling/atlasprofiling_16_0121.html)
- [MSTX 打点指南](https://www.hiascend.com/document/detail/zh/Pytorch/730/apiref/torchnpuCustomsapi/docs/context/torch_npu-npu-mstx.md)
- [profiling-analysis 系列 Skill](../../profiling-analysis/SKILL.md)
