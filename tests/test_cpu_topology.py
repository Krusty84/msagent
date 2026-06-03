#!/usr/bin/env python3
"""
Unit tests for cpu_topology.py
"""

import unittest
import os
import sys

# 添加脚本目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'skills', 'cpu-performance-tuning', 'scripts'))

from cpu_topology import (
    run_command,
    parse_lscpu_output,
    parse_cpu_range,
    collect_cpu_topology,
    collect_npu_topology,
    collect_interrupt_info,
    generate_binding_plan,
)


class TestCpuTopologyParsing(unittest.TestCase):
    """测试 CPU 拓扑解析函数"""

    def test_parse_lscpu_output(self):
        """测试解析 lscpu 输出"""
        sample_output = """Architecture:          x86_64
CPU op-mode(s):        32-bit, 64-bit
Byte Order:            Little Endian
CPU(s):                32
On-line CPU(s) list:   0-31
Thread(s) per core:    2
Core(s) per socket:    8
Socket(s):             2
NUMA node(s):          2
Vendor ID:             GenuineIntel
CPU family:            6
Model:                 85
Model name:            Intel(R) Xeon(R) Gold 6230 CPU @ 2.10GHz
Stepping:              7
CPU MHz:               1000.000
CPU max MHz:           3900.000
CPU min MHz:           1000.000
BogoMIPS:              4200.00
Virtualization:        VT-x
L1d cache:             32K
L1i cache:             32K
L2 cache:              1024K
L3 cache:              28160K
NUMA node0 CPU(s):     0-7,16-23
NUMA node1 CPU(s):     8-15,24-31"""

        result = parse_lscpu_output(sample_output)

        self.assertEqual(result['architecture'], 'x86_64')
        self.assertEqual(result['cpu(s)'], '32')
        self.assertEqual(result['thread(s)_per_core'], '2')
        self.assertEqual(result['core(s)_per_socket'], '8')
        self.assertEqual(result['socket(s)'], '2')
        self.assertEqual(result['numa_node(s)'], '2')

    def test_parse_cpu_range_single(self):
        """测试解析单个 CPU"""
        result = parse_cpu_range("0")
        self.assertEqual(result, [0])

    def test_parse_cpu_range_contiguous(self):
        """测试解析连续 CPU 范围"""
        result = parse_cpu_range("0-7")
        self.assertEqual(result, [0, 1, 2, 3, 4, 5, 6, 7])

    def test_parse_cpu_range_multiple(self):
        """测试解析多个不连续范围"""
        result = parse_cpu_range("0-7,16-23")
        expected = list(range(8)) + list(range(16, 24))
        self.assertEqual(result, expected)

    def test_parse_cpu_range_mixed(self):
        """测试解析混合格式"""
        result = parse_cpu_range("0,2-4,7")
        self.assertEqual(result, [0, 2, 3, 4, 7])

    def test_parse_cpu_range_empty(self):
        """测试解析空字符串"""
        result = parse_cpu_range("")
        self.assertEqual(result, [])

    def test_run_command_success(self):
        """测试执行成功的命令"""
        result = run_command("echo 'hello'")
        self.assertEqual(result, 'hello')

    def test_run_command_failure(self):
        """测试执行失败的命令"""
        result = run_command("nonexistent_command_xyz_123")
        self.assertEqual(result, '')


class TestCpuTopologyCollection(unittest.TestCase):
    """测试 CPU 拓扑采集函数"""

    def test_collect_cpu_topology_structure(self):
        """测试采集的 CPU 拓扑结构"""
        topology = collect_cpu_topology()

        self.assertIsInstance(topology, dict)
        self.assertIn('sockets', topology)
        self.assertIn('cores_per_socket', topology)
        self.assertIn('threads_per_core', topology)
        self.assertIn('total_cpus', topology)
        self.assertIn('numa_nodes', topology)

        self.assertIsInstance(topology['sockets'], int)
        self.assertIsInstance(topology['cores_per_socket'], int)
        self.assertIsInstance(topology['threads_per_core'], int)
        self.assertIsInstance(topology['total_cpus'], int)
        self.assertIsInstance(topology['numa_nodes'], list)

    def test_collect_cpu_topology_numa_structure(self):
        """测试 NUMA 节点结构"""
        topology = collect_cpu_topology()

        for node in topology['numa_nodes']:
            self.assertIn('node_id', node)
            self.assertIn('cpus', node)
            self.assertIn('memory', node)

            self.assertIsInstance(node['node_id'], int)
            self.assertIsInstance(node['cpus'], list)
            self.assertIsInstance(node['memory'], int)

    def test_collect_npu_topology_structure(self):
        """测试 NPU 拓扑结构"""
        topology = collect_npu_topology()

        self.assertIsInstance(topology, dict)
        self.assertIn('devices', topology)
        self.assertIn('affinities', topology)
        self.assertIn('has_ub_bus', topology)

        self.assertIsInstance(topology['devices'], int)
        self.assertIsInstance(topology['affinities'], list)
        self.assertIsInstance(topology['has_ub_bus'], bool)

    def test_collect_interrupt_info_structure(self):
        """测试中断信息结构"""
        interrupts = collect_interrupt_info()

        self.assertIsInstance(interrupts, list)

        for irq in interrupts:
            self.assertIn('irq', irq)
            self.assertIn('handler', irq)
            self.assertIn('cpu_counts', irq)
            self.assertIn('total', irq)

            self.assertIsInstance(irq['irq'], int)
            self.assertIsInstance(irq['handler'], str)
            self.assertIsInstance(irq['cpu_counts'], list)
            self.assertIsInstance(irq['total'], int)


class TestBindingPlanGeneration(unittest.TestCase):
    """测试绑核方案生成"""

    def test_generate_binding_plan_structure(self):
        """测试生成的绑核方案结构"""
        cpu_topo = {
            'numa_nodes': [
                {'node_id': 0, 'cpus': list(range(16)), 'memory': 68719476736},
                {'node_id': 1, 'cpus': list(range(16, 32)), 'memory': 68719476736},
            ]
        }

        npu_topo = {
            'devices': 2,
            'affinities': [
                {'npu_id': 0, 'preferred_numa': 0, 'socket': 0},
                {'npu_id': 1, 'preferred_numa': 1, 'socket': 1},
            ],
            'has_ub_bus': False,
        }

        interrupts = []

        plan = generate_binding_plan(cpu_topo, npu_topo, interrupts)

        self.assertIsInstance(plan, dict)
        self.assertIn('version', plan)
        self.assertIn('generated_at', plan)
        self.assertIn('process_bindings', plan)
        self.assertIn('irq_bindings', plan)
        self.assertIn('system_config', plan)

        self.assertIsInstance(plan['process_bindings'], list)
        self.assertIsInstance(plan['irq_bindings'], list)
        self.assertIsInstance(plan['system_config'], dict)

    def test_generate_binding_plan_with_interrupts(self):
        """测试带中断信息的绑核方案"""
        cpu_topo = {'numa_nodes': [{'node_id': 0, 'cpus': list(range(16)), 'memory': 68719476736}]}

        npu_topo = {'devices': 1, 'affinities': [{'npu_id': 0, 'preferred_numa': 0, 'socket': 0}], 'has_ub_bus': False}

        interrupts = [{'irq': 45, 'handler': 'eth0', 'cpu_counts': [100, 200, 300, 400], 'total': 1000}]

        plan = generate_binding_plan(cpu_topo, npu_topo, interrupts)

        self.assertTrue(len(plan['irq_bindings']) > 0)

    def test_generate_binding_plan_empty_cpu(self):
        """测试空 CPU 列表的绑核方案"""
        cpu_topo = {'numa_nodes': [{'node_id': 0, 'cpus': [], 'memory': 0}]}

        npu_topo = {'devices': 0, 'affinities': [], 'has_ub_bus': False}
        interrupts = []

        plan = generate_binding_plan(cpu_topo, npu_topo, interrupts)

        # 应该不会出错，但绑核方案可能为空
        self.assertIsInstance(plan, dict)
        self.assertEqual(len(plan['process_bindings']), 0)


class TestSystemConfig(unittest.TestCase):
    """测试系统配置建议"""

    def test_binding_plan_system_config(self):
        """测试绑核方案中的系统配置"""
        cpu_topo = {'numa_nodes': [{'node_id': 0, 'cpus': list(range(8)), 'memory': 34359738368}]}

        npu_topo = {'devices': 1, 'affinities': [], 'has_ub_bus': True}
        interrupts = []

        plan = generate_binding_plan(cpu_topo, npu_topo, interrupts)

        self.assertIn('system_config', plan)
        self.assertIn('irqbalance_enabled', plan['system_config'])
        self.assertIn('hugepages_enabled', plan['system_config'])
        self.assertIn('transparent_hugepage', plan['system_config'])

        # 检查默认配置
        self.assertFalse(plan['system_config']['irqbalance_enabled'])
        self.assertTrue(plan['system_config']['hugepages_enabled'])
        self.assertEqual(plan['system_config']['transparent_hugepage'], 'always')


if __name__ == '__main__':
    unittest.main()
