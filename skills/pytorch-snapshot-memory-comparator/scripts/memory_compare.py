#!/usr/bin/env python3
"""
PyTorch Memory Snapshot 对比工具

对比两份 PyTorch CUDA/NPU memory snapshot 的 pickle 文件，
分析 Reserved/Allocated 内存峰值差异、扩容事件次数等。

用法:
    python memory_compare.py snap_a.pkl snap_b.pkl              # 文件间对比
    python memory_compare.py snap.pkl --device 0 --device 2     # 文件内卡间对比
    python memory_compare.py snap.pkl --device 0                # 单卡分析
    python memory_compare.py snap.pkl --all-devices             # 全卡概览
    python memory_compare.py snap_a.pkl snap_b.pkl -o report.json  # 输出 JSON
"""

import argparse
import json
import os
import pickle  # nosec B403
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


def fmt_bytes(size_bytes: int) -> str:
    """格式化字节数为人类可读格式"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024**2):.1f} MB"
    else:
        return f"{size_bytes / (1024**3):.2f} GB"


def load_snapshot(filepath: str) -> Dict[str, Any]:
    """加载 PyTorch memory snapshot pickle 文件

    支持两种格式:
    1. list 格式 - torch.cuda.memory._snapshot() 直接导出
    2. dict 格式 - torch.cuda.memory._dump_snapshot() 导出,
       包含 'segments' 和 'device_traces' 两个 key

    返回: {'segments': [...], 'device_traces': [...]|None}
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")

    with open(filepath, "rb") as f:
        data = pickle.load(f)

    if isinstance(data, dict):
        return {
            "segments": data.get("segments", []),
            "device_traces": data.get("device_traces"),
        }
    elif isinstance(data, list):
        return {"segments": data, "device_traces": None}
    else:
        raise ValueError(f"无效的 snapshot 格式: 期望 list 或 dict, 得到 {type(data).__name__}")


def detect_backend(snapshot: List[Dict]) -> str:
    """检测 snapshot 来源后端 (cuda / npu)"""
    if not snapshot:
        return "unknown"

    for seg in snapshot:
        if isinstance(seg, dict):
            if "allocator_name" in seg:
                name = seg["allocator_name"].lower()
                if "npu" in name:
                    return "npu"
                if "cuda" in name:
                    return "cuda"
            if "segment_type" in seg:
                stype = str(seg.get("segment_type", "")).lower()
                if "npu" in stype:
                    return "npu"

    return "cuda"


def get_segments_by_device(snapshot: List[Dict], device_id: Optional[int] = None) -> List[Dict]:
    """按 device 过滤 segments"""
    if device_id is not None:
        return [s for s in snapshot if isinstance(s, dict) and s.get("device") == device_id]
    return [s for s in snapshot if isinstance(s, dict)]


def analyze_snapshot_state(segments: List[Dict]) -> Dict[str, Any]:
    """分析 snapshot 的内存状态"""
    stats = {
        "total_reserved": 0,
        "total_allocated": 0,
        "total_active": 0,
        "total_free": 0,
        "segment_count": 0,
        "block_count": 0,
        "block_states": defaultdict(int),
        "segment_types": defaultdict(int),
        "largest_segment": 0,
        "largest_block": 0,
        "segments": [],
    }

    for seg in segments:
        total_size = seg.get("total_size", 0)
        allocated_size = seg.get("allocated_size", 0)
        active_size = seg.get("active_size", 0)

        stats["total_reserved"] += total_size
        stats["total_allocated"] += allocated_size
        stats["total_active"] += active_size
        stats["total_free"] += total_size - allocated_size
        stats["segment_count"] += 1
        stats["largest_segment"] = max(stats["largest_segment"], total_size)
        stats["segment_types"][seg.get("segment_type", "unknown")] += 1

        blocks = seg.get("blocks", [])
        for blk in blocks:
            if isinstance(blk, dict):
                stats["block_count"] += 1
                stats["block_states"][blk.get("state", "unknown")] += 1
                stats["largest_block"] = max(stats["largest_block"], blk.get("size", 0))

        stats["segments"].append({
            "address": hex(seg.get("address", 0)),
            "total_size": total_size,
            "allocated_size": allocated_size,
            "active_size": active_size,
            "segment_type": seg.get("segment_type", "unknown"),
            "device": seg.get("device", 0),
            "block_count": len(blocks),
        })

    return stats


def compute_expansion_count(segs_a: List[Dict], segs_b: List[Dict]) -> int:
    """计算扩容次数（B 中新增的 segment 数量）"""
    addrs_a = {seg.get("address", 0) for seg in segs_a}
    addrs_b = {seg.get("address", 0) for seg in segs_b}
    return len(addrs_b - addrs_a)


def compute_grown_segments(segs_a: List[Dict], segs_b: List[Dict]) -> List[Dict]:
    """计算扩容的 segment（B 中 size 变大的 segment）"""
    addr_to_size_a = {seg.get("address", 0): seg.get("total_size", 0) for seg in segs_a}
    addr_to_size_b = {seg.get("address", 0): seg.get("total_size", 0) for seg in segs_b}

    grown = []
    for addr in addr_to_size_b:
        if addr in addr_to_size_a and addr_to_size_b[addr] > addr_to_size_a[addr]:
            grown.append({
                "address": hex(addr),
                "size_a": addr_to_size_a[addr],
                "size_b": addr_to_size_b[addr],
                "growth": addr_to_size_b[addr] - addr_to_size_a[addr],
            })

    return sorted(grown, key=lambda x: x["growth"], reverse=True)


def compare_snapshots(
    segs_a: List[Dict], segs_b: List[Dict],
    device_a: Optional[int] = None, device_b: Optional[int] = None,
) -> Dict[str, Any]:
    """对比两份 snapshot"""
    segs_a = get_segments_by_device(segs_a, device_a)
    segs_b = get_segments_by_device(segs_b, device_b)

    stats_a = analyze_snapshot_state(segs_a)
    stats_b = analyze_snapshot_state(segs_b)

    result = {
        "version": "1.0",
        "devices": {"a": device_a, "b": device_b},
        "summary": {
            "reserved_a": stats_a["total_reserved"],
            "reserved_b": stats_b["total_reserved"],
            "reserved_diff": stats_b["total_reserved"] - stats_a["total_reserved"],
            "allocated_a": stats_a["total_allocated"],
            "allocated_b": stats_b["total_allocated"],
            "allocated_diff": stats_b["total_allocated"] - stats_a["total_allocated"],
            "active_a": stats_a["total_active"],
            "active_b": stats_b["total_active"],
            "active_diff": stats_b["total_active"] - stats_a["total_active"],
        },
        "details": {"a": stats_a, "b": stats_b},
        "expansions": {
            "new_segments": compute_expansion_count(segs_a, segs_b),
            "grown_segments": compute_grown_segments(segs_a, segs_b)[:10],
        },
    }

    if stats_a["total_reserved"] > 0:
        result["summary"]["fragmentation_a"] = \
            round((stats_a["total_reserved"] - stats_a["total_allocated"]) / stats_a["total_reserved"] * 100, 1)
    else:
        result["summary"]["fragmentation_a"] = 0

    if stats_b["total_reserved"] > 0:
        result["summary"]["fragmentation_b"] = \
            round((stats_b["total_reserved"] - stats_b["total_allocated"]) / stats_b["total_reserved"] * 100, 1)
    else:
        result["summary"]["fragmentation_b"] = 0

    return result


def print_separator(title: str = ""):
    """打印分隔线"""
    if title:
        print(f"\n{'=' * 60}")
        print(f"  {title}")
        print(f"{'=' * 60}")
    else:
        print(f"{'=' * 60}")


def trend_str(diff: int) -> str:
    """返回趋势字符串"""
    if diff > 0:
        return "[+]"
    elif diff < 0:
        return "[-]"
    return "[=]"


def _short_name(path: str) -> str:
    """从路径中提取短名"""
    return os.path.basename(path) if path else "?"


def print_comparison(result: Dict[str, Any], snap_paths: Tuple[str, ...]):
    """打印对比结果"""
    summary = result["summary"]
    details = result["details"]
    expansions = result["expansions"]

    name_a = _short_name(snap_paths[0]) if len(snap_paths) >= 1 else "A"
    name_b = _short_name(snap_paths[1]) if len(snap_paths) >= 2 else "B"

    print_separator("PyTorch Memory Snapshot 对比报告")
    print(f"\n  A: {name_a}")
    print(f"  B: {name_b}")

    print(f"\n{'指标':<22} {'  A':>14} {'  B':>14} {'差异':>14} {'趋势':>6}")
    print("-" * 72)

    rows = [
        ("Reserved (峰值)", summary["reserved_a"], summary["reserved_b"], summary["reserved_diff"]),
        ("Allocated", summary["allocated_a"], summary["allocated_b"], summary["allocated_diff"]),
        ("Active", summary["active_a"], summary["active_b"], summary["active_diff"]),
    ]

    for label, val_a, val_b, diff in rows:
        print(f"{label:<22} {fmt_bytes(val_a):>14} {fmt_bytes(val_b):>14} {fmt_bytes(abs(diff)):>14} {trend_str(diff):>6}")

    frag_a = summary.get("fragmentation_a", 0)
    frag_b = summary.get("fragmentation_b", 0)
    frag_diff = frag_b - frag_a
    print(f"{'碎片率':<22} {frag_a:>13}% {frag_b:>13}% {abs(frag_diff):>13.1f}% {trend_str(frag_diff):>6}")

    print_separator("统计信息对比")
    stats_a = details["a"]
    stats_b = details["b"]

    print(f"\n{'指标':<22} {'Snapshot A':>16} {'Snapshot B':>16}")
    print("-" * 56)
    stat_rows = [
        ("Segment 数量", stats_a["segment_count"], stats_b["segment_count"]),
        ("Block 数量", stats_a["block_count"], stats_b["block_count"]),
        ("最大 Segment", fmt_bytes(stats_a["largest_segment"]), fmt_bytes(stats_b["largest_segment"])),
        ("最大 Block", fmt_bytes(stats_a["largest_block"]), fmt_bytes(stats_b["largest_block"])),
    ]
    for label, val_a, val_b in stat_rows:
        print(f"{label:<22} {str(val_a):>16} {str(val_b):>16}")

    print(f"\n{'Block 状态':<22} {'Snapshot A':>12} {'Snapshot B':>12} {'差异':>8}")
    print("-" * 56)
    all_states = set(stats_a["block_states"].keys()) | set(stats_b["block_states"].keys())
    for state in sorted(all_states):
        ca = stats_a["block_states"].get(state, 0)
        cb = stats_b["block_states"].get(state, 0)
        diff = cb - ca
        diff_str = f"+{diff}" if diff > 0 else str(diff)
        print(f"{state:<22} {ca:>12} {cb:>12} {diff_str:>8}")

    print(f"\n{'Segment 类型':<22} {'Snapshot A':>12} {'Snapshot B':>12} {'差异':>8}")
    print("-" * 56)
    all_types = set(stats_a["segment_types"].keys()) | set(stats_b["segment_types"].keys())
    for stype in sorted(all_types):
        ca = stats_a["segment_types"].get(stype, 0)
        cb = stats_b["segment_types"].get(stype, 0)
        diff = cb - ca
        diff_str = f"+{diff}" if diff > 0 else str(diff)
        print(f"{stype:<22} {ca:>12} {cb:>12} {diff_str:>8}")

    print_separator("扩容分析")
    print(f"\n新增 Segment 数量: {expansions['new_segments']}")

    grown = expansions.get("grown_segments", [])
    if grown:
        print(f"\n扩容的 Segment Top {min(5, len(grown))}:")
        print(f"{'地址':<20} {'扩容前':>12} {'扩容后':>12} {'增长量':>12}")
        print("-" * 58)
        for g in grown[:5]:
            print(f"{g['address']:<20} {fmt_bytes(g['size_a']):>12} {fmt_bytes(g['size_b']):>12} {fmt_bytes(g['growth']):>12}")
    else:
        print("\n无 Segment 扩容")

    print_separator("结论")
    total_diff = summary["reserved_diff"]
    if total_diff > 1024 ** 3:
        print(f"[WARN] Reserved 内存增长: {fmt_bytes(total_diff)}，请关注内存泄漏风险")
    elif total_diff > 0:
        print(f"[OK] Reserved 内存增长: {fmt_bytes(total_diff)}，在可接受范围内")
    elif total_diff < 0:
        print(f"[OK] Reserved 内存降低: {fmt_bytes(abs(total_diff))}")
    else:
        print("[OK] Reserved 内存未变化")

    if expansions["new_segments"] > 5:
        print(f"[WARN] 新增 {expansions['new_segments']} 个 Segment，扩容频繁")
    else:
        print(f"[OK] 新增 {expansions['new_segments']} 个 Segment，扩容正常")

    print("")


def print_device_overview(snapshot: List[Dict]):
    """打印全卡概览"""
    backend = detect_backend(snapshot)

    devices = defaultdict(list)
    for seg in snapshot:
        if isinstance(seg, dict):
            devices[seg.get("device", 0)].append(seg)

    print_separator(f"Memory Snapshot 全卡概览 ({backend})")
    print(f"\n设备数量: {len(devices)}")

    print(f"\n{'Device':<8} {'Reserved':>12} {'Allocated':>12} {'Active':>10} {'碎片率':>8} {'Segments':>10} {'Blocks':>8}")
    print("-" * 78)

    for dev_id in sorted(devices.keys()):
        segs = devices[dev_id]
        reserved = sum(s.get("total_size", 0) for s in segs)
        allocated = sum(s.get("allocated_size", 0) for s in segs)
        active = sum(s.get("active_size", 0) for s in segs)
        blocks = sum(len(s.get("blocks", [])) for s in segs)

        frag = round((reserved - allocated) / reserved * 100, 1) if reserved > 0 else 0

        print(f"{dev_id:<8} {fmt_bytes(reserved):>12} {fmt_bytes(allocated):>12} {fmt_bytes(active):>10} {frag:>7.1f}% {len(segs):>10} {blocks:>8}")

    print("")


def main():
    parser = argparse.ArgumentParser(
        description="PyTorch Memory Snapshot 对比工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("snapshots", nargs="+", help="snapshot pickle 文件路径 (1个或2个)")
    parser.add_argument("-d", "--device", type=int, action="append", dest="devices",
                        help="指定分析的 device ID (可多次使用)")
    parser.add_argument("-a", "--all-devices", action="store_true", help="全卡概览模式")
    parser.add_argument("-o", "--output", help="输出 JSON 报告路径")

    args = parser.parse_args()

    if len(args.snapshots) > 2:
        print("错误: 最多支持 2 个文件", file=sys.stderr)
        sys.exit(1)

    try:
        if args.all_devices:
            raw = load_snapshot(args.snapshots[0])
            print_device_overview(raw["segments"])
            return

        if len(args.snapshots) == 1:
            if args.devices and len(args.devices) == 2:
                raw = load_snapshot(args.snapshots[0])
                result = compare_snapshots(raw["segments"], raw["segments"],
                                           device_a=args.devices[0], device_b=args.devices[1])
                result["snapshot_path"] = args.snapshots[0]
            elif args.devices and len(args.devices) == 1:
                raw = load_snapshot(args.snapshots[0])
                segs = get_segments_by_device(raw["segments"], args.devices[0])
                stats = analyze_snapshot_state(segs)
                print_separator(f"Device {args.devices[0]} 内存分析")
                print(f"\nReserved:  {fmt_bytes(stats['total_reserved'])}")
                print(f"Allocated: {fmt_bytes(stats['total_allocated'])}")
                print(f"Active:    {fmt_bytes(stats['total_active'])}")
                if stats["total_reserved"] > 0:
                    frag = round((stats["total_reserved"] - stats["total_allocated"]) / stats["total_reserved"] * 100, 1)
                    print(f"碎片率:    {frag}%")
                print(f"Segments:  {stats['segment_count']}")
                print(f"Blocks:    {stats['block_count']}")
                print(f"最大 Segment: {fmt_bytes(stats['largest_segment'])}")
                print("")
                return
            else:
                raw = load_snapshot(args.snapshots[0])
                print_device_overview(raw["segments"])
                return

        elif len(args.snapshots) == 2:
            raw_a = load_snapshot(args.snapshots[0])
            raw_b = load_snapshot(args.snapshots[1])

            device_a = args.devices[0] if args.devices and len(args.devices) >= 1 else None
            device_b = args.devices[1] if args.devices and len(args.devices) >= 2 else device_a

            result = compare_snapshots(raw_a["segments"], raw_b["segments"],
                                       device_a=device_a, device_b=device_b)
            result["snapshot_a"] = args.snapshots[0]
            result["snapshot_b"] = args.snapshots[1]

        print_comparison(result, tuple(args.snapshots))

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False, default=str)
            print(f"报告已保存到 {args.output}")

    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()