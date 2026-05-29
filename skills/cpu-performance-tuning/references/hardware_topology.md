# 硬件拓扑分析指南

## 一、CPU 拓扑结构

### 1.1 基本概念

| 术语 | 说明 |
|------|------|
| **Socket** | 物理 CPU 插槽，每个 Socket 包含多个核心 |
| **Core** | 物理核心，每个核心可以有多个线程 |
| **Thread** | 逻辑线程（超线程），共享核心资源 |
| **NUMA Node** | NUMA 节点，包含一组 CPU 核心和本地内存 |
| **QPI/UPI** | CPU 间高速互联总线 |

### 1.2 查看 CPU 拓扑

```bash
# 查看完整 CPU 信息
lscpu

# 查看 NUMA 节点
numactl -H

# 查看每个 CPU 的详细信息
cat /proc/cpuinfo

# 查看 NUMA 节点的 CPU 列表
for i in /sys/devices/system/node/node*/cpulist; do
    echo "Node $(basename $(dirname $i) | sed 's/node//'): $(cat $i)"
done
```

### 1.3 NUMA 架构特点

| 特性 | 说明 |
|------|------|
| **本地内存访问** | CPU 访问同一 NUMA 节点的内存速度更快 |
| **远程内存访问** | CPU 访问其他 NUMA 节点的内存速度较慢（约 2-3 倍延迟） |
| **内存带宽** | 每个 NUMA 节点有独立的内存控制器和带宽 |
| **缓存一致性** | 通过 QPI/UPI 维护缓存一致性 |

## 二、NPU 拓扑结构

### 2.1 查看 NPU 拓扑

```bash
# 华为昇腾 NPU 拓扑
npu-smi info -t topo

# 查看 NPU 设备信息
npu-smi info
```

### 2.2 NUMA 亲和关系

| NPU 型号 | NUMA 亲和 | 说明 |
|----------|-----------|------|
| **Ascend A2** | 有 | 绑定到特定 NUMA 节点 |
| **Ascend A3** | 有 | 绑定到特定 NUMA 节点 |
| **Ascend A5** | 无 | 使用灵衢总线，无 NUMA 亲和 |

### 2.3 灵衢总线（UB）

- **特点**：高带宽、低延迟的片间互联
- **优势**：不依赖 NUMA 架构，可灵活调度
- **检测**：通过 `npu-smi info` 输出判断

## 三、网络设备拓扑

### 3.1 网卡中断绑核

```bash
# 查看网卡中断
grep eth0 /proc/interrupts

# 设置中断绑核
echo "56-63" > /proc/irq/120/smp_affinity_list

# 配置多队列
ethtool -L eth0 combined 8
```

### 3.2 RPS/RFS 配置

```bash
# 启用 RPS
echo ffffffff > /sys/class/net/eth0/queues/rx-0/rps_cpus

# 配置 RFS 流动表大小
echo 32768 > /proc/sys/net/core/rps_sock_flow_entries
```

## 四、最佳实践

### 4.1 绑核策略

1. **计算密集型任务** → 绑定到性能核，保持 NUMA 本地性
2. **I/O 密集型任务** → 绑定到普通核，可与其他 I/O 任务共享
3. **中断处理** → 绑定到专用核，隔离干扰
4. **内存访问密集型任务** → 绑定到靠近内存控制器的核

### 4.2 NUMA 内存策略

```bash
# 绑定到指定 NUMA 节点
numactl --cpunodebind=0 --membind=0 ./application

# 使用 interleaving 模式
numactl --interleave=all ./application

# 优先使用本地内存
numactl --preferred=0 ./application
```

### 4.3 性能监控

```bash
# 实时 CPU 监控
mpstat -P ALL 1

# 进程级 CPU 统计
pidstat -u -t -p <pid> 1

# NUMA 内存访问统计
numastat -p <pid>
```

## 五、常见问题

### 5.1 CPU 负载不均衡

**现象**：部分 CPU 核负载过高，其他核空闲

**原因**：
- 绑核策略不合理
- 中断分布不均
- 进程调度问题

**解决**：
- 重新设计绑核方案
- 配置中断绑核
- 调整进程优先级

### 5.2 NUMA 穿越率高

**现象**：远程内存访问比例高

**原因**：
- 进程绑定到错误的 NUMA 节点
- 内存分配策略不当
- 数据布局不合理

**解决**：
- 使用 numactl 绑定进程
- 配置内存策略
- 优化数据结构布局

### 5.3 中断干扰严重

**现象**：关键任务被频繁中断

**原因**：
- 中断未隔离
- irqbalance 自动均衡导致波动
- 高频中断源过多

**解决**：
- 关闭 irqbalance
- 手动配置中断绑核
- 启用 RPS/RFS
