# torch_npu.profiler API 参考

## 核心参数

| 参数 | 说明 |
|---|---|
| `activities` | `[ProfilerActivity.CPU, ProfilerActivity.NPU]`，决定采集来源 |
| `schedule` | `torch_npu.profiler.schedule(wait=0, warmup=0, active=1, repeat=1, skip_first=1)` |
| `on_trace_ready` | 结果处理器，常用 `tensorboard_trace_handler("./result")` |
| `profile_memory` | `bool`，是否采集内存 |
| `with_stack` / `with_modules` | 调用栈/模块级采集 |
| `record_shapes` | `bool`，记录 Tensor shape |
| `experimental_config` | 扩展配置对象，见下方 |

## `_ExperimentalConfig` 常用字段

| 字段 | 取值 |
|---|---|
| `export_type` | `[ExportType.Db]` |
| `profiler_level` | `ProfilerLevel.Level0`（基础）/ `Level1`（带通信算子） |
| `aic_metrics` | `AiCMetrics.AiCoreNone` / `PipeUtilization` 等 |
| `data_simplification` | `bool` |
| `mstx` | `bool`，MSTX 标记 |

## 两种使用方式

**with 语句**（自动管理生命周期）：
```python
with torch_npu.profiler.profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.NPU],
    schedule=torch_npu.profiler.schedule(wait=0, warmup=0, active=1, repeat=1, skip_first=1),
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("./result"),
    experimental_config=_ExperimentalConfig(
        export_type=[ExportType.Text], profiler_level=ProfilerLevel.Level0,
    ),
) as prof:
    for step in range(steps):
        train_one_step()
        prof.step()
```

**start/stop**（灵活控制）：
```python
prof = torch_npu.profiler.profile(activities=[...], schedule=..., on_trace_ready=...)
prof.start()
for step in range(steps):
    train_one_step()
    prof.step()
prof.stop()
```

## 关键约束

- **单进程单实例**：一个进程只允许一个 profiler 实例
- **同进程调用**：profiler 必须与被采集流程同一进程
- **Step 数**：`skip_first + (wait + warmup + active) * repeat` < 总 step 数

## 输出结构 & 验证

```
{OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/
├── trace_view.json      # 全量 trace
├── op_statistic.csv     # 算子统计
├── kernel_details.csv   # kernel 详情
└── ...
```

```bash
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/trace_view.json
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/op_statistic.csv
ls {OUTPUTDIR}/*/ASCEND_PROFILER_OUTPUT/kernel_details.csv
```
