#!/usr/bin/env python3
"""
CPU 拓扑信息采集与分析
"""

import subprocess
import os
import json
import argparse


def run_command(cmd):
    """执行命令并返回输出"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return ""


def parse_lscpu_output(output):
    """解析 lscpu 输出"""
    topology = {}
    for line in output.split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().lower().replace(" ", "_")
            value = value.strip()
            topology[key] = value
    return topology


def parse_cpu_range(cpu_str):
    """解析 CPU 范围字符串"""
    cpus = []
    if not cpu_str:
        return cpus
    parts = cpu_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-")
            start = start.strip()
            end = end.strip()
            if start and end:
                cpus.extend(range(int(start), int(end) + 1))
        else:
            cpus.append(int(part))
    return sorted(set(cpus))


def collect_cpu_topology():
    """采集 CPU 拓扑信息"""
    topology = {
        "sockets": 0,
        "cores_per_socket": 0,
        "threads_per_core": 0,
        "total_cpus": 0,
        "numa_nodes": [],
        "cpu_info": [],
    }
    
    output = run_command("lscpu")
    if output:
        lscpu = parse_lscpu_output(output)
        
        topology["sockets"] = int(lscpu.get("socket(s)", 1))
        topology["cores_per_socket"] = int(lscpu.get("core(s)_per_socket", 1))
        topology["threads_per_core"] = int(lscpu.get("thread(s)_per_core", 1))
        topology["total_cpus"] = int(lscpu.get("cpu(s)", 0))
        
        if "numa_node(s)" in lscpu:
            numa_count = int(lscpu["numa_node(s)"])
            for node_id in range(numa_count):
                node_info = {
                    "node_id": node_id,
                    "cpus": [],
                    "memory": 0,
                }
                cpu_list_path = f"/sys/devices/system/node/node{node_id}/cpulist"
                if os.path.exists(cpu_list_path):
                    with open(cpu_list_path, "r") as f:
                        cpus_str = f.read().strip()
                        if cpus_str:
                            node_info["cpus"] = parse_cpu_range(cpus_str)
                
                mem_info_path = f"/sys/devices/system/node/node{node_id}/meminfo"
                if os.path.exists(mem_info_path):
                    with open(mem_info_path, "r") as f:
                        for line in f:
                            if "MemTotal" in line:
                                parts = line.split()
                                if len(parts) >= 2:
                                    node_info["memory"] = int(parts[1]) * 1024
                                break
                
                topology["numa_nodes"].append(node_info)
    
    if not topology["numa_nodes"]:
        topology["numa_nodes"] = [{
            "node_id": 0,
            "cpus": list(range(topology["total_cpus"])),
            "memory": 0,
        }]
    
    return topology


def collect_npu_topology():
    """采集 NPU 拓扑信息"""
    topology = {
        "devices": 0,
        "affinities": [],
        "has_ub_bus": False,
    }
    
    output = run_command("npu-smi info -t topo 2>/dev/null || echo '[]'")
    if output and output.startswith("{"):
        try:
            data = json.loads(output)
            topology["devices"] = len(data.get("devices", []))
            for dev in data.get("devices", []):
                affinity = {
                    "npu_id": dev.get("id", 0),
                    "preferred_numa": dev.get("numa_node", 0),
                    "socket": dev.get("socket", 0),
                }
                topology["affinities"].append(affinity)
        except Exception:
            pass
    
    output2 = run_command("npu-smi info 2>/dev/null || echo ''")
    if output2:
        if "A5" in output2 or "UB" in output2 or "灵衢" in output2:
            topology["has_ub_bus"] = True
    
    return topology


def collect_interrupt_info():
    """采集中断信息"""
    interrupts = []
    if os.path.exists("/proc/interrupts"):
        with open("/proc/interrupts", "r") as f:
            lines = f.readlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    irq = parts[0].replace(":", "")
                    if irq.isdigit():
                        cpu_counts = []
                        # 计算各 CPU 上的中断次数
                        for i in range(1, len(parts)-1):
                            if parts[i].isdigit():
                                cpu_counts.append(int(parts[i]))
                        handler = parts[-1]
                        interrupts.append({
                            "irq": int(irq),
                            "handler": handler,
                            "cpu_counts": cpu_counts,
                            "total": sum(cpu_counts)
                        })
    
    return sorted(interrupts, key=lambda x: x["total"], reverse=True)


def generate_binding_plan(cpu_topo, npu_topo, interrupts):
    """生成绑核方案"""
    plan = {
        "version": "1.0",
        "generated_at": run_command("date -Iseconds"),
        "process_bindings": [],
        "irq_bindings": [],
        "system_config": {
            "irqbalance_enabled": False,
            "hugepages_enabled": True,
            "transparent_hugepage": "always"
        }
    }
    
    for node in cpu_topo["numa_nodes"]:
        node_id = node["node_id"]
        cpus = node["cpus"]
        if not cpus:
            continue
        
        # 为每个 NUMA 节点分配管理核和计算核
        main_cpu = cpus[0]
        worker_cpus = cpus[1:-2] if len(cpus) > 3 else cpus[1:]
        irq_cpus = cpus[-2:] if len(cpus) > 2 else []
        
        plan["process_bindings"].append({
            "role": f"management_numa{node_id}",
            "cpus": str(main_cpu),
            "numa": node_id,
            "description": "管理线程绑核"
        })
        
        if worker_cpus:
            cpu_str = f"{worker_cpus[0]}-{worker_cpus[-1]}" if len(worker_cpus) > 1 else str(worker_cpus[0])
            plan["process_bindings"].append({
                "role": f"worker_numa{node_id}",
                "cpus": cpu_str,
                "numa": node_id,
                "description": "计算线程绑核"
            })
        
        if irq_cpus:
            cpu_str = f"{irq_cpus[0]}-{irq_cpus[-1]}" if len(irq_cpus) > 1 else str(irq_cpus[0])
            plan["irq_bindings"].append({
                "numa": node_id,
                "cpus": cpu_str,
                "description": "中断绑核"
            })
    
    # 添加高频中断的绑核建议
    high_freq_irqs = [i for i in interrupts if i["total"] > 1000]
    for irq in high_freq_irqs[:5]:
        plan["irq_bindings"].append({
            "irq": irq["irq"],
            "handler": irq["handler"],
            "suggested_numa": 0,
            "reason": f"高频中断，建议隔离到专用核"
        })
    
    return plan


def print_topology(cpu_topo, npu_topo):
    """打印拓扑信息"""
    print("=" * 60)
    print("          CPU 拓扑信息")
    print("=" * 60)
    print(f"物理 Socket: {cpu_topo['sockets']}")
    print(f"每 Socket 核心数: {cpu_topo['cores_per_socket']}")
    print(f"每核心线程数: {cpu_topo['threads_per_core']}")
    print(f"总 CPU 数: {cpu_topo['total_cpus']}")
    print(f"NUMA 节点数: {len(cpu_topo['numa_nodes'])}")
    
    for node in cpu_topo["numa_nodes"]:
        mem_gb = node["memory"] / (1024 ** 3) if node["memory"] else 0
        print(f"\nNUMA Node {node['node_id']}:")
        print(f"  CPUs: {len(node['cpus'])} 核 ({node['cpus'][0]}-{node['cpus'][-1]})")
        print(f"  内存: {mem_gb:.1f} GB")
    
    print("\n" + "=" * 60)
    print("          NPU 拓扑信息")
    print("=" * 60)
    print(f"NPU 数量: {npu_topo['devices']}")
    print(f"使用灵衢总线: {'是' if npu_topo['has_ub_bus'] else '否'}")
    
    for aff in npu_topo["affinities"]:
        print(f"\nNPU {aff['npu_id']}:")
        print(f"  首选 NUMA: {aff['preferred_numa']}")
        print(f"  Socket: {aff['socket']}")


def print_binding_plan(plan):
    """打印绑核方案"""
    print("\n" + "=" * 60)
    print("          推荐绑核方案")
    print("=" * 60)
    print(f"生成时间: {plan['generated_at']}")
    
    print("\n【进程绑核】")
    for binding in plan["process_bindings"]:
        print(f"  {binding['role']:20s} → CPUs: {binding['cpus']:10s}  NUMA: {binding['numa']}")
        print(f"        {binding['description']}")
    
    print("\n【中断绑核】")
    for binding in plan["irq_bindings"]:
        if "irq" in binding:
            print(f"  IRQ {binding['irq']:3d} ({binding['handler']}) → NUMA: {binding['suggested_numa']}")
        else:
            print(f"  NUMA {binding['numa']} → CPUs: {binding['cpus']}")
    
    print("\n【系统配置建议】")
    print(f"  irqbalance: {'关闭' if not plan['system_config']['irqbalance_enabled'] else '开启'}")
    print(f"  大页: {'开启' if plan['system_config']['hugepages_enabled'] else '关闭'}")
    print(f"  透明大页: {plan['system_config']['transparent_hugepage']}")


def main():
    parser = argparse.ArgumentParser(description='CPU/NPU 拓扑信息采集与绑核方案生成')
    parser.add_argument('-p', '--print', action='store_true', help='打印拓扑信息')
    parser.add_argument('-g', '--generate', action='store_true', help='生成绑核方案')
    parser.add_argument('-o', '--output', help='输出文件路径')
    args = parser.parse_args()
    
    # 采集信息
    cpu_topo = collect_cpu_topology()
    npu_topo = collect_npu_topology()
    interrupts = collect_interrupt_info()
    
    # 打印信息
    if args.print:
        print_topology(cpu_topo, npu_topo)
    
    # 生成方案
    if args.generate:
        plan = generate_binding_plan(cpu_topo, npu_topo, interrupts)
        print_binding_plan(plan)
        
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)
            print(f"\n绑核方案已保存到 {args.output}")
    
    # 如果没有指定参数，默认打印并生成
    if not args.print and not args.generate:
        print_topology(cpu_topo, npu_topo)
        plan = generate_binding_plan(cpu_topo, npu_topo, interrupts)
        print_binding_plan(plan)


if __name__ == '__main__':
    main()
