#!/usr/bin/env python3
"""
ftrace 数据转换为 SQLite 数据库
参考: MindStudio trace_record.py
"""

import sqlite3
import re
import os
import argparse
from typing import Optional, Dict, Any


def parse_ftrace_line(line):
    """解析单条 ftrace 记录"""
    # 格式示例：
    # <idle>-0     [000] d...  1234.567890: sched:sched_switch: prev_comm=swapper/0 prev_pid=0 prev_prio=120 prev_state=R ==> next_comm=bash next_pid=1234 next_prio=120
    # 支持带类别和不带类别的格式
    pattern = r'^(\S+)-(\d+)\s+\[(\d+)\]\s+(\S+)\s+(\d+\.\d+):\s*(\S+):\s*(.+)$'
    match = re.match(pattern, line)
    if not match:
        return None
    
    event_full = match.group(6)
    # 分离类别和事件名
    category = None
    event_name = event_full
    if ':' in event_full:
        parts = event_full.split(':', 1)
        category = parts[0]
        event_name = parts[1]
    
    return {
        'comm': match.group(1),
        'pid': int(match.group(2)),
        'cpu': int(match.group(3)),
        'flags': match.group(4),
        'timestamp': float(match.group(5)),
        'category': category,
        'event': event_name,
        'event_full': event_full,
        'details': match.group(7)
    }


def parse_sched_switch(details):
    """解析 sched_switch 事件详情"""
    result = {}
    # prev_comm=swapper/0 prev_pid=0 prev_prio=120 prev_state=R ==> next_comm=bash next_pid=1234 next_prio=120
    parts = details.split(' ==> ')
    if len(parts) == 2:
        for part in parts[0].split():
            if '=' in part:
                key, value = part.split('=', 1)
                result['prev_' + key] = value if not value.isdigit() else int(value)
        for part in parts[1].split():
            if '=' in part:
                key, value = part.split('=', 1)
                result['next_' + key] = value if not value.isdigit() else int(value)
    return result


def parse_sched_wakeup(details):
    """解析 sched_wakeup/sched_waking 事件详情"""
    result = {}
    # comm=bash pid=1234 prio=120 target_cpu=000
    for part in details.split():
        if '=' in part:
            key, value = part.split('=', 1)
            if value.isdigit():
                result[key] = int(value)
            elif re.match(r'^0x[0-9a-fA-F]+$', value):
                result[key] = int(value, 16)
            else:
                result[key] = value
    return result


def parse_sched_stat_runtime(details):
    """解析 sched_stat_runtime 事件详情"""
    result = {}
    # comm=bash pid=1234 runtime=123456789 [ns] vruntime=123456789 [ns]
    parts = details.split()
    i = 0
    while i < len(parts):
        if '=' in parts[i]:
            key, value = parts[i].split('=', 1)
            if i + 2 < len(parts) and parts[i + 2] == '[ns]':
                result[key] = int(value)
                i += 3
            else:
                result[key] = int(value) if value.isdigit() else value
                i += 1
        else:
            i += 1
    return result


def parse_sched_process_fork(details):
    """解析 sched_process_fork 事件详情"""
    result = {}
    # parent_comm=bash parent_pid=1234 child_comm=bash child_pid=5678
    for part in details.split():
        if '=' in part:
            key, value = part.split('=', 1)
            result[key] = int(value) if value.isdigit() else value
    return result


def parse_irq_handler(details):
    """解析 irq_handler 事件详情"""
    result = {}
    # irq=45 name=eth0
    for part in details.split():
        if '=' in part:
            key, value = part.split('=', 1)
            result[key] = int(value) if value.isdigit() else value
    return result


def parse_softirq(details):
    """解析 softirq 事件详情"""
    result = {}
    # vec=1 (TIMER)
    parts = details.split()
    for part in parts:
        if '=' in part:
            key, value = part.split('=', 1)
            result[key] = int(value) if value.isdigit() else value
        elif part.startswith('(') and part.endswith(')'):
            result['action'] = part[1:-1]
    return result


def parse_syscall_futex(details):
    """解析 futex 系统调用详情"""
    result = {}
    # uaddr=0x12345678 op=0 val=0x1234
    for part in details.split():
        if '=' in part:
            key, value = part.split('=', 1)
            if value.startswith('0x'):
                try:
                    result[key] = int(value, 16)
                except ValueError:
                    result[key] = value
            elif value.isdigit():
                result[key] = int(value)
            else:
                result[key] = value
    return result


def create_tables(conn):
    """创建数据库表"""
    cursor = conn.cursor()
    
    # 原始事件表（用于存储所有事件）
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS raw_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        cpu INTEGER,
        comm TEXT,
        pid INTEGER,
        flags TEXT,
        category TEXT,
        event TEXT,
        event_full TEXT,
        details TEXT
    )
    ''')
    
    # 调度事件表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sched_switch (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        cpu INTEGER,
        comm TEXT,
        pid INTEGER,
        prev_comm TEXT,
        prev_pid INTEGER,
        prev_prio INTEGER,
        prev_state TEXT,
        next_comm TEXT,
        next_pid INTEGER,
        next_prio INTEGER
    )
    ''')
    
    # 唤醒事件表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sched_wakeup (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        cpu INTEGER,
        comm TEXT,
        pid INTEGER,
        wakee_comm TEXT,
        wakee_pid INTEGER,
        prio INTEGER,
        target_cpu INTEGER
    )
    ''')
    
    # 调度统计事件表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sched_stat_runtime (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        cpu INTEGER,
        comm TEXT,
        pid INTEGER,
        runtime INTEGER,
        vruntime INTEGER
    )
    ''')
    
    # 进程创建/退出事件表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sched_process (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        cpu INTEGER,
        event TEXT,
        parent_comm TEXT,
        parent_pid INTEGER,
        child_comm TEXT,
        child_pid INTEGER,
        comm TEXT,
        pid INTEGER
    )
    ''')
    
    # 中断事件表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS irq_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        cpu INTEGER,
        irq INTEGER,
        name TEXT,
        type TEXT
    )
    ''')
    
    # 软中断事件表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS softirq_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        cpu INTEGER,
        vec INTEGER,
        action TEXT,
        type TEXT
    )
    ''')
    
    # 锁竞争事件表
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS futex_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        cpu INTEGER,
        comm TEXT,
        pid INTEGER,
        uaddr INTEGER,
        op INTEGER,
        val INTEGER,
        type TEXT
    )
    ''')
    
    # CPU 统计视图
    cursor.execute('''
    CREATE VIEW IF NOT EXISTS cpu_stats AS
    SELECT 
        cpu,
        COUNT(*) as total_events,
        SUM(CASE WHEN event_full LIKE 'sched:sched_switch' THEN 1 ELSE 0 END) as switch_count,
        SUM(CASE WHEN event_full LIKE 'irq:irq_handler_%' THEN 1 ELSE 0 END) as irq_count,
        SUM(CASE WHEN event_full LIKE 'irq:softirq_%' THEN 1 ELSE 0 END) as softirq_count
    FROM raw_events
    GROUP BY cpu
    ORDER BY total_events DESC
    ''')
    
    conn.commit()


def insert_raw_event(conn, parsed):
    """插入原始事件"""
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO raw_events (timestamp, cpu, comm, pid, flags, category, event, event_full, details)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        parsed['comm'],
        parsed['pid'],
        parsed['flags'],
        parsed['category'],
        parsed['event'],
        parsed['event_full'],
        parsed['details']
    ))


def insert_sched_switch(conn, parsed):
    """插入 sched_switch 事件"""
    details = parse_sched_switch(parsed['details'])
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO sched_switch (
        timestamp, cpu, comm, pid,
        prev_comm, prev_pid, prev_prio, prev_state,
        next_comm, next_pid, next_prio
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        parsed['comm'],
        parsed['pid'],
        details.get('prev_comm'),
        details.get('prev_pid'),
        details.get('prev_prio'),
        details.get('prev_state'),
        details.get('next_comm'),
        details.get('next_pid'),
        details.get('next_prio')
    ))


def insert_sched_wakeup(conn, parsed):
    """插入 sched_wakeup/sched_waking 事件"""
    details = parse_sched_wakeup(parsed['details'])
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO sched_wakeup (
        timestamp, cpu, comm, pid,
        wakee_comm, wakee_pid, prio, target_cpu
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        parsed['comm'],
        parsed['pid'],
        details.get('comm'),
        details.get('pid'),
        details.get('prio'),
        details.get('target_cpu')
    ))


def insert_sched_stat_runtime(conn, parsed):
    """插入 sched_stat_runtime 事件"""
    details = parse_sched_stat_runtime(parsed['details'])
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO sched_stat_runtime (timestamp, cpu, comm, pid, runtime, vruntime)
    VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        details.get('comm'),
        details.get('pid'),
        details.get('runtime'),
        details.get('vruntime')
    ))


def insert_sched_process_fork(conn, parsed):
    """插入 sched_process_fork 事件"""
    details = parse_sched_process_fork(parsed['details'])
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO sched_process (
        timestamp, cpu, event,
        parent_comm, parent_pid, child_comm, child_pid
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        'fork',
        details.get('parent_comm'),
        details.get('parent_pid'),
        details.get('child_comm'),
        details.get('child_pid')
    ))


def insert_sched_process_exec(conn, parsed):
    """插入 sched_process_exec 事件"""
    details = parse_sched_process_fork(parsed['details'])
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO sched_process (timestamp, cpu, event, comm, pid)
    VALUES (?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        'exec',
        details.get('comm'),
        details.get('pid')
    ))


def insert_sched_process_exit(conn, parsed):
    """插入 sched_process_exit 事件"""
    details = parse_sched_process_fork(parsed['details'])
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO sched_process (timestamp, cpu, event, comm, pid)
    VALUES (?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        'exit',
        details.get('comm'),
        details.get('pid')
    ))


def insert_irq_event(conn, parsed, event_type):
    """插入中断事件"""
    details = parse_irq_handler(parsed['details'])
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO irq_events (timestamp, cpu, irq, name, type)
    VALUES (?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        details.get('irq'),
        details.get('name'),
        event_type
    ))


def insert_softirq_event(conn, parsed, event_type):
    """插入软中断事件"""
    details = parse_softirq(parsed['details'])
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO softirq_events (timestamp, cpu, vec, action, type)
    VALUES (?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        details.get('vec'),
        details.get('action'),
        event_type
    ))


def insert_futex_event(conn, parsed, event_type):
    """插入 futex 事件"""
    details = parse_syscall_futex(parsed['details'])
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO futex_events (timestamp, cpu, comm, pid, uaddr, op, val, type)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        parsed['timestamp'],
        parsed['cpu'],
        parsed['comm'],
        parsed['pid'],
        details.get('uaddr'),
        details.get('op'),
        details.get('val'),
        event_type
    ))


def main():
    parser = argparse.ArgumentParser(description='将 ftrace 数据转换为 SQLite 数据库')
    parser.add_argument('input_file', help='输入的 ftrace 原始文件')
    parser.add_argument('output_db', help='输出的 SQLite 数据库路径')
    args = parser.parse_args()
    
    # 检查输入文件
    if not os.path.exists(args.input_file):
        print(f"错误：输入文件 {args.input_file} 不存在")
        return
    
    # 创建数据库连接
    conn = sqlite3.connect(args.output_db)
    create_tables(conn)
    
    # 事件处理器映射
    event_handlers = {
        'sched_switch': insert_sched_switch,
        'sched_wakeup': insert_sched_wakeup,
        'sched_waking': insert_sched_wakeup,
        'sched_wakeup_new': insert_sched_wakeup,
        'sched_stat_runtime': insert_sched_stat_runtime,
        'sched_process_fork': insert_sched_process_fork,
        'sched_process_exec': insert_sched_process_exec,
        'sched_process_exit': insert_sched_process_exit,
        'irq_handler_entry': lambda c, p: insert_irq_event(c, p, 'entry'),
        'irq_handler_exit': lambda c, p: insert_irq_event(c, p, 'exit'),
        'softirq_raise': lambda c, p: insert_softirq_event(c, p, 'raise'),
        'softirq_entry': lambda c, p: insert_softirq_event(c, p, 'entry'),
        'softirq_exit': lambda c, p: insert_softirq_event(c, p, 'exit'),
        'sys_enter_futex': lambda c, p: insert_futex_event(c, p, 'enter'),
        'sys_exit_futex': lambda c, p: insert_futex_event(c, p, 'exit'),
    }
    
    # 读取并解析 ftrace 数据
    with open(args.input_file, 'r') as f:
        line_count = 0
        parsed_count = 0
        event_counts = {}
        
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            parsed = parse_ftrace_line(line)
            if parsed:
                insert_raw_event(conn, parsed)
                
                # 根据事件类型插入对应表
                event_name = parsed['event']
                if event_name in event_handlers:
                    try:
                        event_handlers[event_name](conn, parsed)
                        event_counts[event_name] = event_counts.get(event_name, 0) + 1
                    except Exception as e:
                        print(f"警告：解析事件 {event_name} 时出错: {e}")
                
                parsed_count += 1
            
            line_count += 1
            
            if line_count % 10000 == 0:
                print(f"已处理 {line_count} 行，解析 {parsed_count} 条事件")
    
    conn.commit()
    conn.close()
    
    print(f"\n转换完成！")
    print(f"输入文件: {args.input_file}")
    print(f"输出数据库: {args.output_db}")
    print(f"总行数: {line_count}")
    print(f"解析事件数: {parsed_count}")
    print("\n各事件类型统计:")
    for event, count in sorted(event_counts.items()):
        print(f"  {event}: {count} 条")


if __name__ == '__main__':
    main()
