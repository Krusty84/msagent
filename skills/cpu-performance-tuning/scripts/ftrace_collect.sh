#!/bin/bash
#
# ftrace 数据采集脚本
# 支持自定义事件、CPU 范围、采集时长
# 参考: MindStudio trace_record.py

set -e

# 默认配置
DURATION=30
CPU_MASK="all"
OUTPUT_DIR="./ftrace_out"
BUFFER_SIZE_KB=40960

# 事件配置（参考 MindStudio trace_record.py）
# CPU调度事件
SCHED_EVENTS=(
    "sched:sched_switch"
    "sched:sched_wakeup"
    "sched:sched_waking"
    "sched:sched_wakeup_new"
    "sched:sched_migrate_task"
    "sched:sched_stat_runtime"
    "sched:sched_process_fork"
    "sched:sched_process_exec"
    "sched:sched_process_exit"
)

# 中断事件
IRQ_EVENTS=(
    "irq:irq_handler_entry"
    "irq:irq_handler_exit"
    "irq:softirq_raise"
    "irq:softirq_entry"
    "irq:softirq_exit"
)

# 锁竞争事件（默认关闭）
FUTEX_EVENTS=(
    "syscalls:sys_enter_futex"
    "syscalls:sys_exit_futex"
)

# 默认启用的事件
ENABLED_EVENTS=("${SCHED_EVENTS[@]}" "${IRQ_EVENTS[@]}")

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        -d|--duration)
            DURATION="$2"
            shift 2
            ;;
        -c|--cpu-mask)
            CPU_MASK="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        -b|--buffer-size)
            BUFFER_SIZE_KB="$2"
            shift 2
            ;;
        --sched)
            if [ "$2" = "0" ]; then
                # 从 ENABLED_EVENTS 中移除 SCHED_EVENTS
                for sched_event in "${SCHED_EVENTS[@]}"; do
                    ENABLED_EVENTS=("${ENABLED_EVENTS[@]/$sched_event}")
                done
            fi
            shift 2
            ;;
        --irq)
            if [ "$2" = "0" ]; then
                # 从 ENABLED_EVENTS 中移除 IRQ_EVENTS
                for irq_event in "${IRQ_EVENTS[@]}"; do
                    ENABLED_EVENTS=("${ENABLED_EVENTS[@]/$irq_event}")
                done
            fi
            shift 2
            ;;
        --futex)
            if [ "$2" = "1" ]; then
                # 添加 FUTEX_EVENTS 到 ENABLED_EVENTS
                ENABLED_EVENTS=("${ENABLED_EVENTS[@]}" "${FUTEX_EVENTS[@]}")
            fi
            shift 2
            ;;
        -e|--event)
            ENABLED_EVENTS+=("$2")
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo "Options:"
            echo "  -d, --duration <seconds>    采集时长（默认：30）"
            echo "  -c, --cpu-mask <mask>       CPU 范围，如 0-31 或 all（默认：all）"
            echo "  -o, --output <dir>          输出目录（默认：./ftrace_out）"
            echo "  -b, --buffer-size <kb>      缓冲区大小（默认：40960 KB）"
            echo "  --sched <0|1>               启用/禁用调度事件（默认：1）"
            echo "  --irq <0|1>                 启用/禁用中断事件（默认：1）"
            echo "  --futex <0|1>               启用/禁用锁竞争事件（默认：0）"
            echo "  -e, --event <event>         添加额外事件（格式：category:event）"
            echo "  -h, --help                  显示帮助"
            echo ""
            echo "支持的事件列表:"
            echo "  调度事件: ${SCHED_EVENTS[*]}"
            echo "  中断事件: ${IRQ_EVENTS[*]}"
            echo "  锁竞争事件: ${FUTEX_EVENTS[*]}"
            exit 0
            ;;
        *)
            echo "未知参数: $1"
            exit 1
            ;;
    esac
done

# 检查权限
if [[ $EUID -ne 0 ]]; then
    echo "错误：需要 root 权限运行此脚本"
    exit 1
fi

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 挂载 debugfs（如未挂载）
if [ ! -d /sys/kernel/debug ]; then
    mount -t debugfs nodev /sys/kernel/debug 2>/dev/null || true
fi

# 查找追踪根目录
TRACE_ROOT=""
if [ -d "/sys/kernel/tracing" ]; then
    TRACE_ROOT="/sys/kernel/tracing"
elif [ -d "/sys/kernel/debug/tracing" ]; then
    TRACE_ROOT="/sys/kernel/debug/tracing"
else
    echo "错误：未找到追踪根目录"
    exit 1
fi

# 停止已有 trace
echo 0 > "$TRACE_ROOT/tracing_on"

# 清除旧数据
echo > "$TRACE_ROOT/trace"

# 设置 CPU 缓冲大小
echo "$BUFFER_SIZE_KB" > "$TRACE_ROOT/buffer_size_kb"

# 设置 CPU 范围
if [[ "$CPU_MASK" != "all" ]]; then
    echo "$CPU_MASK" > "$TRACE_ROOT/cpumask"
fi

# 设置追踪时钟
echo "mono_raw" > "$TRACE_ROOT/trace_clock"

# 清除事件
echo > "$TRACE_ROOT/set_event"
echo 0 > "$TRACE_ROOT/events/enable"

# 启用事件
enabled_count=0
for event in "${ENABLED_EVENTS[@]}"; do
    if [ -z "$event" ]; then
        continue
    fi
    # 事件格式: category:event
    event_path="$TRACE_ROOT/events/${event//:/\/}/enable"
    if [[ -f "$event_path" ]]; then
        echo 1 > "$event_path"
        echo "已启用事件: $event"
        enabled_count=$((enabled_count + 1))
    else
        echo "警告：事件 $event 不可用，已跳过"
    fi
done

# 记录采集配置
echo "采集配置:" > "$OUTPUT_DIR/config.txt"
echo "  时长: $DURATION 秒" >> "$OUTPUT_DIR/config.txt"
echo "  CPU 范围: $CPU_MASK" >> "$OUTPUT_DIR/config.txt"
echo "  缓冲区大小: $BUFFER_SIZE_KB KB" >> "$OUTPUT_DIR/config.txt"
echo "  启用事件数: $enabled_count" >> "$OUTPUT_DIR/config.txt"
echo "  事件列表: ${ENABLED_EVENTS[*]}" >> "$OUTPUT_DIR/config.txt"
echo "  时间: $(date -Iseconds)" >> "$OUTPUT_DIR/config.txt"
echo "  追踪根目录: $TRACE_ROOT" >> "$OUTPUT_DIR/config.txt"

# 开始采集
echo "开始采集，持续 $DURATION 秒..."
echo "追踪根目录: $TRACE_ROOT"
echo "启用事件数: $enabled_count"
echo 1 > "$TRACE_ROOT/tracing_on"

sleep "$DURATION"

# 停止采集
echo 0 > "$TRACE_ROOT/tracing_on"
echo "采集完成"

# 保存原始数据
cp "$TRACE_ROOT/trace" "$OUTPUT_DIR/ftrace_raw.txt"

# 保存额外信息
cat /proc/interrupts > "$OUTPUT_DIR/interrupts.txt"
cat /proc/stat > "$OUTPUT_DIR/cpu_stat.txt"
ps aux --sort=-%cpu > "$OUTPUT_DIR/process_list.txt"
if [ -f "/proc/uptime" ]; then
    cat /proc/uptime > "$OUTPUT_DIR/uptime.txt"
fi

echo "数据已保存到 $OUTPUT_DIR"
echo "文件列表:"
ls -la "$OUTPUT_DIR/"
