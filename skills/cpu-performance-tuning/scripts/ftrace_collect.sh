#!/bin/bash
#
# ftrace 数据采集脚本
# 支持自定义事件、CPU 范围、采集时长
# 参考: MindStudio trace_record.py
#
# 特性:
#  - 采集前自动备份原配置
#  - 采集完成后自动恢复原配置
#  - 支持信号处理（Ctrl+C）
#  - 完善的异常处理和日志记录

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

# 全局变量
TRACE_ROOT=""
BACKUP_DIR=""
ORIGINAL_CONFIG=()
COLLECTING=0
EXIT_CODE=0

# 清理函数
cleanup() {
    local exit_code=$1
    
    # 如果正在采集，先停止
    if [ $COLLECTING -eq 1 ]; then
        echo ""
        echo "正在停止采集..."
        if [ -n "$TRACE_ROOT" ] && [ -f "$TRACE_ROOT/tracing_on" ]; then
            echo 0 > "$TRACE_ROOT/tracing_on" 2>/dev/null || true
        fi
        COLLECTING=0
    fi
    
    # 恢复原始配置
    restore_original_config
    
    if [ $exit_code -ne 0 ]; then
        echo "采集异常退出，退出码: $exit_code"
    else
        echo "清理完成"
    fi
    
    exit $exit_code
}

# 信号处理
trap 'cleanup 1' INT TERM HUP QUIT

# 备份原始配置
backup_original_config() {
    echo "备份原始 ftrace 配置..."
    
    # 记录原始配置
    ORIGINAL_CONFIG=(
        "tracing_on=$(cat "$TRACE_ROOT/tracing_on" 2>/dev/null || echo "0")"
        "buffer_size_kb=$(cat "$TRACE_ROOT/buffer_size_kb" 2>/dev/null || echo "1024")"
        "cpumask=$(cat "$TRACE_ROOT/tracing_cpumask" 2>/dev/null || echo "")"
        "trace_clock=$(cat "$TRACE_ROOT/trace_clock" 2>/dev/null || echo "local")"
        "current_tracer=$(cat "$TRACE_ROOT/current_tracer" 2>/dev/null || echo "nop")"
        "set_event=$(cat "$TRACE_ROOT/set_event" 2>/dev/null || echo "")"
        "events_enable=$(cat "$TRACE_ROOT/events/enable" 2>/dev/null || echo "0")"
    )
    
    # 保存到文件
    mkdir -p "$BACKUP_DIR"
    printf "%s\n" "${ORIGINAL_CONFIG[@]}" > "$BACKUP_DIR/original_config.txt"
    
    echo "原始配置已备份到 $BACKUP_DIR/original_config.txt"
}

# 恢复原始配置
restore_original_config() {
    # 如果没有备份，跳过
    if [ ${#ORIGINAL_CONFIG[@]} -eq 0 ] || [ -z "$TRACE_ROOT" ]; then
        return
    fi
    
    echo "恢复原始 ftrace 配置..."
    
    # 停止追踪
    echo 0 > "$TRACE_ROOT/tracing_on" 2>/dev/null || true
    
    # 恢复各项配置
    for config in "${ORIGINAL_CONFIG[@]}"; do
        key="${config%%=*}"
        value="${config#*=}"
        
        case "$key" in
            tracing_on)
                echo "$value" > "$TRACE_ROOT/tracing_on" 2>/dev/null || true
                ;;
            buffer_size_kb)
                echo "$value" > "$TRACE_ROOT/buffer_size_kb" 2>/dev/null || true
                ;;
            cpumask)
                if [ -n "$value" ]; then
                    echo "$value" > "$TRACE_ROOT/tracing_cpumask" 2>/dev/null || true
                fi
                ;;
            trace_clock)
                echo "$value" > "$TRACE_ROOT/trace_clock" 2>/dev/null || true
                ;;
            current_tracer)
                echo "$value" > "$TRACE_ROOT/current_tracer" 2>/dev/null || true
                ;;
            set_event)
                echo "$value" > "$TRACE_ROOT/set_event" 2>/dev/null || true
                ;;
            events_enable)
                echo "$value" > "$TRACE_ROOT/events/enable" 2>/dev/null || true
                ;;
        esac
    done
    
    echo "原始配置已恢复"
}

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
                for sched_event in "${SCHED_EVENTS[@]}"; do
                    ENABLED_EVENTS=("${ENABLED_EVENTS[@]/$sched_event}")
                done
            fi
            shift 2
            ;;
        --irq)
            if [ "$2" = "0" ]; then
                for irq_event in "${IRQ_EVENTS[@]}"; do
                    ENABLED_EVENTS=("${ENABLED_EVENTS[@]/$irq_event}")
                done
            fi
            shift 2
            ;;
        --futex)
            if [ "$2" = "1" ]; then
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
            echo "错误：未知参数: $1"
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
BACKUP_DIR="$OUTPUT_DIR/backup"

# 挂载 debugfs（如未挂载）
if [ ! -d /sys/kernel/debug ]; then
    echo "挂载 debugfs..."
    mount -t debugfs nodev /sys/kernel/debug 2>/dev/null || {
        echo "错误：无法挂载 debugfs"
        exit 1
    }
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

# 检查追踪目录是否可用
if [ ! -f "$TRACE_ROOT/tracing_on" ] || [ ! -f "$TRACE_ROOT/set_event" ]; then
    echo "错误：追踪目录不可写或不完整"
    exit 1
fi

# 备份原始配置
backup_original_config

# 开始配置和采集
echo "================================"
echo "      ftrace 采集配置"
echo "================================="
echo "追踪根目录: $TRACE_ROOT"
echo "采集时长: $DURATION 秒"
echo "CPU 范围: $CPU_MASK"
echo "缓冲区大小: $BUFFER_SIZE_KB KB"
echo "================================="

# 停止已有追踪
echo "停止已有追踪..."
echo 0 > "$TRACE_ROOT/tracing_on"

# 清除旧数据
echo "清除旧数据..."
echo "" > "$TRACE_ROOT/trace"

# 设置缓冲区大小
echo "设置缓冲区大小为 $BUFFER_SIZE_KB KB..."
echo "$BUFFER_SIZE_KB" > "$TRACE_ROOT/buffer_size_kb"

# 设置 CPU 范围
if [[ "$CPU_MASK" != "all" ]]; then
    echo "设置 CPU 范围为 $CPU_MASK..."
    echo "$CPU_MASK" > "$TRACE_ROOT/tracing_cpumask"
fi

# 设置追踪时钟
echo "设置追踪时钟为 mono_raw..."
echo "mono_raw" > "$TRACE_ROOT/trace_clock"

# 设置当前追踪器为 nop
echo "设置追踪器为 nop..."
echo "nop" > "$TRACE_ROOT/current_tracer"

# 清除事件
echo "清除已有事件..."
echo "" > "$TRACE_ROOT/set_event"
echo 0 > "$TRACE_ROOT/events/enable"

# 启用事件
echo "启用事件..."
enabled_count=0
disabled_events=()
for event in "${ENABLED_EVENTS[@]}"; do
    if [ -z "$event" ]; then
        continue
    fi
    event_path="$TRACE_ROOT/events/${event//:/\/}/enable"
    if [[ -f "$event_path" ]]; then
        echo 1 > "$event_path"
        enabled_count=$((enabled_count + 1))
    else
        disabled_events+=("$event")
    fi
done

# 显示启用结果
echo "已启用 $enabled_count 个事件"
if [ ${#disabled_events[@]} -gt 0 ]; then
    echo "以下事件不可用（已跳过）: ${disabled_events[*]}"
fi

# 记录采集配置
echo "记录采集配置..."
{
    echo "================================="
    echo "      ftrace 采集配置"
    echo "================================="
    echo "开始时间: $(date -Iseconds)"
    echo "追踪根目录: $TRACE_ROOT"
    echo "采集时长: $DURATION 秒"
    echo "CPU 范围: $CPU_MASK"
    echo "缓冲区大小: $BUFFER_SIZE_KB KB"
    echo "启用事件数: $enabled_count"
    echo "启用事件列表:"
    for event in "${ENABLED_EVENTS[@]}"; do
        [ -n "$event" ] && echo "  - $event"
    done
    if [ ${#disabled_events[@]} -gt 0 ]; then
        echo "不可用事件:"
        for event in "${disabled_events[@]}"; do
            echo "  - $event"
        done
    fi
} > "$OUTPUT_DIR/config.txt"

# 开始采集
echo "================================="
echo "开始采集，持续 $DURATION 秒..."
echo "按 Ctrl+C 可提前停止"
echo "================================="
COLLECTING=1
echo 1 > "$TRACE_ROOT/tracing_on"

# 显示倒计时
if [ $DURATION -gt 0 ]; then
    remaining=$DURATION
    while [ $remaining -gt 0 ] && [ $COLLECTING -eq 1 ]; do
        echo -ne "剩余时间: ${remaining}s\033[0K\r"
        sleep 1
        remaining=$((remaining - 1))
    done
    echo ""
else
    echo "长期采集模式，按 Ctrl+C 停止..."
    while [ $COLLECTING -eq 1 ]; do
        sleep 1
    done
fi

# 停止采集
echo "停止采集..."
COLLECTING=0
echo 0 > "$TRACE_ROOT/tracing_on"

# 保存原始数据
echo "保存采集数据..."
cp "$TRACE_ROOT/trace" "$OUTPUT_DIR/ftrace_raw.txt"

# 保存额外信息
echo "保存系统信息..."
cat /proc/interrupts > "$OUTPUT_DIR/interrupts.txt"
cat /proc/stat > "$OUTPUT_DIR/cpu_stat.txt"
ps aux --sort=-%cpu > "$OUTPUT_DIR/process_list.txt"
if [ -f "/proc/uptime" ]; then
    cat /proc/uptime > "$OUTPUT_DIR/uptime.txt"
fi

# 检查是否有数据丢失
if [ -f "$TRACE_ROOT/per_cpu" ]; then
    echo "检查数据完整性..."
    total_lost=0
    for cpu_dir in "$TRACE_ROOT/per_cpu"/cpu*; do
        if [ -d "$cpu_dir" ] && [ -f "$cpu_dir/stats" ]; then
            lost=$(grep -E "overrun|dropped events" "$cpu_dir/stats" 2>/dev/null | awk '{sum+=$1} END {print sum}')
            total_lost=$((total_lost + lost))
        fi
    done
    if [ $total_lost -gt 0 ]; then
        echo "警告: 检测到 $total_lost 个丢失的事件，考虑增大缓冲区大小"
    else
        echo "数据完整性检查通过"
    fi
fi

# 记录采集结束时间
echo "结束时间: $(date -Iseconds)" >> "$OUTPUT_DIR/config.txt"

# 恢复原始配置
restore_original_config

# 输出结果
echo "================================="
echo "      采集完成"
echo "================================="
echo "数据保存到: $OUTPUT_DIR"
echo "文件列表:"
ls -la "$OUTPUT_DIR/"
echo "================================="
echo "事件统计: $(wc -l < "$OUTPUT_DIR/ftrace_raw.txt") 行"
echo "================================="

exit 0
