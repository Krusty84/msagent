#!/usr/bin/env python3
"""检查msprobe数据，确定分析级别（L1或mix）。支持三种输入类型：
  - dump路径（2个参数）：包含dump.json，校验CRC-32和level字段
  - db路径（1个参数）：包含.vis.db文件（mix级别比对结果）
  - csv/xlsx路径（1个参数）：包含.csv或.xlsx文件（L1级别比对结果）"""

import argparse
import os


def find_first_dump_file(root_path):
    for dirpath, _, filenames in os.walk(root_path):
        for f in filenames:
            if f == 'dump.json':
                return os.path.join(dirpath, f)
    return None


def detect_path_type(root_path):
    """检测路径类型: 'db', 'csv_xlsx', 或 None"""
    if os.path.isfile(root_path):
        return 'db' if root_path.endswith('.vis.db') else None
    for f in os.listdir(root_path):
        if f.endswith('.vis.db'):
            return 'db'
        if f.endswith(('.xlsx', '.csv')):
            return 'csv_xlsx'
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
            level = stripped.split(':', 1)[-1].strip().strip('"')
            break

    if level is None:
        raise RuntimeError(f"({label}) dump.json 中未找到 level 字段。\n  文件: {filepath}")
    if level not in ('L1', 'mix'):
        raise RuntimeError(
            f"({label}) 当前dump数据的level=\"{level}\"，不等于\"L1\"或\"mix\"，无法分析确定性问题。\n"
            f"  文件: {filepath}"
        )
    return level


def main():
    parser = argparse.ArgumentParser(description='检查msprobe数据，确定分析级别（L1或mix）。')
    parser.add_argument('target', help='dump target路径，或 db/csv/xlsx 路径')
    parser.add_argument('golden', nargs='?', help='dump golden路径（db/csv/xlsx 路径不需要）')
    args = parser.parse_args()

    if args.golden is None:
        # 单路径模式：db 或 csv/xlsx
        if not os.path.exists(args.target):
            parser.exit(1, f"错误: 路径不存在: {args.target}\n")
        ptype = detect_path_type(args.target)
        if ptype == 'db':
            print('level="mix"')
        elif ptype == 'csv_xlsx':
            print('level="L1"')
        else:
            parser.exit(1, f"错误: 无法识别路径类型。路径中未找到 .vis.db 或 .csv/.xlsx 文件: {args.target}\n")
        return

    target_path, golden_path = args.target, args.golden
    for p in (target_path, golden_path):
        if not os.path.exists(p):
            parser.exit(1, f"错误: 路径不存在: {p}\n")

    target_file = find_first_dump_file(target_path)
    golden_file = find_first_dump_file(golden_path)

    all_pass = True
    levels = {}
    for filepath, label in [(target_file, 'target'), (golden_file, 'golden')]:
        try:
            levels[label] = check_dump_file(filepath, label)
        except RuntimeError as e:
            print(e)
            all_pass = False

    if all_pass and len(levels) == 2 and levels['target'] != levels['golden']:
        print(f"target和golden的level不一致: target=\"{levels['target']}\", golden=\"{levels['golden']}\"")
        all_pass = False

    if all_pass:
        print(f"level=\"{levels['target']}\"")
    else:
        parser.exit(1)


if __name__ == "__main__":
    main()
