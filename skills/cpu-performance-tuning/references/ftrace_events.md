# ftrace 事件指南

## 一、ftrace 概述

### 1.1 什么是 ftrace

ftrace 是 Linux 内核提供的一种强大的追踪工具，用于：
- 调试和分析内核行为
- 性能分析和瓶颈定位
- 理解进程调度和中断处理

### 1.2 常用术语

| 术语 | 说明 |
|------|------|
| **tracepoint** | 内核中定义的追踪点 |
| **event** | 触发的追踪事件 |
| **buffer** | 事件存储缓冲区 |
| **trigger** | 事件触发条件 |
| **filter** | 事件过滤条件 |

## 二、常用事件

### 2.1 调度事件

| 事件名 | 说明 |
|--------|------|
| `sched_switch` | 进程调度切换 |
| `sched_wakeup` | 进程唤醒 |
| `sched_wakeup_new` | 新进程唤醒 |
| `sched_migrate_task` | 任务迁移 |
| `sched_stat_runtime` | 进程运行时间统计 |
| `sched_stat_wait` | 进程等待时间统计 |
| `sched_stat_iowait` | 进程 I/O 等待时间统计 |

### 2.2 中断事件

| 事件名 | 说明 |
|--------|------|
| `irq_handler_entry` | 中断处理入口 |
| `irq_handler_exit` | 中断处理出口 |
| `softirq_entry` | 软中断入口 |
| `softirq_exit` | 软中断出口 |
| `softirq_raise` | 软中断触发 |

### 2.3 定时器事件

| 事件名 | 说明 |
|--------|------|
| `timer_start` | 定时器启动 |
| `timer_expire_entry` | 定时器到期 |
| `timer_expire_exit` | 定时器处理完成 |

### 2.4 进程事件

| 事件名 | 说明 |
|--------|------|
| `task_newtask` | 新进程创建 |
| `task_rename` | 进程重命名 |
| `sched_process_fork` | 进程 fork |
| `sched_process_exit` | 进程退出 |

### 2.5 内存事件

| 事件名 | 说明 |
|--------|------|
| `mm_page_alloc` | 页面分配 |
| `mm_page_free` | 页面释放 |
| `page_fault_user` | 用户态页错误 |
| `page_fault_kernel` | 内核态页错误 |

### 2.6 工作队列事件

| 事件名 | 说明 |
|--------|------|
| `workqueue_execute_start` | 工作队列执行开始 |
| `workqueue_execute_end` | 工作队列执行结束 |
| `workqueue_queue_work` | 工作队列入队 |

## 三、使用方法

### 3.1 基本操作

```bash
# 挂载 debugfs（如果未挂载）
mount -t debugfs nodev /sys/kernel/debug

# 查看可用事件
cat /sys/kernel/debug/tracing/available_events

# 启用事件
echo 1 > /sys/kernel/debug/tracing/events/sched/sched_switch/enable

# 禁用事件
echo 0 > /sys/kernel/debug/tracing/events/sched/sched_switch/enable

# 开始追踪
echo 1 > /sys/kernel/debug/tracing/tracing_on

# 停止追踪
echo 0 > /sys/kernel/debug/tracing/tracing_on

# 查看追踪数据
cat /sys/kernel/debug/tracing/trace
```

### 3.2 配置过滤

```bash
# 设置进程过滤
echo "comm == bash" > /sys/kernel/debug/tracing/events/sched/sched_switch/filter

# 设置 PID 过滤
echo "pid == 1234" > /sys/kernel/debug/tracing/events/sched/sched_wakeup/filter

# 设置 CPU 范围
echo "0-3" > /sys/kernel/debug/tracing/cpumask

# 设置缓冲区大小
echo 10240 > /sys/kernel/debug/tracing/buffer_size_kb
```

### 3.3 追踪特定进程

```bash
# 追踪特定 PID
echo "1234" > /sys/kernel/debug/tracing/set_ftrace_pid

# 清除 PID 过滤
echo > /sys/kernel/debug/tracing/set_ftrace_pid
```

## 四、数据分析

### 4.1 事件格式

典型的 ftrace 事件格式：
```
<idle>-0     [000] d...  1234.567890: sched_switch: prev_comm=swapper/0 prev_pid=0 prev_prio=120 prev_state=R ==> next_comm=bash next_pid=1234 next_prio=120
```

| 字段 | 说明 |
|------|------|
| `comm` | 进程名 |
| `pid` | 进程 ID |
| `cpu` | CPU 编号 |
| `flags` | 进程状态标志 |
| `timestamp` | 时间戳 |
| `event` | 事件名 |
| `details` | 事件详情 |

### 4.2 状态标志

| 标志 | 说明 |
|------|------|
| `R` | Running |
| `S` | Sleeping |
| `D` | Uninterruptible sleep |
| `T` | Stopped |
| `Z` | Zombie |
| `+` | In foreground process group |

### 4.3 常用分析命令

```bash
# 统计调度切换次数
grep sched_switch trace | wc -l

# 统计特定进程的调度次数
grep "next_comm=bash" trace | wc -l

# 分析 CPU 分布
awk '{print $3}' trace | sort | uniq -c

# 分析调度延迟
python analyze_sched_delay.py trace
```

## 五、性能影响

### 5.1 开销评估

| 事件数量 | 开销 |
|----------|------|
| 1-5 | < 1% |
| 5-10 | 1-3% |
| 10-20 | 3-5% |
| 20+ | > 5% |

### 5.2 优化建议

1. **只追踪需要的事件**：避免启用过多事件
2. **限制 CPU 范围**：只追踪相关 CPU
3. **设置合适的缓冲区大小**：避免频繁刷新
4. **使用过滤条件**：减少事件数量
5. **短期采集**：避免长时间追踪

## 六、注意事项

### 6.1 权限要求

- ftrace 需要 root 权限
- debugfs 需要挂载
- 部分功能需要内核配置支持

### 6.2 内核版本

- 建议使用 Linux 4.15+
- 不同版本事件可能有所差异
- 某些事件需要特定内核配置

### 6.3 数据安全

- 追踪数据可能包含敏感信息
- 注意保护追踪日志
- 生产环境谨慎使用
