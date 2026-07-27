# `torch_npu.profiler` 配置参考

## 默认建议

| 配置项 | 建议值 |
|--------|--------|
| `profiler_level` | `Level0` |
| `activities` | `CPU + NPU` |
| `wait` | `0` |
| `warmup` | `0` |
| `active` | `1` |
| `repeat` | `1` |
| `skip_first` | `1` |
| `record_shapes` | `False` |
| `profile_memory` | `False` |
| `with_stack` | `False` |
| `output_dir` | `./result` |

## 关键参数

| 参数 | 说明 |
|------|------|
| `activities` | 通常只用 `torch_npu.profiler.ProfilerActivity.CPU` 和 `NPU` |
| `schedule.active` | 实际采集步数 |
| `schedule.skip_first` | 采集前先跳过的 step 数 |
| `record_shapes` | 是否记录 shape |
| `profile_memory` | 是否记录内存 |
| `with_stack` | 是否记录调用栈 |
| `experimental_config.profiler_level` | `Level0` / `Level1` / `Level2` |

## 输出产物

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

验证时以 `ASCEND_PROFILER_OUTPUT/` 下的产物为准，不要按通用 PyTorch Profiler 的 `.pt.json` 或 `trace_*.json` 结构判断。

## 注意

- 默认不要使用 `skip_first_wait`
- 如果采到的只有 warmup，优先增大 `skip_first`

## 参考链接

- [昇腾 PyTorch Profiler 官方文档](https://www.hiascend.com/document/detail/zh/mindstudio/830/T&ITools/Profiling/atlasprofiling_16_0121.html)
- [MSTX 打点指南](https://www.hiascend.com/document/detail/zh/Pytorch/730/apiref/torchnpuCustomsapi/docs/context/torch_npu-npu-mstx.md)
