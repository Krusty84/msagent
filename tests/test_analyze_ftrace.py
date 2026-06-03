#!/usr/bin/env python3
"""
Unit tests for analyze_ftrace.py
"""

import unittest
import sqlite3
import os
import sys

# 添加脚本目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'skills', 'cpu-performance-tuning', 'scripts'))

from analyze_ftrace import (
    analyze_cpu_load,
    analyze_process_schedule,
    analyze_irq_distribution,
    analyze_irq_by_name,
    generate_report,
)


class TestAnalyzeFtrace(unittest.TestCase):
    """测试 ftrace 数据分析函数"""

    def setUp(self):
        """在每个测试前设置临时数据库"""
        self.conn = sqlite3.connect(':memory:')
        self._create_test_tables()
        self._insert_test_data()

    def tearDown(self):
        """在每个测试后关闭数据库连接"""
        self.conn.close()

    def _create_test_tables(self):
        """创建测试所需的表"""
        cursor = self.conn.cursor()

        # sched_events 表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS sched_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            cpu INTEGER,
            comm TEXT,
            pid INTEGER,
            event TEXT,
            prev_comm TEXT,
            prev_pid INTEGER,
            prev_prio INTEGER,
            prev_state TEXT,
            next_comm TEXT,
            next_pid INTEGER,
            next_prio INTEGER
        )
        ''')

        # irq_events 表
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

        # raw_events 表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            cpu INTEGER,
            comm TEXT,
            pid INTEGER,
            flags TEXT,
            event TEXT,
            event_full TEXT,
            details TEXT
        )
        ''')

        self.conn.commit()

    def _insert_test_data(self):
        """插入测试数据"""
        cursor = self.conn.cursor()

        # 插入调度事件
        for i in range(10):
            cursor.execute(
                '''
            INSERT INTO sched_events (timestamp, cpu, next_comm, next_pid)
            VALUES (?, ?, ?, ?)
            ''',
                (i * 0.1, i % 4, f'process_{i % 3}', 1000 + i),
            )

        # 插入中断事件
        for i in range(20):
            cursor.execute(
                '''
            INSERT INTO irq_events (timestamp, cpu, irq, name, type)
            VALUES (?, ?, ?, ?, ?)
            ''',
                (i * 0.05, i % 4, 40 + (i % 3), f'irq_{i % 2}', 'entry'),
            )

        self.conn.commit()

    def test_analyze_cpu_load(self):
        """测试分析 CPU 负载分布"""
        results = analyze_cpu_load(self.conn)

        self.assertIsInstance(results, list)
        self.assertTrue(len(results) > 0)

        # 检查结果格式
        for result in results:
            self.assertIn('cpu', result)
            self.assertIn('switch_count', result)
            self.assertIsInstance(result['cpu'], int)
            self.assertIsInstance(result['switch_count'], int)

    def test_analyze_process_schedule(self):
        """测试分析进程调度频率"""
        results = analyze_process_schedule(self.conn)

        self.assertIsInstance(results, list)

        # 检查结果格式
        for result in results:
            self.assertIn('comm', result)
            self.assertIn('wakeup_count', result)
            self.assertIsInstance(result['wakeup_count'], int)

    def test_analyze_irq_distribution(self):
        """测试分析中断分布"""
        results = analyze_irq_distribution(self.conn)

        self.assertIsInstance(results, list)
        self.assertTrue(len(results) > 0)

        # 检查结果格式
        for result in results:
            self.assertIn('cpu', result)
            self.assertIn('irq_count', result)
            self.assertIsInstance(result['irq_count'], int)

    def test_analyze_irq_by_name(self):
        """测试按中断名称分析"""
        results = analyze_irq_by_name(self.conn)

        self.assertIsInstance(results, list)

        # 检查结果格式
        for result in results:
            self.assertIn('name', result)
            self.assertIn('count', result)
            self.assertIsInstance(result['count'], int)

    def test_generate_report(self):
        """测试生成分析报告"""
        # 创建临时输出文件
        import tempfile

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            temp_path = f.name

        try:
            report = generate_report(self.conn, temp_path)

            # 检查报告结构
            self.assertIn('analysis', report)
            self.assertIn('recommendations', report)

            # 检查分析结果
            self.assertIn('cpu_load', report['analysis'])
            self.assertIn('process_schedule', report['analysis'])
            self.assertIn('irq_distribution', report['analysis'])
            self.assertIn('irq_by_name', report['analysis'])
            self.assertIn('switch_delay', report['analysis'])

            # 检查报告文件是否生成
            self.assertTrue(os.path.exists(temp_path))

            # 检查报告文件内容
            import json

            with open(temp_path, 'r', encoding='utf-8') as f:
                report_data = json.load(f)
            self.assertIn('analysis', report_data)

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_generate_report_without_output(self):
        """测试生成报告但不指定输出文件"""
        report = generate_report(self.conn, None)

        # 检查报告结构
        self.assertIn('analysis', report)
        self.assertIn('recommendations', report)

    def test_report_recommendations(self):
        """测试报告中的建议生成"""
        # 先清空数据
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM sched_events')
        cursor.execute('DELETE FROM irq_events')

        # 插入极端数据（CPU 0 有大量事件，CPU 1 只有少量）
        for i in range(1000):
            cursor.execute(
                'INSERT INTO sched_events (timestamp, cpu, next_comm) VALUES (?, ?, ?)',
                (i * 0.1, 0 if i < 990 else 1, 'process_0'),
            )

        self.conn.commit()

        report = generate_report(self.conn, None)

        # 应该有警告建议
        self.assertTrue(len(report['recommendations']) > 0)
        self.assertIn('CPU 负载分布不均衡', str(report['recommendations']))


class TestReportSummary(unittest.TestCase):
    """测试报告摘要打印"""

    def setUp(self):
        """设置测试数据"""
        self.sample_report = {
            'analysis': {
                'cpu_load': [
                    {'cpu': 0, 'switch_count': 100},
                    {'cpu': 1, 'switch_count': 80},
                    {'cpu': 2, 'switch_count': 60},
                ],
                'process_schedule': [
                    {'comm': 'bash', 'wakeup_count': 150},
                    {'comm': 'python', 'wakeup_count': 100},
                ],
                'irq_distribution': [
                    {'cpu': 0, 'irq_count': 200},
                    {'cpu': 1, 'irq_count': 150},
                ],
                'irq_by_name': [
                    {'name': 'eth0', 'count': 180},
                    {'name': 'timer', 'count': 120},
                ],
                'switch_delay': [
                    {'comm': 'bash', 'avg_delay_ms': 0.5, 'max_delay_ms': 2.0},
                ],
            },
            'recommendations': [
                {'severity': 'info', 'message': 'Test recommendation', 'suggestion': 'Test suggestion'}
            ],
        }

    def test_summary_print(self):
        """测试打印摘要不会报错"""
        from analyze_ftrace import print_summary
        import io
        from contextlib import redirect_stdout

        # 捕获输出
        f = io.StringIO()
        with redirect_stdout(f):
            print_summary(self.sample_report)

        output = f.getvalue()

        # 检查输出是否包含关键信息
        self.assertIn('CPU 负载分布', output)
        self.assertIn('进程调度频率', output)
        self.assertIn('中断分布', output)
        self.assertIn('优化建议', output)


if __name__ == '__main__':
    unittest.main()
