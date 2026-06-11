---
name: pytorch-snapshot-memory-comparator
description: 对比两份 PyTorch CUDA/NPU memory snapshot pickle 文件，分析 Reserved/Allocated 峰值差异、扩容事件、碎片率。Invoke when user needs to compare memory snapshots, diagnose OOM, analyze memory growth, or compare across NPU/GPU devices.
---

# PyTorch Memory Snapshot Comparator

## 角色定义

你是 PyTorch Memory Snapshot 对比分析专家，帮助用户快速对比内存快照数据。

## 核心能力

| 能力 | 说明 |
|------|------|
| **峰值对比** | 对比 Reserved / Allocated / Active 内存峰值 |
| **状态对比** | 对比开始采集和结束采集时的内存状态 |
| **扩容统计** | 统计新增 Segment 数量、扩容 Segment 详情 |
| **碎片分析** | 计算碎片率 (Reserved - Allocated) / Reserved |
| **Block 分析** | 统计各状态 Block 数量和分布 |
| **卡间对比** | 单文件内不同 device 的内存对比 |
| **全卡概览** | 展示所有 device 的内存使用一览 |

## 支持的后端

- CUDA (`torch.cuda.memory._dump_snapshot`)
- NPU (`torch_npu.npu.memory._dump_snapshot`)

## 使用方式

### 文件间对比

对比两份不同时间点的 snapshot：

```bash
python scripts/memory_compare.py snap_start.pkl snap_end.pkl
```

报告示例输出：

```
============================================================
  PyTorch Memory Snapshot 对比报告
============================================================

后端类型: cuda

指标                   Snapshot A    Snapshot B          差异   趋势
------------------------------------------------------------------------
Reserved (峰值)           10.24 GB       12.56 GB       2.32 GB    ⬆️
Allocated                 8.50 GB       10.80 GB       2.30 GB    ⬆️
Active                    8.49 GB       10.78 GB       2.29 GB    ⬆️
碎片率                        5.2%          3.8%         1.4%    ⬇️

============================================================
  扩容分析
============================================================

新增 Segment 数量: 3

扩容的 Segment Top 3:
地址                        扩容前        扩容后        增长量
----------------------------------------------------------
0x7f0000000000             1.00 GB       2.20 GB       1.20 GB
0x7f0100000000           512.00 MB       1.00 GB     512.00 MB
```

### 文件内卡间对比

```bash
python scripts/memory_compare.py snap.pkl --device 0 --device 2
```

### 全卡概览

```bash
python scripts/memory_compare.py snap.pkl --all-devices
```

### 单卡分析

```bash
python scripts/memory_compare.py snap.pkl --device 0
```

### 输出 JSON 报告

```bash
python scripts/memory_compare.py snap_a.pkl snap_b.pkl -o report.json
```

## 采集 snapshot 数据

### CUDA 环境

```python
import torch

# 开始记录
torch.cuda.memory._record_memory_history(max_entries=100000)

# 执行你的代码
# ...

# 导出 snapshot
torch.cuda.memory._dump_snapshot("snapshot.pkl")

# 停止记录
torch.cuda.memory._record_memory_history(enabled=None)
```

### NPU 环境

```python
import torch
import torch_npu

# 开始记录
torch_npu.npu.memory._record_memory_history(max_entries=100000)

# 执行你的代码
# ...

# 导出 snapshot
torch_npu.npu.memory._dump_snapshot("snapshot.pkl")

# 停止记录
torch_npu.npu.memory._record_memory_history(enabled=None)
```

## 输出解读

| 指标 | 说明 |
|------|------|
| **Reserved** | 缓存分配器从系统预留的总内存（含碎片） |
| **Allocated** | 实际分配给 tensor 的内存 |
| **Active** | 当前正在使用的内存 |
| **碎片率** | (Reserved - Allocated) / Reserved，越高说明碎片越严重 |
| **新增 Segment** | 对比 B 比 A 多出的 segment 数量，表示扩容事件 |
| **扩容 Segment** | 地址相同但 size 变大的 segment，表示扩容 |