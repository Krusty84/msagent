#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
检查 msProbe dump 数据结构，确认 tensor 数据是否可直接读取。

用法：
  python3 inspect_dump.py /path/to/dump/step0/rank0
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_log import add_audit_arg, get_logger


def parse_args():
    p = argparse.ArgumentParser(description="检查 msProbe dump 数据结构")
    p.add_argument("dump_path", help="dump 目录（含 dump.json）")
    p.add_argument("--limit", type=int, default=5, help="显示前 N 条记录")
    add_audit_arg(p)
    return p.parse_args()


def main():
    args = parse_args()
    logger = get_logger(args)

    dump_file = None
    for root, _, files in os.walk(args.dump_path):
        if "dump.json" in files:
            dump_file = os.path.join(root, "dump.json")
            break

    if not dump_file:
        print(f"[ERROR] 未找到 dump.json: {args.dump_path}")
        raise SystemExit(1)

    print(f"=== dump.json: {dump_file} ===")
    print(f"文件大小: {os.path.getsize(dump_file)} bytes")
    print()

    with open(dump_file, "r", encoding="utf-8") as f:
        content = f.read()

    # dump.json 通常是 JSONL 或单行 JSON
    lines = [l for l in content.split("\n") if l.strip()]
    print(f"总行数: {len(lines)}")
    print()

    count = 0
    for line in lines:
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        count += 1
        if count > args.limit:
            break

        print(f"--- 记录 {count} ---")
        for k, v in rec.items():
            if isinstance(v, dict):
                print(f"  {k}:")
                for kk, vv in v.items():
                    val_str = str(vv)
                    if len(val_str) > 100:
                        val_str = val_str[:100] + "..."
                    print(f"    {kk}: {val_str}")
            else:
                val_str = str(v)
                if len(val_str) > 100:
                    val_str = val_str[:100] + "..."
                print(f"  {k}: {val_str}")
        print()

    # 检查是否有 tensor 文件
    tensor_files = list(Path(args.dump_path).rglob("*.npy")) + \
                   list(Path(args.dump_path).rglob("*.pt")) + \
                   list(Path(args.dump_path).rglob("*.bin"))
    print(f"=== tensor 文件数: {len(tensor_files)} ===")
    for tf in tensor_files[:5]:
        print(f"  {tf}")

    # 检查 construct.json
    construct_file = None
    for root, _, files in os.walk(args.dump_path):
        if "construct.json" in files:
            construct_file = os.path.join(root, "construct.json")
            break
    if construct_file:
        print(f"\n=== construct.json: {construct_file} ===")
        with open(construct_file, "r", encoding="utf-8") as f:
            c = json.load(f)
        if isinstance(c, dict):
            print(f"顶层 keys: {list(c.keys())[:10]}")
            if "level" in c:
                print(f"level: {c['level']}")
            if "task" in c:
                print(f"task: {c['task']}")
            if "summary_mode" in c:
                print(f"summary_mode: {c['summary_mode']}")

    logger.log("inspect_dump", {
        "dump_path": args.dump_path,
        "dump_file": dump_file,
        "tensor_files_count": len(tensor_files),
        "construct_file": construct_file,
    })


if __name__ == "__main__":
    main()
