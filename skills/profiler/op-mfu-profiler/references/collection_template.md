# 采集脚本模板

**仅用于对照，不要要求用户脚本照抄。** 用户脚本结构与参数化方式各不相同，重点是四项必须配置到位（见 SKILL.md 第 2 步），其余保持用户原设置即可。

## 完整模板

```python
import torch
import torch_npu

def train_one_step():
    # 你的算子或者模型
    pass

experimental_config = torch_npu.profiler._ExperimentalConfig(
    profiler_level=torch_npu.profiler.ProfilerLevel.Level1,
    aic_metrics=torch_npu.profiler.AiCMetrics.PipeUtilization,
    mstx=True,
    data_simplification=True,
    export_type=[
        torch_npu.profiler.ExportType.Text,
        torch_npu.profiler.ExportType.Db,
    ],
)

prof = torch_npu.profiler.profile(
    activities=[
        torch_npu.profiler.ProfilerActivity.CPU,
        torch_npu.profiler.ProfilerActivity.NPU,
    ],
    schedule=torch_npu.profiler.schedule(wait=0, warmup=1, active=1, repeat=1, skip_first=1),
    on_trace_ready=torch_npu.profiler.tensorboard_trace_handler("./result"),
    profile_memory=False,
    with_stack=False,
    with_flops=True,
    experimental_config=experimental_config,
)
prof.start()    # 启动性能数据采集
for step in range(3):
    train_one_step()
    prof.step()    # 与 schedule 配套使用
prof.stop()    # 结束性能数据采集
```

四项必须配置对应位置：

| 配置项 | 位置 |
| --- | --- |
| `with_flops=True` | `torch_npu.profiler.profile(...)` |
| `mstx=True` | `_ExperimentalConfig(...)` |
| `export_type` 含 `Db` | `_ExperimentalConfig(...)` |
| `profiler_level ≥ Level1` | `_ExperimentalConfig(...)` |

## （可选）module 级 msTX 打点

只要 kernel 级 MFU 明细可跳过。需要 module 级统计时，在模型代码里加 `torch_npu.npu.mstx.range_start/range_end`，domain 用 `"Module"`：

```python
original_call = nn.Module.__call__

def custom_call(self, *args, **kwargs):
    module_name = self.__class__.__name__
    mstx_id = torch_npu.npu.mstx.range_start(module_name, domain="Module")
    result = original_call(self, *args, **kwargs)
    torch_npu.npu.mstx.range_end(mstx_id, domain="Module")
    return result

nn.Module.__call__ = custom_call
```
