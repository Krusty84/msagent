#!/usr/bin/env python3
"""检查msprobe数据，确定分析级别（L1或mix）。支持三种输入类型：
  - dump路径（2个参数）：包含dump.json，校验CRC-32和level字段
  - db路径（1个参数）：包含.vis.db文件，校验tb_config表
  - csv/xlsx路径（1个参数）：包含.csv或.xlsx文件，校验字段列"""

import argparse
import csv
import os
import sqlite3


def find_first_dump_file(root_path):
    for dirpath, _, filenames in os.walk(root_path):
        for f in filenames:
            if f == 'dump.json':
                return os.path.join(dirpath, f)
    return None


def detect_path_type(root_path):
    """检测路径类型: 'db', 'csv_xlsx', 或 None"""
    if os.path.isfile(root_path):
        if root_path.endswith('.vis.db'):
            return 'db'
        if root_path.endswith(('.csv', '.xlsx')):
            return 'csv_xlsx'
        return None
    for f in os.listdir(root_path):
        if f.endswith('.vis.db'):
            return 'db'
        if f.endswith(('.xlsx', '.csv')):
            return 'csv_xlsx'
    return None


def _first_file(root_path, exts):
    """在路径中查找第一个匹配扩展名的文件。"""
    if os.path.isfile(root_path):
        return root_path
    for f in sorted(os.listdir(root_path)):
        if f.endswith(exts):
            return os.path.join(root_path, f)
    return None


def validate_csv_xlsx(root_path):
    """校验csv/xlsx文件头是否包含NPU MD5和BENCH MD5列。校验通过返回None，否则返回错误信息。"""
    first = _first_file(root_path, ('.csv', '.xlsx'))
    if not first:
        return "未找到 .csv 或 .xlsx 文件"
    ext = os.path.splitext(first)[1].lower()
    try:
        if ext == '.csv':
            with open(first, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames or []
        else:
            import openpyxl
            wb = openpyxl.load_workbook(first, read_only=True)
            ws = wb.active
            fieldnames = [str(cell.value) if cell.value is not None else '' for cell in next(ws.iter_rows(min_row=1, max_row=1))]
            wb.close()
    except Exception as e:
        return f"读取文件失败: {first}\n  {e}"
    missing = [c for c in ('NPU MD5', 'BENCH MD5') if c not in fieldnames]
    if missing:
        return f"文件 {os.path.basename(first)} 缺少字段: {', '.join(missing)}，没有包含tensor的CRC-32校验值，无法分析确定性问题。"
    return None


def validate_db(root_path):
    """校验db的tb_config表是否包含task='md5'。校验通过返回None，否则返回错误信息。"""
    first = _first_file(root_path, ('.vis.db',))
    if not first:
        return "未找到 .vis.db 文件"
    try:
        conn = sqlite3.connect(first)
        cursor = conn.cursor()
        cursor.execute("SELECT task FROM tb_config")
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        return f"读取db文件失败: {first}\n  {e}"
    task_values = {row[0] for row in rows}
    if 'md5' not in task_values:
        current = task_values if task_values else '空'
        return f"tb_config 表的 task 字段不是 md5，当前值: {current}，没有包含tensor的CRC-32校验值，无法分析确定性问题。"
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
            err = validate_db(args.target)
            if err:
                parser.exit(1, f"错误: db文件校验不通过。\n  {err}\n")
            print('level="mix"')
        elif ptype == 'csv_xlsx':
            err = validate_csv_xlsx(args.target)
            if err:
                parser.exit(1, f"错误: csv/xlsx文件校验不通过。\n  {err}\n")
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
