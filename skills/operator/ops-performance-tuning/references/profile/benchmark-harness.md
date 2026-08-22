# 工程无 benchmark 时的计时 harness 模板

> SKILL Step 3 要求固定 warmup/repeat 与设备 event 计时，但 cann-samples 类样例普遍只有"单次 launch + 精度 PASS"。本文档给出合规的最小补齐方式。

## 归类原则：测量设施 ≠ 优化变量

- 在**产物目录的工程副本**上加计时，不直接改用户基线源码；改动前先把原始源码备份到 `baseline/`。
- harness 改动（aclrtEvent 创建/记录、warmup 循环、打印耗时）属于测量设施：前后两轮使用**同一份 harness**，不得把 harness 改动写进优化 diff。
- kernel 源码一行不动；若必须改（如 SHMEMI_PROF 插桩、phase skip 位），单独归类为"诊断插桩"，同样不算优化变量，并在报告中注明。

## aclrtEvent 模板（host 侧）

```cpp
// 在 kernel launch 调用处包一层；WARMUP/REPEAT 编译期或环境变量固定，前后一致
aclrtEvent start, stop;
aclrtCreateEvent(&start);
aclrtCreateEvent(&stop);
for (int i = 0; i < WARMUP; ++i) { /* kernel launch */ }
aclrtSynchronizeStream(stream);
aclrtRecordEvent(start, stream);
for (int i = 0; i < REPEAT; ++i) { /* kernel launch */ }
aclrtRecordEvent(stop, stream);
aclrtSynchronizeStream(stream);
float ms = 0.f;
aclrtEventElapsedTime(&ms, start, stop);
printf("[perf] kernel avg: %.2f us over %d iters (warmup %d)\n",
       ms * 1000.0f / REPEAT, REPEAT, WARMUP);
```

- warmup 剔除冷跑（首跑含初始化/缓存预热，实测可差 25%）；稳态多轮取中位。
- 通信/多 rank 算子：跨 rank 同步后再计时，报告每 rank 最大值与均值（通信性能由最慢 rank 决定）；e2e wall 含进程派生与 SHMEM 握手，与 kernel 耗时分开记录。
- 进程级复跑（≥3 次）消除单次噪声；run 间噪声 >5% 时，小于噪声的收益不得归因。

## 何时允许用 profiler Task Duration 作基线

- kernel 为 ms 级长耗时、工程无 event 设施、且插桩开销占比可忽略时：允许（`result_saver.py --timing-method msprof_task_duration`），报告注明口径。
- µs 级短 kernel：禁止——插桩耗时不能替代 event；用"stream 内多 launch 单 event 对"放大可测时长，或以 device duration 作辅助口径并注明局限。
