#!/usr/bin/env python3
"""
ftrace 数据分析脚本
"""

import sqlite3
import argparse
import json
from collections import defaultdict


def analyze_cpu_load(conn):
    """分析 CPU 负载分布"""
    cursor = conn.cursor()
    cursor.execute('''
    SELECT cpu, COUNT(*) as switch_count
    FROM sched_events
    GROUP BY cpu
    ORDER BY switch_count DESC
    ''')
    
    results = []
    for row in cursor.fetchall():
        results.append({
            'cpu': row[0],
            'switch_count': row[1]
        })
    
    return results


def analyze_process_schedule(conn):
    """分析进程调度频率"""
    cursor = conn.cursor()
    cursor.execute('''
    SELECT next_comm, COUNT(*) as wakeup_count
    FROM sched_events
    GROUP BY next_comm
    ORDER BY wakeup_count DESC
    LIMIT 10
    ''')
    
    results = []
    for row in cursor.fetchall():
        results.append({
            'comm': row[0],
            'wakeup_count': row[1]
        })
    
    return results


def analyze_irq_distribution(conn):
    """分析中断分布"""
    cursor = conn.cursor()
    cursor.execute('''
    SELECT cpu, COUNT(*) as irq_count
    FROM irq_events
    WHERE type = 'entry'
    GROUP BY cpu
    ORDER BY irq_count DESC
    ''')
    
    results = []
    for row in cursor.fetchall():
        results.append({
            'cpu': row[0],
            'irq_count': row[1]
        })
    
    return results


def analyze_irq_by_name(conn):
    """按中断名称分析"""
    cursor = conn.cursor()
    cursor.execute('''
    SELECT name, COUNT(*) as count
    FROM irq_events
    WHERE type = 'entry' AND name IS NOT NULL
    GROUP BY name
    ORDER BY count DESC
    LIMIT 10
    ''')
    
    results = []
    for row in cursor.fetchall():
        results.append({
            'name': row[0],
            'count': row[1]
        })
    
    return results


def analyze_switch_delay(conn):
    """分析调度切换延迟"""
    cursor = conn.cursor()
    cursor.execute('''
    SELECT 
        prev_comm,
        AVG(next_timestamp - timestamp) as avg_delay,
        MAX(next_timestamp - timestamp) as max_delay
    FROM (
        SELECT 
            prev_comm,
            timestamp,
            (SELECT MIN(timestamp) FROM sched_events se2 
             WHERE se2.timestamp > se1.timestamp AND se2.prev_pid = se1.next_pid) as next_timestamp
        FROM sched_events se1
        WHERE next_pid != 0
        LIMIT 1000
    )
    WHERE next_timestamp IS NOT NULL
    GROUP BY prev_comm
    ORDER BY avg_delay DESC
    LIMIT 10
    ''')
    
    results = []
    for row in cursor.fetchall():
        results.append({
            'comm': row[0],
            'avg_delay_ms': row[1] * 1000,
            'max_delay_ms': row[2] * 1000
        })
    
    return results


def generate_report(conn, output_path):
    """生成分析报告"""
    report = {
        'analysis': {
            'cpu_load': analyze_cpu_load(conn),
            'process_schedule': analyze_process_schedule(conn),
            'irq_distribution': analyze_irq_distribution(conn),
            'irq_by_name': analyze_irq_by_name(conn),
            'switch_delay': analyze_switch_delay(conn)
        },
        'recommendations': []
    }
    
    # 生成优化建议
    cpu_load = report['analysis']['cpu_load']
    if cpu_load:
        max_load = cpu_load[0]['switch_count']
        min_load = cpu_load[-1]['switch_count']
        if max_load > 2 * min_load:
            report['recommendations'].append({
                'severity': 'warning',
                'message': 'CPU 负载分布不均衡',
                'detail': f'CPU {cpu_load[0]["cpu"]} 负载最高（{max_load}次切换），CPU {cpu_load[-1]["cpu"]} 负载最低（{min_load}次切换）',
                'suggestion': '考虑重新分配进程绑核，均衡各 CPU 负载'
            })
    
    irq_dist = report['analysis']['irq_distribution']
    if irq_dist and irq_dist[0]['irq_count'] > 100:
        report['recommendations'].append({
            'severity': 'info',
            'message': '中断分布集中',
            'detail': f'CPU {irq_dist[0]["cpu"]} 中断次数最多（{irq_dist[0]["irq_count"]}次）',
            'suggestion': '考虑将高频中断绑定到专用 CPU 核'
        })
    
    # 保存报告
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    return report


def print_summary(report):
    """打印分析摘要"""
    print("=" * 60)
    print("          ftrace 数据分析报告")
    print("=" * 60)
    
    print("\n【CPU 负载分布 Top 5】")
    for item in report['analysis']['cpu_load'][:5]:
        print(f"  CPU {item['cpu']:3d}: {item['switch_count']:6d} 次切换")
    
    print("\n【进程调度频率 Top 5】")
    for item in report['analysis']['process_schedule'][:5]:
        print(f"  {item['comm']:20s}: {item['wakeup_count']:6d} 次唤醒")
    
    print("\n【中断分布 Top 5】")
    for item in report['analysis']['irq_distribution'][:5]:
        print(f"  CPU {item['cpu']:3d}: {item['irq_count']:6d} 次中断")
    
    print("\n【中断名称统计 Top 5】")
    for item in report['analysis']['irq_by_name'][:5]:
        print(f"  {item['name']:20s}: {item['count']:6d} 次")
    
    print("\n【调度延迟 Top 5】")
    for item in report['analysis']['switch_delay'][:5]:
        print(f"  {item['comm']:20s}: 平均 {item['avg_delay_ms']:.2f}ms  最大 {item['max_delay_ms']:.2f}ms")
    
    print("\n【优化建议】")
    for rec in report['recommendations']:
        print(f"  [{rec['severity'].upper()}] {rec['message']}")
        print(f"     → {rec['suggestion']}")
    
    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description='分析 ftrace SQLite 数据库')
    parser.add_argument('db_path', help='SQLite 数据库路径')
    parser.add_argument('-o', '--output', help='输出报告路径（JSON格式）')
    args = parser.parse_args()
    
    # 连接数据库
    conn = sqlite3.connect(args.db_path)
    
    # 生成报告
    report = generate_report(conn, args.output or 'ftrace_report.json')
    
    # 打印摘要
    print_summary(report)
    
    conn.close()


if __name__ == '__main__':
    main()
