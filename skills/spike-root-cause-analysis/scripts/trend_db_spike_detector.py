#!/usr/bin/env python3
"""
Phase 1 — 异常前反向定位: 从梯度监控数据检测 spike 候选坐标。

功能:
  - 自动切分分析 (PP/TP/micro_step 累积窗口检测)
  - 按数据类型分叉: step级(全局基线IQR) / micro_step累积(top-3 suspect→delta突变) / dump(绝对值排序)

输入:
  - trend.db (SQLite): msprobe 梯度趋势, 含 trend_data/monitoring_targets/monitoring_metrics
  - monitor CSV: vpp_stage,name,step,micro_step,min,max,mean,norm,shape,dtype
  - dump_statistic 目录: dump.json 中的 parameters_grad 条目

输出: JSON with sharding_analysis, summary, target_rank_norms, anomalies

用法:
  python trend_db_spike_detector.py <data> [--csv|--dump] [-o result.json]
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from statistics import median


def load_trend_db(db_path):
    """加载 trend.db，返回结构化数据。"""
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found", file=sys.stderr)
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 验证 schema
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    required = {'trend_data', 'monitoring_targets', 'monitoring_metrics'}
    if not required.issubset(set(tables)):
        print(f"Error: {db_path} missing tables: {required - set(tables)}", file=sys.stderr)
        conn.close()
        return None

    # 加载元数据
    targets = {}
    for r in conn.execute("SELECT target_id, target_name, vpp_stage, micro_step FROM monitoring_targets"):
        targets[r['target_id']] = {
            'name': r['target_name'],
            'vpp_stage': r['vpp_stage'],
            'micro_step': r['micro_step']
        }

    metrics = {}
    for r in conn.execute("SELECT metric_id, metric_name FROM monitoring_metrics"):
        metrics[r['metric_id']] = r['metric_name']

    # 加载标签
    tags = {}
    for r in conn.execute("SELECT tag_id, tag_name, category, metric_id FROM monitoring_tags"):
        tags[r['tag_id']] = {
            'name': r['tag_name'],
            'category': r['category'],
            'metric_id': r['metric_id']
        }

    # 加载 tag→target 映射
    tag_target = defaultdict(set)
    for r in conn.execute("SELECT tag_id, target_id FROM tag_target_mapping"):
        tag_target[r['tag_id']].add(r['target_id'])

    # 加载 global_stats
    gs = conn.execute("SELECT * FROM global_stats").fetchone()
    global_stats = dict(gs) if gs else {}

    # 加载 trend_data
    trend_rows = []
    cursor = conn.execute(
        "SELECT rank, step, target_id, metric_id, norm, min, max, mean "
        "FROM trend_data ORDER BY rank, step, target_id, metric_id"
    )
    for r in cursor:
        trend_rows.append({
            'rank': r['rank'],
            'step': r['step'],
            'target_id': r['target_id'],
            'metric_id': r['metric_id'],
            'norm': r['norm'],
            'min': r['min'],
            'max': r['max'],
            'mean': r['mean']
        })

    conn.close()

    return {
        'targets': targets,
        'metrics': metrics,
        'tags': tags,
        'tag_target': {k: list(v) for k, v in tag_target.items()},
        'global_stats': global_stats,
        'trend_rows': trend_rows
    }


def load_dump_statistic(dump_dir):
    """
    加载 dump_statistic 目录中的 parameters_grad 数据。
    自动检测目录结构:
      - 单 step: dump_dir/rank{N}/dump.json
      - 多 step: dump_dir/step{N}/rank{M}/dump.json

    从每个 dump.json 中提取所有 parameters_grad.* 条目的梯度 norm。
    """
    if not os.path.isdir(dump_dir):
        print(f"Error: {dump_dir} is not a directory", file=sys.stderr)
        return None

    targets = {}
    rows = []
    tid = 0
    name_to_id = {}
    step = 0  # 默认 step

    # 检测目录结构
    subdirs = [d for d in os.listdir(dump_dir) if os.path.isdir(os.path.join(dump_dir, d))]
    has_steps = any(d.startswith('step') for d in subdirs)

    if has_steps:
        # 多 step 结构: dump_dir/step{N}/rank{M}/dump.json
        step_dirs = sorted([d for d in subdirs if d.startswith('step')])
        for step_dir in step_dirs:
            step_num = int(step_dir.replace('step', ''))
            step_path = os.path.join(dump_dir, step_dir)
            _load_dump_ranks(step_path, step_num, name_to_id, targets, rows)
    else:
        # 单 step 结构: dump_dir/rank{N}/dump.json
        _load_dump_ranks(dump_dir, step, name_to_id, targets, rows)

    if not rows:
        print("Error: no parameters_grad entries found", file=sys.stderr)
        return None

    return {
        'targets': targets,
        'metrics': {1: 'grad_norm'},
        'trend_rows': rows
    }


def _load_dump_ranks(base_path, step, name_to_id, targets, rows):
    """加载一个 step 下所有 rank 的 dump.json"""
    tid = len(targets)
    for entry in sorted(os.listdir(base_path)):
        rank_dir = os.path.join(base_path, entry)
        if not os.path.isdir(rank_dir) or not entry.startswith('rank'):
            continue

        rank = int(entry.replace('rank', ''))
        dump_file = os.path.join(rank_dir, 'dump.json')
        if not os.path.exists(dump_file):
            continue

        with open(dump_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        ops = data.get('data', {})
        for op_name, op_data in ops.items():
            if 'parameters_grad' not in op_name:
                continue

            param_name = op_name.rsplit('.parameters_grad', 1)[0]

            for wkey, wval in op_data.items():
                if isinstance(wval, list) and len(wval) > 0 and 'Norm' in wval[0]:
                    norm = wval[0].get('Norm', 0)
                    mx = wval[0].get('Max', 0)
                    mn = wval[0].get('Min', 0)
                    mean_val = wval[0].get('Mean', 0)
                    full_name = f"{param_name}.{wkey}"

                    if full_name not in name_to_id:
                        name_to_id[full_name] = tid
                        targets[tid] = {'name': full_name, 'vpp_stage': 0, 'micro_step': 0}
                        tid += 1

                    rows.append({
                        'rank': rank,
                        'step': step,
                        'target_id': name_to_id[full_name],
                        'norm': norm if isinstance(norm, (int, float)) else 0,
                        'min': mn if isinstance(mn, (int, float)) else 0,
                        'max': mx if isinstance(mx, (int, float)) else 0,
                        'mean': mean_val if isinstance(mean_val, (int, float)) else 0,
                        'metric_id': 1
                    })


# ─── Dump 数据异常检测 ──────────────────────────────────

def detect_dump_spikes(trend_rows, targets, top_n=20):
    """
    dump parameters_grad 数据异常检测。

    单 step (所有 row step 相同): 无时间维度，按绝对值 top-N
    多 step: 有 step 维度，按参数计算跨 step 基线 (MAD/IQR)，动态阈值检测
    """
    steps = sorted(set(r['step'] for r in trend_rows))
    has_multi_step = len(steps) > 1

    if not has_multi_step:
        # 单 step: 绝对值 top-N
        anomalies = []
        for r in trend_rows:
            anomalies.append({
                'rank': r['rank'], 'step': r['step'],
                'target_id': r['target_id'],
                'target_name': targets[r['target_id']]['name'],
                'metric': 'grad_norm',
                'norm': r['norm'],
                'min': r['min'], 'max': r['max'], 'mean': r['mean'],
                'deviation_ratio': 0, 'trigger': 'dump_abs_norm'
            })
        anomalies.sort(key=lambda x: x['norm'], reverse=True)
        anomalies = anomalies[:top_n]

        if anomalies:
            med = sorted([a['norm'] for a in anomalies])[len(anomalies) // 2]
            for a in anomalies:
                a['baseline_median'] = med
                a['deviation_ratio'] = a['norm'] / med if med > 0 else 0
        return anomalies

    # 多 step: 按参数计算跨 step 基线
    baselines = compute_per_target_baselines(trend_rows, targets)
    anomalies = detect_spikes(trend_rows, targets, baselines,
                              metric_id=1, iqr_multiplier=5.0, mad_multiplier=10.0)
    return anomalies[:top_n]


def load_monitor_csv(csv_path):
    """加载 monitor CSV 文件。"""
    import csv
    if not os.path.exists(csv_path):
        print(f"Error: {csv_path} not found", file=sys.stderr)
        return None

    # 从文件名推断 metric 类型
    fname = os.path.basename(csv_path)
    if 'grad_reduced' in fname:
        default_metric = 2
    else:
        default_metric = 1

    # 尝试从目录名推断 rank (如 rank16, rank34)
    default_rank = 0
    import re
    rank_match = re.search(r'rank(\d+)', csv_path)
    if rank_match:
        default_rank = int(rank_match.group(1))

    rows = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({
                'rank': default_rank,
                'vpp_stage': int(r.get('vpp_stage', 0)),
                'name': r['name'],
                'step': int(r['step']),
                'micro_step': int(r.get('micro_step', 0)) if r.get('micro_step', '') != '' else 0,
                'min': float(r.get('min', 0)),
                'max': float(r.get('max', 0)),
                'mean': float(r.get('mean', 0)),
                'norm': float(r.get('norm', 0)),
                'metric_id': default_metric,
                'shape': r.get('shape', ''),
                'dtype': r.get('dtype', ''),
            })

    # 提取唯一目标名，记录每个参数的 micro_step 范围
    name_micro_steps = defaultdict(set)
    for r in rows:
        name_micro_steps[r['name']].add(r['micro_step'])

    names = sorted(name_micro_steps.keys())
    # 取所有 micro_steps 的并集作为整体 micro_step 范围
    all_micro_steps = sorted(set(ms for mss in name_micro_steps.values() for ms in mss))
    targets = {
        i: {
            'name': n,
            'vpp_stage': 0,
            'micro_step': sorted(name_micro_steps[n])[0] if name_micro_steps[n] else 0
        }
        for i, n in enumerate(names)
    }
    name_to_id = {n: i for i, n in enumerate(names)}

    for r in rows:
        r['target_id'] = name_to_id[r['name']]

    return {
        'targets': targets,
        'metrics': {1: 'grad_unreduced', 2: 'grad_reduced'},
        'trend_rows': rows
    }


# ─── Step 0: 切分分析 ──────────────────────────────────────

def detect_accumulation_window(trend_rows, num_samples=5, drop_threshold=3.0):
    """
    检测 trend.db 中 step 列是否实际是 micro_step (梯度累积)。

    方法: 采样若干参数，找 norm 的周期性重置点。
    重置点 = norm 突然大幅下降 (相对前一步)，意味着梯度累积清空，进入下一个 optimizer step。
    如果多个参数的 reset 间隔一致 → step 实际是 micro_step，间隔 = 累积窗口大小。

    返回: (is_micro_step: bool, window_size: int, optimizer_step_count: int)
    """
    if not trend_rows:
        return False, 1, 0

    steps = sorted(set(r['step'] for r in trend_rows))
    if len(steps) < 10:
        return False, 1, 0

    # 选一个 rank (取最多的那个)
    rank_counts = {}
    for r in trend_rows:
        rank_counts[r['rank']] = rank_counts.get(r['rank'], 0) + 1
    best_rank = max(rank_counts, key=rank_counts.get)

    # 采样参数: 取不同 layer 的 target_id, 避免全采同一层
    # 先按 target_id 分组看看哪些有完整 step 覆盖
    tid_steps = {}
    for r in trend_rows:
        if r['rank'] != best_rank:
            continue
        tid = r['target_id']
        if tid not in tid_steps:
            tid_steps[tid] = []
        tid_steps[tid].append((r['step'], r['norm']))

    # 选覆盖全部 step 的 target_id
    full_coverage = []
    for tid, data in tid_steps.items():
        if len(data) == len(steps):
            full_coverage.append(tid)

    if not full_coverage:
        return False, 1, 0

    # 策略采样: 从不同 layer 各取一个
    import random
    sample_tids = full_coverage[:num_samples] if len(full_coverage) <= num_samples else \
        [full_coverage[i * len(full_coverage) // num_samples] for i in range(num_samples)]

    # 对每个采样参数，找 reset 点
    all_intervals = []
    for tid in sample_tids:
        data = sorted(tid_steps[tid], key=lambda x: x[0])
        norms = [d[1] for d in data]

        reset_steps = []
        for i in range(1, len(norms)):
            if norms[i - 1] > 0 and norms[i] > 0:
                ratio = norms[i - 1] / norms[i]
                if ratio > drop_threshold:
                    reset_steps.append(data[i][0])

        # 计算间隔
        if len(reset_steps) >= 2:
            intervals = [reset_steps[j + 1] - reset_steps[j] for j in range(len(reset_steps) - 1)]
            all_intervals.extend(intervals)

    if not all_intervals:
        return False, 1, 0

    # 找最常见的间隔 (众数)
    from collections import Counter
    interval_counts = Counter(all_intervals)
    most_common_interval, count = interval_counts.most_common(1)[0]

    # 需要至少 2 个参数确认, 且间隔一致
    if count >= 2 and most_common_interval > 1:
        window_size = most_common_interval
        optimizer_steps = (max(steps) + 1) // window_size
        return True, window_size, optimizer_steps

    return False, 1, 0


def analyze_sharding(data):
    """
    分析数据的并行切分方式，确定前反向的基本单位。

    检测逻辑:
      - PP: 检查 vpp_stage 是否有多个值
      - TP: 检查跨 shard/rank 参数名是否重叠
      - micro_step:
          1. targets/CSV 中有 micro_step 字段 → 直接使用
          2. DB 数据无 micro_step 字段 → 通过 norm 重置点检测梯度累积窗口
      - 确定前反向粒度

    返回: {
        'pp_stages': int,           # vpp_stage 数量
        'has_tp': bool,             # 是否存在 TP 切分
        'has_micro_step': bool,     # 是否有 micro_step 维度
        'micro_step_range': list,   # micro_step 范围
        'accumulation_window': int, # 梯度累积窗口大小 (0 表示无累积)
        'optimizer_step_count': int,# optimizer step 数量
        'pass_unit': str,           # 前反向单位描述
        'conclusion': str           # 一句话结论
    }
    """
    targets = data.get('targets', {})
    trend_rows = data.get('trend_rows', [])

    # 1. PP 检测
    vpp_stages = set()
    for tid, t in targets.items():
        vpp_stages.add(t.get('vpp_stage', 0))
    pp_stages = len(vpp_stages)

    # 2. micro_step 检测
    # 2a. 先从 targets 和 trend_rows 字段中找 micro_step
    micro_steps = set()
    for tid, t in targets.items():
        micro_steps.add(t.get('micro_step', 0))
    for r in trend_rows:
        if 'micro_step' in r:
            micro_steps.add(r['micro_step'])
    has_explicit_micro_step = len(micro_steps) > 1

    # 2b. 如果没有显式 micro_step, 检测 DB 的梯度累积模式
    accumulation_window = 0
    optimizer_step_count = 0
    has_accumulation = False

    if not has_explicit_micro_step and len(trend_rows) > 0:
        # 判断数据来源: DB 还是 CSV
        # DB 数据: targets 中 micro_step 全是 0, 但 step 可能实际是 micro_step
        has_accumulation, accumulation_window, optimizer_step_count = \
            detect_accumulation_window(trend_rows)

    if has_accumulation:
        has_micro_step = True
        micro_step_range = list(range(0, accumulation_window))
    elif has_explicit_micro_step:
        has_micro_step = True
        micro_step_range = sorted(micro_steps)
    else:
        has_micro_step = False
        micro_step_range = [0]

    # 3. TP 检测
    rank_params = {}
    for r in trend_rows:
        rank = r.get('rank', 0)
        tid = r.get('target_id')
        if rank not in rank_params:
            rank_params[rank] = set()
        rank_params[rank].add(tid)

    has_tp = False
    if len(rank_params) >= 2:
        param_sets = list(rank_params.values())
        first_set = param_sets[0]
        for s in param_sets[1:]:
            intersection = len(first_set & s)
            overlap_ratio = intersection / max(len(first_set), 1)
            if overlap_ratio < 0.8:
                has_tp = True
                break

    # 4. 确定前反向单位
    pass_parts = []
    if has_tp:
        pass_parts.append("跨 TP rank 聚合")
    if has_micro_step:
        if has_accumulation:
            pass_parts.append(f"(optimizer_step, micro_step), 累积窗口={accumulation_window}")
        else:
            pass_parts.append("(step, micro_step)")
    else:
        pass_parts.append("(step,)")
    pass_unit = " + ".join(pass_parts)

    # 5. 结论
    pp_note = f"PP={pp_stages}"
    tp_note = "有 TP 切分" if has_tp else "无 TP (纯 DP)"

    if has_accumulation:
        micro_note = (f"DB step 实际为 micro_step (梯度累积), 窗口={accumulation_window}, "
                      f"{optimizer_step_count} 个 optimizer step")
    elif has_micro_step:
        micro_note = f"micro_step={micro_step_range[0]}..{micro_step_range[-1]}"
    else:
        micro_note = "无 micro_step"

    conclusion = (
        f"{pp_note}, {tp_note}. {micro_note}. "
        f"前反向单位: {pass_unit}. "
        f"{'每个 rank 有完整模型，独立构成一次前反向。' if not has_tp else '多个 TP rank 共同组成一次前反向。'}"
    )

    return {
        'pp_stages': pp_stages,
        'vpp_stages': sorted(vpp_stages),
        'has_tp': has_tp,
        'has_micro_step': has_micro_step,
        'micro_step_range': micro_step_range,
        'accumulation_window': accumulation_window,
        'optimizer_step_count': optimizer_step_count,
        'accumulation_detected': has_accumulation,
        'pass_unit': pass_unit,
        'conclusion': conclusion
    }


# ─── Baseline Learning ───────────────────────────────────────────

def compute_per_target_baselines(trend_rows, targets):
    """
    对每个 target 学习其梯度的正常基线分布。
    使用 MAD-based robust statistics 排除异常 step 的干扰。
    """
    # 按 (target_id, metric_id) 分组，收集所有 (step, rank, norm)
    groups = defaultdict(list)
    for r in trend_rows:
        key = (r['target_id'], r['metric_id'])
        groups[key].append(r['norm'])

    baselines = {}
    for (tid, mid), norms in groups.items():
        n = len(norms)
        if n < 3:
            # 样本太少，直接用简单统计
            baselines[(tid, mid)] = {
                'median': median(norms),
                'q1': min(norms),
                'q3': max(norms),
                'iqr': max(norms) - min(norms),
                'mad': 0,
                'sample_count': n
            }
            continue

        sorted_norms = sorted(norms)
        q1_idx = n // 4
        q3_idx = (3 * n) // 4
        q1 = sorted_norms[q1_idx]
        q3 = sorted_norms[q3_idx]
        iqr = q3 - q1
        med = median(norms)

        # MAD (Median Absolute Deviation)
        abs_devs = sorted(abs(v - med) for v in norms)
        mad = abs_devs[n // 2] if abs_devs else 0

        baselines[(tid, mid)] = {
            'median': med,
            'q1': q1,
            'q3': q3,
            'iqr': iqr,
            'mad': mad,
            'sample_count': n
        }

    return baselines


def detect_spikes(trend_rows, targets, baselines, metric_id=1,
                  iqr_multiplier=5.0, mad_multiplier=10.0):
    """
    基于学习到的基线分布，检测异常 spike。

    Args:
        iqr_multiplier: IQR 倍数阈值（超过 Q3 + k*IQR 视为异常）
        mad_multiplier: MAD 倍数阈值
    """
    anomalies = []
    for r in trend_rows:
        if r['metric_id'] != metric_id:
            continue

        tid = r['target_id']
        baseline = baselines.get((tid, metric_id))
        if not baseline or baseline['sample_count'] < 3:
            continue

        norm = r['norm']
        med = baseline['median']
        iqr = baseline['iqr']
        mad = baseline['mad']

        # 跳过 baseline 为 0 且 norm 极小的情况
        if med < 1e-10 and norm < 1e-10:
            continue

        # 动态阈值判定
        if iqr > 0:
            iqr_threshold = baseline['q3'] + iqr_multiplier * iqr
        else:
            iqr_threshold = med * 10 if med > 0 else float('inf')

        if mad > 0:
            mad_threshold = med + mad_multiplier * mad
        else:
            mad_threshold = float('inf')

        is_spike_iqr = norm > iqr_threshold and norm > 0
        is_spike_mad = norm > mad_threshold and norm > 0

        # 至少一个阈值触发
        if is_spike_iqr or is_spike_mad:
            # 计算偏离度
            if med > 0:
                deviation_ratio = norm / med
            elif norm > 0:
                deviation_ratio = float('inf')
            else:
                deviation_ratio = 1.0

            if iqr > 0:
                z_score_like = (norm - med) / (iqr / 1.349)  # IQR/1.349 ≈ std for normal dist
            else:
                z_score_like = float('inf') if norm > 0 else 0

            anomaly = {
                'rank': r['rank'],
                'step': r['step'],
                'target_id': tid,
                'target_name': targets[tid]['name'],
                'metric': 'grad_unreduced' if metric_id == 1 else 'grad_reduced',
                'norm': norm,
                'min': r['min'],
                'max': r['max'],
                'mean': r['mean'],
                'baseline_median': med,
                'baseline_iqr': iqr,
                'deviation_ratio': deviation_ratio,
                'z_score_like': z_score_like,
                'trigger': 'iqr+mad' if (is_spike_iqr and is_spike_mad) else
                           ('iqr' if is_spike_iqr else 'mad')
            }
            if 'micro_step' in r:
                anomaly['micro_step'] = r['micro_step']
            anomalies.append(anomaly)

    # 按偏离度降序排列
    anomalies.sort(key=lambda x: x['deviation_ratio'], reverse=True)
    return anomalies


# ─── Micro-step 两步检测 ──────────────────────────────────

def detect_micro_step_spikes(trend_rows, targets, top_per_step=3,
                              delta_iqr_mult=5.0):
    """
    两步法检测 micro_step 累积数据中的 spike:

    Step 1 — 最终态 Top-N:
      对每个 optimizer step, 取最终 micro_step (最后 1 个) 的累积 norm。
      按 norm 绝对值降序排列，取 top N 个 (rank, target)。

    Step 2 — 累积曲线 delta 检测:
      对每个 top suspect，展开 micro_step 累积曲线，组内检测 delta 突变点。

    返回每个 optimizer step 的 top 异常列表。
    """
    if not trend_rows:
        return []

    window = max(r.get('micro_step', 0) for r in trend_rows) + 1
    last_ms = window - 1  # 最终 micro_step

    # ── Step 1: 最终态绝对值 Top-N ──
    final_rows = defaultdict(list)  # optimizer_step -> [(norm, rank, target_id, row)]
    for r in trend_rows:
        ms = r.get('micro_step', 0)
        if ms == last_ms:
            opt_step = r.get('optimizer_step', 0)
            final_rows[opt_step].append((r['norm'], r['rank'], r['target_id'], r))

    suspects = []
    for opt_step in sorted(final_rows.keys()):
        items = sorted(final_rows[opt_step], key=lambda x: x[0], reverse=True)
        for norm, rank, tid, row in items[:top_per_step]:
            suspects.append({
                'rank': rank, 'optimizer_step': opt_step,
                'target_id': tid, 'target_name': targets[tid]['name'],
                'final_norm': norm, 'final_micro_step': last_ms
            })

    # ── Step 2: 累积曲线 delta 检测 ──
    suspect_keys = {(s['target_id'], s['rank'], s['optimizer_step']) for s in suspects}
    ms_index = defaultdict(list)
    for r in trend_rows:
        key = (r['target_id'], r['rank'], r.get('optimizer_step', 0))
        if key in suspect_keys:
            ms_index[key].append((r.get('micro_step', 0), r))

    suspect_map = {(s['target_id'], s['rank'], s['optimizer_step']): s for s in suspects}
    result = []

    for key, items in ms_index.items():
        tid, rank, opt_step = key
        items.sort(key=lambda x: x[0])
        sus_info = suspect_map.get(key)

        # delta 序列
        deltas = []
        for i, (ms, r) in enumerate(items):
            if i == 0:
                delta = r['norm']
            else:
                prev_norm = items[i - 1][1]['norm']
                delta = max(0, r['norm'] - prev_norm)
            deltas.append(delta)

        if len(deltas) < 5:
            continue

        # 组内 IQR 检测 delta 突变
        sd = sorted([d for d in deltas if d > 0])
        if len(sd) < 4:
            continue
        n = len(sd)
        q1 = sd[n // 4]
        q3 = sd[(3 * n) // 4]
        iqr = q3 - q1
        med = sd[n // 2]
        if iqr <= 0:
            continue

        delta_threshold = q3 + delta_iqr_mult * iqr

        # 收集该 suspect 的所有 delta 突变, 取 top 2
        suspect_spikes = []
        for i, delta in enumerate(deltas):
            if delta > delta_threshold and delta > 0:
                r = items[i][1]
                ms = items[i][0]
                deviation = delta / med if med > 0 else 0
                suspect_spikes.append((deviation, r, ms, delta))

        suspect_spikes.sort(key=lambda x: x[0], reverse=True)
        for deviation, r, ms, delta in suspect_spikes[:2]:
            anomaly = {
                'rank': r['rank'], 'step': r['step'],
                'micro_step': ms, 'optimizer_step': opt_step,
                'target_id': tid, 'target_name': targets[tid]['name'],
                'metric': 'grad_unreduced',
                'norm': r['norm'], 'delta': delta,
                'min': r['min'], 'max': r['max'], 'mean': r['mean'],
                'group_delta_median': med, 'group_delta_iqr': iqr,
                'deviation_ratio': deviation,
                'trigger': 'micro_step_delta'
            }
            if sus_info:
                anomaly['suspect_final_norm'] = sus_info['final_norm']
            result.append(anomaly)

    return result


# ─── 前反向判定 ────────────────────────────────────────────

def classify_pass_direction(param_name):
    """
    通过参数名判断前反向。
    趋势 DB 中的参数名是模型参数路径 (weight/bias 等)，其梯度统计 (grad_unreduced/grad_reduced)
    来自反向传播。但 Phase 1 的核心是: 用梯度数据的异常来定位异常前反向。

    返回: 'forward' | 'backward' | 'unknown'
    """
    name_lower = param_name.lower()
    # 反向标识
    backward_markers = ['_grad', 'grad_', '.grad']
    for m in backward_markers:
        if m in name_lower:
            return 'backward'

    # 前向参数 (权重、bias、norm 参数)
    forward_markers = ['.weight', '.bias', 'running_mean', 'running_var',
                       'layer_norm_weight', 'layernorm.weight']
    for m in forward_markers:
        if m in name_lower:
            return 'forward'

    return 'unknown'


def group_anomalies_by_pass(anomalies):
    """按 (step, micro_step, 方向) 聚合异常，辅助判断异常前反向。"""
    groups = defaultdict(list)
    has_micro_step = any('micro_step' in a for a in anomalies)
    for a in anomalies:
        direction = classify_pass_direction(a['target_name'])
        micro_step = a.get('micro_step', 0)
        if has_micro_step:
            key = (a['step'], micro_step, direction)
        else:
            key = (a['step'], direction)
        groups[key].append(a)

    result = []
    for key, items in groups.items():
        entry = {
            'step': key[0],
            'direction': key[-1],
            'anomaly_count': len(items),
            'max_deviation': max(i['deviation_ratio'] for i in items),
            'top_params': sorted(items, key=lambda x: x['deviation_ratio'], reverse=True)[:5],
            'affected_ranks': sorted(set(i['rank'] for i in items)),
            'affected_targets': sorted(set(i['target_name'] for i in items))
        }
        if has_micro_step:
            entry['micro_step'] = key[1]
        result.append(entry)

    return sorted(result, key=lambda x: x['max_deviation'], reverse=True)


# ─── 跨设备对比 ────────────────────────────────────────────

def cross_device_compare(anomalies_a, anomalies_b, label_a='NPU', label_b='GPU'):
    """
    对比两份数据的异常，识别设备特异性模式。
    """
    # 按 (target_name, step) 索引
    idx_a = {(a['target_name'], a['step'], a['rank']): a for a in anomalies_a}
    idx_b = {(b['target_name'], b['step'], b['rank']): b for b in anomalies_b}

    # 找出只在 A 中异常的 (设备特异性)
    only_a = []
    for key, a in idx_a.items():
        if key not in idx_b:
            only_a.append(a)

    only_b = []
    for key, b in idx_b.items():
        if key not in idx_a:
            only_b.append(b)

    # 找出共同的异常，比较幅度
    both = []
    common_keys = set(idx_a.keys()) & set(idx_b.keys())
    for key in common_keys:
        a = idx_a[key]
        b = idx_b[key]
        both.append({
            'target_name': a['target_name'],
            'step': a['step'],
            'rank': a['rank'],
            f'{label_a}_deviation': a['deviation_ratio'],
            f'{label_b}_deviation': b['deviation_ratio'],
            'deviation_diff': a['deviation_ratio'] - b['deviation_ratio'],
            f'{label_a}_dominant': a['deviation_ratio'] > b['deviation_ratio']
        })

    # 结论
    if len(only_a) > len(only_b) and len(only_a) > 0:
        conclusion = f"{label_a} 存在 {len(only_a)} 个特有异常，{label_b} 仅有 {len(only_b)} 个。异常可能是 {label_a} 设备特异的。"
    elif len(only_b) > len(only_a) and len(only_b) > 0:
        conclusion = f"{label_b} 存在 {len(only_b)} 个特有异常，{label_a} 仅有 {len(only_a)} 个。异常可能是 {label_b} 设备特异的。"
    elif both:
        amplified = sum(1 for b in both if b['deviation_diff'] > 0)
        if amplified > len(both) / 2:
            conclusion = f"共有 {len(both)} 个共同异常，其中 {amplified}/{len(both)} 在 {label_a} 上偏离更大。"
        else:
            conclusion = f"共有 {len(both)} 个共同异常，异常模式在两设备间高度一致，排除设备特异性。"
    else:
        conclusion = "无法得出明确的跨设备对比结论。"

    return {
        'only_A': only_a[:20],
        'only_B': only_b[:20],
        'both_count': len(both),
        'both_amplified_on_A': sum(1 for b in both if b.get(f'{label_a}_dominant', False)),
        'conclusion': conclusion
    }


# ─── 根因前反向判定 ──────────────────────────────────────

def infer_root_cause_pass(pass_groups, anomalies):
    """
    根据异常前反向的模式推断根因。
    规则:
      - step(N) 所有前反向普遍偏高 vs step(N-1) → 根因在 step(N-1) 前向
      - step(N) 仅个别前反向异常 → 该前反向为候选根因
    """
    if not pass_groups:
        return {'status': 'no_anomalies_found'}

    # 检查是否存在 step 全前反向偏高
    anomalies_by_step = defaultdict(list)
    for a in anomalies:
        anomalies_by_step[a['step']].append(a)

    reasoning_parts = []

    # 找出最异常的 step
    step_max_dev = {}
    for pg in pass_groups:
        step = pg['step']
        if step not in step_max_dev or pg['max_deviation'] > step_max_dev[step]:
            step_max_dev[step] = pg['max_deviation']

    sorted_steps = sorted(step_max_dev.keys())

    if len(sorted_steps) >= 2:
        # 检查相邻 step 的异常变化
        for i in range(1, len(sorted_steps)):
            prev_step = sorted_steps[i - 1]
            curr_step = sorted_steps[i]
            curr_anomalies = anomalies_by_step[curr_step]
            prev_anomalies = anomalies_by_step[prev_step]

            # 如果当前 step 的异常参数数远大于前一个 step
            curr_count = len(curr_anomalies)
            prev_count = len(prev_anomalies)

            if curr_count > 3 * prev_count and curr_count > 5:
                reasoning_parts.append(
                    f"step-{curr_step} 异常参数 ({curr_count}) 远多于 step-{prev_step} ({prev_count})，"
                    f"推测根因在 step-{prev_step} 的前向过程，其参数变化导致后续 step 梯度普遍异常。"
                )

    # 找出最异常的前反向
    if pass_groups:
        top_pass = pass_groups[0]
        reasoning_parts.append(
            f"最异常前反向: step-{top_pass['step']} {top_pass['direction']}，"
            f"涉及 {top_pass['anomaly_count']} 个参数，"
            f"最大偏离 {top_pass['max_deviation']:.1f}x。"
        )

    return {
        'status': 'completed',
        'top_pass': pass_groups[0] if pass_groups else None,
        'reasoning': ' '.join(reasoning_parts) if reasoning_parts else '见异常前反向列表'
    }


# ─── Phase 2 辅助: 全 rank 最终 norm 收集 ───────────────

def collect_target_rank_norms(trend_rows, anomalies, window, targets=None):
    """
    对 anomalies 中的 top suspect target，收集每个 optimizer step 的
    **所有 rank** 在最终 micro_step 的 norm。
    供 Phase 2 跨 step 全 rank 对比使用。
    """
    # 找出每个 optimizer step 的 top target (final_norm 最大)
    step_top_target = {}
    for a in anomalies:
        os_step = a.get('optimizer_step', 0)
        fn = a.get('suspect_final_norm', 0)
        tname = a.get('target_name', '')
        if os_step not in step_top_target or fn > step_top_target[os_step][1]:
            step_top_target[os_step] = (tname, fn)

    if not step_top_target:
        return None

    # 建 tid → tname 映射
    tid_to_name = {}
    if targets:
        for tid, t in targets.items():
            tid_to_name[tid] = t['name']

    # 收集每个 optimizer step 的 top target 在所有 rank 上的最终 norm
    last_ms = window - 1

    result = {}
    for os_step, (tname, _) in step_top_target.items():
        rank_norms = {}
        for r in trend_rows:
            ms = r.get('micro_step', 0)
            opt = r.get('optimizer_step', 0)
            tid = r.get('target_id')
            name = tid_to_name.get(tid, '')
            if ms == last_ms and opt == os_step and name == tname:
                rank_norms[str(r['rank'])] = r['norm']
        result[str(os_step)] = {
            'target_name': tname,
            'ranks': rank_norms
        }

    return result


# ─── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Spike 检测 - Phase 1: 异常前反向定位')
    parser.add_argument('db_path', nargs='?', help='trend.db 路径')
    parser.add_argument('--compare', nargs=2, metavar=('DB_A', 'DB_B'),
                        help='对比两份 trend.db (如 NPU vs GPU)')
    parser.add_argument('--csv', action='store_true', help='输入为 CSV 格式')
    parser.add_argument('--dump', action='store_true', help='输入为 dump_statistic 目录 (读取 parameters_grad)')
    parser.add_argument('--metric', type=int, default=1, help='使用 metric_id (1=grad_unreduced, 2=grad_reduced)')
    parser.add_argument('--iqr-mult', type=float, default=5.0, help='IQR 倍数阈值 (default: 5)')
    parser.add_argument('--mad-mult', type=float, default=10.0, help='MAD 倍数阈值 (default: 10)')
    parser.add_argument('--output', '-o', help='输出 JSON 文件路径')
    parser.add_argument('--summary', action='store_true', help='仅输出摘要')
    args = parser.parse_args()

    if args.compare:
        # 跨设备对比模式
        loader = load_monitor_csv if args.csv else load_trend_db
        data_a = loader(args.compare[0])
        data_b = loader(args.compare[1])
        if not data_a or not data_b:
            sys.exit(1)

        baselines_a = compute_per_target_baselines(data_a['trend_rows'], data_a['targets'])
        baselines_b = compute_per_target_baselines(data_b['trend_rows'], data_b['targets'])

        anomalies_a = detect_spikes(data_a['trend_rows'], data_a['targets'], baselines_a,
                                    metric_id=args.metric, iqr_multiplier=args.iqr_mult,
                                    mad_multiplier=args.mad_mult)
        anomalies_b = detect_spikes(data_b['trend_rows'], data_b['targets'], baselines_b,
                                    metric_id=args.metric, iqr_multiplier=args.iqr_mult,
                                    mad_multiplier=args.mad_mult)

        passes_a = group_anomalies_by_pass(anomalies_a)
        passes_b = group_anomalies_by_pass(anomalies_b)

        cross = cross_device_compare(anomalies_a, anomalies_b,
                                     label_a=os.path.basename(args.compare[0]),
                                     label_b=os.path.basename(args.compare[1]))

        result = {
            'phase': 1,
            'mode': 'cross_device_compare',
            'db_a': {'path': args.compare[0], 'anomaly_count': len(anomalies_a),
                     'pass_groups': passes_a[:10]},
            'db_b': {'path': args.compare[1], 'anomaly_count': len(anomalies_b),
                     'pass_groups': passes_b[:10]},
            'cross_device_comparison': cross,
            'root_cause_a': infer_root_cause_pass(passes_a, anomalies_a),
            'root_cause_b': infer_root_cause_pass(passes_b, anomalies_b)
        }
    elif args.db_path:
        # 单数据源模式
        if args.dump:
            loader = load_dump_statistic
        elif args.csv:
            loader = load_monitor_csv
        else:
            loader = load_trend_db
        data = loader(args.db_path)
        if not data:
            sys.exit(1)

        # Step 0: 切分分析
        sharding = analyze_sharding(data)

        # dump 数据特殊处理: 无时间维度, 直接用绝对值排序
        if args.dump:
            anomalies = detect_dump_spikes(data['trend_rows'], data['targets'])
            passes = []
            root_cause = {'status': 'dump_snapshot'}
        elif sharding.get('accumulation_detected'):
            # 如果检测到梯度累积, 为每条数据派生 optimizer_step / micro_step
            window = sharding['accumulation_window']
            for r in data['trend_rows']:
                r['optimizer_step'] = r['step'] // window
                r['micro_step'] = r['step'] % window

        # 根据数据类型选择异常检测方式
        if args.dump:
            pass  # 已在上面处理
        elif sharding.get('accumulation_detected'):
            # micro_step 累积数据: 用 delta (增量) 检测
            anomalies = detect_micro_step_spikes(
                data['trend_rows'], data['targets'],
                delta_iqr_mult=args.iqr_mult)
        else:
            baselines = compute_per_target_baselines(data['trend_rows'], data['targets'])
            anomalies = detect_spikes(data['trend_rows'], data['targets'], baselines,
                                      metric_id=args.metric, iqr_multiplier=args.iqr_mult,
                                      mad_multiplier=args.mad_mult)

        passes = group_anomalies_by_pass(anomalies)
        root_cause = infer_root_cause_pass(passes, anomalies)

        # 为 Phase 2 收集 top suspect target 的全 rank 最终 norm
        target_rank_norms = None
        if sharding.get('accumulation_detected') and anomalies:
            target_rank_norms = collect_target_rank_norms(
                data['trend_rows'], anomalies,
                sharding['accumulation_window'],
                data['targets'])

        result = {
            'phase': 1,
            'mode': 'single_db',
            'db_path': args.db_path,
            'sharding_analysis': sharding,
            'target_rank_norms': target_rank_norms,
            'summary': {
                'total_targets': len(data['targets']),
                'total_rows': len(data['trend_rows']),
                'steps': sorted(set(r['step'] for r in data['trend_rows'])),
                'ranks': sorted(set(r['rank'] for r in data['trend_rows'])),
                'pass_unit': sharding['pass_unit'],
                'anomaly_count': len(anomalies),
                'top_pass': passes[0] if passes else None
            },
            'anomalies': anomalies if not args.summary else anomalies[:50],
            'pass_groups': passes[:20],
            'root_cause': root_cause
        }
    else:
        parser.print_help()
        sys.exit(1)

    # 输出
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        print(f"Output written to {args.output}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
