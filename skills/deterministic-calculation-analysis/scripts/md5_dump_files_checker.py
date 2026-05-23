#!/usr/bin/env python3
"""检查msprobe dump数据是否包含CRC-32校验值和正确的level（L1或mix）。"""

import os
import sys


def find_first_dump_file(root_path):
    for dirpath, _, filenames in os.walk(root_path):
        for f in filenames:
            if f == 'dump.json':
                return os.path.join(dirpath, f)
    return None


def check_dump_file(filepath, label):
    """检查dump.json前100行，返回level值或抛出异常。"""
    if not filepath:
        raise RuntimeError(f"({label}) 未找到 dump.json 文件")

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = [f.readline() for _ in range(100)]
    except Exception as e:
        raise RuntimeError(f"({label}) 读取文件失败: {filepath}\n  {e}")

    content = ''.join(lines)

    # 检查是否有 "md5" 字段
    if '"md5":' not in content:
        raise RuntimeError(
            f"({label}) 当前dump数据没有包含tensor的CRC-32校验值，无法分析确定性问题。\n"
            f"  文件: {filepath}"
        )

    # 查找 level 字段
    level = None
    for line in lines:
        stripped = line.strip().rstrip(',')
        if stripped.startswith('"level"'):
            # 提取 value，格式: "level": "L1" 或 "level": "mix"
            parts = stripped.split(':', 1)
            if len(parts) == 2:
                val = parts[1].strip().strip('"')
                level = val
            break

    if level is None:
        raise RuntimeError(
            f"({label}) dump.json 中未找到 level 字段。\n"
            f"  文件: {filepath}"
        )

    if level not in ('L1', 'mix'):
        raise RuntimeError(
            f"({label}) 当前dump数据的level=\"{level}\"，不等于\"L1\"或\"mix\"，无法分析确定性问题。\n"
            f"  文件: {filepath}"
        )

    return level


def main():
    if len(sys.argv) < 3:
        print("用法: python3 md5_dump_files_checker.py <target_path> <golden_path>")
        print("示例: python3 md5_dump_files_checker.py dump_L1_1 dump_L1_2")
        sys.exit(1)

    target_path = sys.argv[1]
    golden_path = sys.argv[2]

    for p in (target_path, golden_path):
        if not os.path.exists(p):
            print(f"错误: 路径不存在: {p}")
            sys.exit(1)

    target_file = find_first_dump_file(target_path)
    golden_file = find_first_dump_file(golden_path)

    all_pass = True
    levels = {}
    for filepath, label in [(target_file, 'target'), (golden_file, 'golden')]:
        try:
            level = check_dump_file(filepath, label)
            levels[label] = level
        except RuntimeError as e:
            print(e)
            all_pass = False

    # 检查两个路径的level是否一致
    if all_pass and len(levels) == 2 and levels['target'] != levels['golden']:
        print(f"target和golden的level不一致: target=\"{levels['target']}\", golden=\"{levels['golden']}\"")
        all_pass = False

    if all_pass:
        level = levels.get('target', 'unknown')
        print(f"level=\"{level}\"")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
