#!/usr/bin/env python3
"""
Phase 3 — 激活过程差异追溯: 四组对照 (A=异常 B=异常标杆 C=邻近正常 D=邻近标杆)。

功能:
  - dump 数据: 参数梯度对齐 → 锚点逐层四组对比 (前向+反向, Max/Min/Norm)
  - monitor 数据: 目标参数梯度在异常/标杆/邻近坐标间的对比
  - 自动选邻近正常坐标作为 C (无用户指定时)

输出: gradient_comparison, layer_overview (全层差异), drilldowns (逐层下钻), divergence_summary

用法:
  python phase3_trace_analyzer.py \
    --target '<full_param_name>' \
    --abnormal-dump <dump_dir> [--baseline-dump <dump_dir>] \
    [--abnormal-monitor p1.json --abnormal-coord '{"opt_step":0,"rank":34,"ms":16}'] \
    [-o result.json]
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from statistics import median


# ─── 数据加载 ────────────────────────────────────────────

def load_dump_ops(dump_dir):
    dump_file = os.path.join(dump_dir, 'dump.json')
    construct_file = os.path.join(dump_dir, 'construct.json')
    if not os.path.exists(dump_file):
        return None
    with open(dump_file, 'r', encoding='utf-8') as f:
        dump_data = json.load(f)
    construct = {}
    if os.path.exists(construct_file):
        with open(construct_file, 'r', encoding='utf-8') as f:
            construct = json.load(f)
    return {
        'ops': dump_data.get('data', {}),
        'construct': construct,
        'meta': {k: v for k, v in dump_data.items() if k != 'data'}
    }


def load_p1_json(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ─── 张量统计 ────────────────────────────────────────────

def extract_stats(op_entry):
    """提取算子所有张量的 Max/Min/Mean/Norm"""
    result = {'Max': None, 'Min': None, 'Mean': None, 'Norm': None}

    def _extract(obj):
        if isinstance(obj, dict):
            if 'Max' in obj and isinstance(obj['Max'], (int, float)):
                for k in ('Max', 'Min', 'Mean', 'Norm'):
                    val = obj.get(k)
                    if isinstance(val, (int, float)) and val == val:  # not NaN
                        if result[k] is None or abs(val) > abs(result[k]):
                            result[k] = val
            else:
                for v in obj.values():
                    _extract(v)
        elif isinstance(obj, list):
            for item in obj:
                _extract(item)

    for field in ['input_args', 'input', 'output']:
        if field in op_entry:
            _extract(op_entry[field])
    for key in op_entry:
        if key not in ('input_args', 'input_kwargs', 'input', 'output', 'is_recompute'):
            _extract(op_entry[key])
    return {k: v for k, v in result.items() if v is not None}


# ─── Module 层级 ─────────────────────────────────────────

def extract_layer_name(op_name):
    """
    从算子名提取 TransformerLayer 层级标识。
    layers.0.self_attention.linear_q_proj... → layers.0
    layers.15.mlp.experts... → layers.15
    embedding.word_embeddings... → embedding
    output_layer... → output_layer
    返回: layer_key, sub_module
    """
    parts = op_name.split('.')
    for i, p in enumerate(parts):
        if p == 'layers' and i + 1 < len(parts) and parts[i + 1].isdigit():
            layer_idx = parts[i + 1]
            # 找 sub-module: self_attention, mlp, input_layernorm, post_attention_layernorm
            sub = 'other'
            for j in range(i + 2, min(i + 5, len(parts))):
                if parts[j] in ('self_attention', 'mlp', 'input_layernorm',
                                'post_attention_layernorm', 'pre_mlp_layernorm'):
                    sub = parts[j]
                    break
            return f"layers.{layer_idx}", sub

    if 'embedding' in op_name.lower():
        return 'embedding', 'embedding'
    if 'output_layer' in op_name:
        return 'output_layer', 'output_layer'
    return 'other', 'other'


def is_forward_op(op_name):
    return '.forward' in op_name and '.backward' not in op_name


def is_backward_op(op_name):
    return '.backward' in op_name


# ─── Module 统计聚合 ─────────────────────────────────────

def aggregate_module_stats(ops):
    """
    按 (layer_key, sub_module, direction) 聚合所有算子的统计值。
    返回: {(layer_key, sub_module, fwd|bwd): {'Max': max_of_all, 'Min': ..., 'Norm': ..., 'count': N}}
    """
    agg = defaultdict(lambda: {'Max': 0, 'Min': 0, 'Mean': 0, 'Norm': 0, 'count': 0})

    for op_name, entry in ops.items():
        layer, sub = extract_layer_name(op_name)
        direction = 'forward' if is_forward_op(op_name) else ('backward' if is_backward_op(op_name) else 'other')
        if direction == 'other':
            continue

        stats = extract_stats(entry)
        key = (layer, sub, direction)
        agg[key]['count'] += 1
        for m in ('Max', 'Min', 'Mean', 'Norm'):
            if m in stats:
                agg[key][m] += abs(stats[m])

    return agg


def gen_op_name_variants(op_name):
    """
    生成算子名的可能变体，用于跨设备模糊匹配。
    NPU: NPU.npu_fusion_attention  GPU: core_attention.fused_attention.FusedAttention
    NPU: MindSpeed.npu_rotary_position_embedding  GPU: Triton.rotary_fwd_kv_kernel
    """
    variants = [op_name]
    # 去掉 namespace 前缀
    for prefix in ['NPU.', 'MindSpeed.', 'Triton.', 'Torch.', 'Tensor.', 'Functional.', 'Distributed.']:
        if op_name.startswith(prefix):
            variants.append(op_name[len(prefix):])
    return variants


# ─── 逐层追溯 ────────────────────────────────────────────

def compare_layer_stats(ab_agg, bl_agg):
    """
    对比异常侧和标杆侧各 module 的统计差异。
    返回按差异排序的 module 列表。
    """
    diffs = []
    for key, ab in ab_agg.items():
        bl = bl_agg.get(key)
        if not bl or bl['count'] == 0 or ab['count'] == 0:
            continue

        ratios = {}
        for m in ('Max', 'Min', 'Mean', 'Norm'):
            if bl[m] > 0:
                ratios[m] = max(ab[m] / bl[m], bl[m] / ab[m]) if ab[m] > 0 else float('inf')
            elif ab[m] > 0:
                ratios[m] = float('inf')

        max_ratio = max((v for v in ratios.values() if v != float('inf')), default=1.0)
        diffs.append({
            'layer': key[0], 'sub_module': key[1], 'direction': key[2],
            'abnormal': {m: ab[m] for m in ('Max', 'Min', 'Mean', 'Norm')},
            'baseline': {m: bl[m] for m in ('Max', 'Min', 'Mean', 'Norm')} if bl else {},
            'ratios': ratios,
            'max_ratio': max_ratio
        })

    diffs.sort(key=lambda x: x['max_ratio'], reverse=True)
    return diffs


def drill_down_layer(layer_key, direction, ab_ops, bl_ops, ab_construct, bl_construct):
    """
    对指定 layer 和 direction (forward/backward) 的所有算子做细粒度对比。
    返回该层内各算子的差异。
    """
    ab_layer_ops = {}
    bl_layer_ops = {}

    for op_name, entry in ab_ops.items():
        lk, _ = extract_layer_name(op_name)
        if lk != layer_key:
            continue
        if direction == 'forward' and not is_forward_op(op_name):
            continue
        if direction == 'backward' and not is_backward_op(op_name):
            continue
        ab_layer_ops[op_name] = entry

    for op_name, entry in (bl_ops or {}).items():
        lk, _ = extract_layer_name(op_name)
        if lk != layer_key:
            continue
        if direction == 'forward' and not is_forward_op(op_name):
            continue
        if direction == 'backward' and not is_backward_op(op_name):
            continue
        bl_layer_ops[op_name] = entry

    # 对比: 对每个 ab op, 找 bl 中最匹配的
    comparisons = []
    for op_name, entry in ab_layer_ops.items():
        stats = extract_stats(entry)
        # 尝试精确匹配
        bl_entry = bl_layer_ops.get(op_name)
        if not bl_entry and bl_layer_ops:
            # 模糊匹配: 找名字最接近的
            # 简化: 用 sub_module + 操作类型匹配
            _, sub = extract_layer_name(op_name)
            for bl_name, bl_e in bl_layer_ops.items():
                _, bl_sub = extract_layer_name(bl_name)
                if sub == bl_sub:
                    # 比较操作类型相似度
                    op_type = op_name.split('.')[-2] if '.' in op_name else ''
                    bl_op_type = bl_name.split('.')[-2] if '.' in bl_name else ''
                    if op_type == bl_op_type:
                        bl_entry = bl_e
                        break

        bl_stats = extract_stats(bl_entry) if bl_entry else {}
        diffs = {}
        for m in ('Max', 'Min', 'Mean', 'Norm'):
            a = stats.get(m, 0)
            b = bl_stats.get(m, 0)
            if b > 0:
                diffs[m] = max(a / b, b / a) if a > 0 else float('inf')
            elif a > 0:
                diffs[m] = float('inf')

        max_diff = max((v for v in diffs.values() if v != float('inf')), default=0)
        comparisons.append({
            'op_name': op_name,
            'abnormal_stats': stats,
            'baseline_stats': bl_stats,
            'diff_ratios': diffs,
            'max_diff': max_diff,
            'matched': bl_entry is not None
        })

    comparisons.sort(key=lambda x: x['max_diff'], reverse=True)
    return comparisons


# ─── 主追溯逻辑 ──────────────────────────────────────────

def full_trace(ab_ops, bl_ops, ab_construct, target_param):
    """
    完整追溯流程:
    1. 聚合 module 统计 → 找差异最大的层
    2. 判断反向/前向差异来源
    3. 逐层下钻到算子级
    """
    result = {'target': target_param, 'steps': []}

    # Step 1: 参数梯度对比
    grad_ab = None
    grad_bl = None
    grad_name = target_param.rsplit('.', 1)[0] + '.parameters_grad.0'
    if grad_name in ab_ops:
        grad_ab = extract_stats(ab_ops[grad_name])
    if bl_ops and grad_name in bl_ops:
        grad_bl = extract_stats(bl_ops[grad_name])

    result['gradient_comparison'] = {
        'op_name': grad_name,
        'abnormal': grad_ab,
        'baseline': grad_bl,
        'note': '参数梯度对比: 异常侧 vs 标杆侧'
    }

    # Step 2: 按 module 聚合, 对比整体趋势
    ab_agg = aggregate_module_stats(ab_ops)
    bl_agg = aggregate_module_stats(bl_ops) if bl_ops else {}

    layer_diffs = compare_layer_stats(ab_agg, bl_agg)
    result['layer_overview'] = layer_diffs[:30]

    if not layer_diffs:
        result['status'] = 'no_common_modules'
        return result

    # Step 3: 判断方向 — 反向差异 vs 前向差异
    # 分离前向和反向的差异
    fwd_diffs = [d for d in layer_diffs if d['direction'] == 'forward']
    bwd_diffs = [d for d in layer_diffs if d['direction'] == 'backward']

    direction_analysis = {}
    if bwd_diffs:
        # 看反向第一个 (最末层) 的差异
        # 反向传播顺序: layers.26 → layers.0
        # 按 layer index 降序排
        bwd_sorted = sorted(bwd_diffs, key=lambda x: int(x['layer'].split('.')[1])
                           if '.' in x['layer'] and x['layer'].split('.')[1].isdigit() else 999,
                           reverse=True)
        first_bwd = bwd_sorted[0] if bwd_sorted else None
        direction_analysis['backward_entrance'] = {
            'first_diverging_layer': first_bwd['layer'] if first_bwd else None,
            'max_ratio': first_bwd['max_ratio'] if first_bwd else 0,
            'note': '反向传播入口层差异 (从深层往浅层看)'
        }

    if fwd_diffs:
        fwd_sorted = sorted(fwd_diffs, key=lambda x: int(x['layer'].split('.')[1])
                           if '.' in x['layer'] and x['layer'].split('.')[1].isdigit() else 0)
        first_fwd = fwd_sorted[0] if fwd_sorted else None
        direction_analysis['forward_entrance'] = {
            'first_diverging_layer': first_fwd['layer'] if first_fwd else None,
            'max_ratio': first_fwd['max_ratio'] if first_fwd else 0,
            'note': '前向传播入口层差异 (从浅层往深层看)'
        }

    # 判断主要差异方向
    top_bwd_ratio = bwd_diffs[0]['max_ratio'] if bwd_diffs else 0
    top_fwd_ratio = fwd_diffs[0]['max_ratio'] if fwd_diffs else 0

    if top_bwd_ratio > top_fwd_ratio * 2:
        primary_direction = 'backward'
        direction_analysis['conclusion'] = '反向差异显著大于前向，问题主要在反向传播过程'
    elif top_fwd_ratio > top_bwd_ratio * 2:
        primary_direction = 'forward'
        direction_analysis['conclusion'] = '前向差异显著大于反向，问题可能源自前向计算'
    else:
        primary_direction = 'both'
        direction_analysis['conclusion'] = '前向和反向均有明显差异，需分别追溯'

    result['direction_analysis'] = direction_analysis

    # Step 4: 对差异最大的 3 个 layer 逐层下钻
    top_layers = set()
    for d in layer_diffs[:5]:
        top_layers.add((d['layer'], d['direction']))

    result['drilldowns'] = []
    for layer_key, direction in sorted(top_layers):
        if not bl_ops:
            break
        ops_comparison = drill_down_layer(layer_key, direction, ab_ops, bl_ops,
                                          ab_construct, None)
        result['drilldowns'].append({
            'layer': layer_key,
            'direction': direction,
            'top_divergent_ops': ops_comparison[:10],
            'matched_count': sum(1 for c in ops_comparison if c['matched']),
            'total_count': len(ops_comparison)
        })

    # Step 5: 总结 — 最早出现差异的位置
    result['divergence_summary'] = {
        'primary_direction': primary_direction,
        'top_diverging_layers': [
            {'layer': d['layer'], 'sub': d['sub_module'], 'dir': d['direction'],
             'ratio': d['max_ratio']}
            for d in layer_diffs[:5]
        ]
    }

    result['status'] = 'completed'
    return result


# ─── Monitor 对比 (无 dump) ─────────────────────────────

def monitor_compare(target_param, ab_p1, ab_coord, bl_p1, bl_coord):
    def find_norm(p1_data, coord, target):
        if not p1_data: return None
        anomalies = p1_data.get('anomalies', [])
        for a in anomalies:
            if (a.get('target_name') == target and
                a.get('rank') == coord.get('rank') and
                a.get('micro_step') == coord.get('micro_step')):
                return a.get('norm', a.get('delta', 0))
        # also check target_rank_norms
        trn = p1_data.get('target_rank_norms', {})
        for os_data in trn.values():
            ranks = os_data.get('ranks', {})
            if str(coord.get('rank', '')) in ranks:
                return ranks[str(coord['rank'])]
        return None

    ab_norm = find_norm(ab_p1, ab_coord, target_param)
    bl_norm = find_norm(bl_p1, bl_coord, target_param) if bl_p1 else None

    result = {
        'target': target_param, 'abnormal_coord': ab_coord,
        'abnormal_norm': ab_norm, 'baseline_coord': bl_coord, 'baseline_norm': bl_norm
    }
    if ab_norm and bl_norm and bl_norm > 0:
        result['ratio'] = ab_norm / bl_norm
        result['conclusion'] = (f"异常 norm={ab_norm:.4g}, 标杆 norm={bl_norm:.4g}, "
                                f"差异 {ab_norm / bl_norm:.1f}x")
    elif ab_norm:
        result['conclusion'] = f"异常 norm={ab_norm:.4g}, 无标杆对比"
    else:
        result['conclusion'] = "未找到梯度数据"
    return result


def auto_select_baseline(p1_data, target_param, ab_coord):
    if not p1_data:
        return None
    anomalies = p1_data.get('anomalies', [])
    ab_opt = ab_coord.get('optimizer_step', 0)
    candidates = []
    for a in anomalies:
        if a.get('target_name') != target_param: continue
        if a.get('optimizer_step') != ab_opt: continue
        if a.get('rank') == ab_coord.get('rank') and a.get('micro_step') == ab_coord.get('micro_step'): continue
        candidates.append(a)
    candidates.sort(key=lambda x: x.get('deviation_ratio', float('inf')))
    if candidates:
        c = candidates[0]
        return {'optimizer_step': c.get('optimizer_step', 0), 'rank': c.get('rank', 0), 'micro_step': c.get('micro_step', 0)}
    return None


# ─── Main ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Phase 3 — 激活过程差异追溯')
    parser.add_argument('--target', required=True, help='目标参数完整名')
    parser.add_argument('--abnormal-dump', help='异常侧 dump 目录')
    parser.add_argument('--baseline-dump', help='标杆侧 dump 目录')
    parser.add_argument('--abnormal-monitor', help='异常侧 Phase 1 JSON')
    parser.add_argument('--abnormal-coord', help='异常坐标 JSON')
    parser.add_argument('--baseline-monitor', help='标杆侧 Phase 1 JSON')
    parser.add_argument('--baseline-coord', help='标杆坐标 JSON')
    parser.add_argument('--auto-baseline', action='store_true', help='自动选邻近坐标')
    parser.add_argument('--output', '-o', help='输出 JSON')
    args = parser.parse_args()

    ab_coord = json.loads(args.abnormal_coord) if args.abnormal_coord else {}
    bl_coord = json.loads(args.baseline_coord) if args.baseline_coord else None
    ab_p1 = load_p1_json(args.abnormal_monitor)
    bl_p1 = load_p1_json(args.baseline_monitor)

    if args.auto_baseline and not bl_coord and ab_p1:
        bl_coord = auto_select_baseline(ab_p1, args.target, ab_coord)
        print(f"自动标杆: {bl_coord}")

    result = {'phase': 3, 'target': args.target}

    if args.abnormal_dump:
        ab_ops = load_dump_ops(args.abnormal_dump)
        bl_ops = load_dump_ops(args.baseline_dump) if args.baseline_dump else None
        if not ab_ops:
            print("Error: dump load failed"); sys.exit(1)

        print(f"abnormal: {len(ab_ops['ops'])} ops, baseline: {len(bl_ops['ops']) if bl_ops else 0} ops")
        trace = full_trace(ab_ops['ops'], bl_ops['ops'] if bl_ops else None,
                           ab_ops['construct'], args.target)
        result['trace'] = trace

        # 打印摘要
        da = trace.get('direction_analysis', {})
        print(f"\n方向判定: {da.get('conclusion', 'N/A')}")
        print(f"差异最大的层:")
        for d in trace.get('layer_overview', [])[:8]:
            print(f"  {d['layer']}/{d['sub_module']} [{d['direction']}]: {d['max_ratio']:.1f}x")

        if trace.get('drilldowns'):
            print(f"\n下钻结果:")
            for dd in trace['drilldowns']:
                top = dd['top_divergent_ops'][0] if dd['top_divergent_ops'] else None
                if top:
                    print(f"  {dd['layer']} [{dd['direction']}] top: {top['op_name'].split('.')[-1]} diff={top['max_diff']:.1f}x")

    elif args.abnormal_monitor:
        compare = monitor_compare(args.target, ab_p1, ab_coord, bl_p1, bl_coord)
        result['monitor_compare'] = compare
        print(f"Monitor 对比: {compare.get('conclusion', 'N/A')}")

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n→ {args.output}")
    else:
        print(f"\n{json.dumps(result, indent=2, ensure_ascii=False, default=str)[:3000]}")


if __name__ == '__main__':
    main()
