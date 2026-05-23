"""msprobe分析工具公共模块。

提供:
- EXCLUDED_PARAMS / build_excluded_rules / is_key_excluded: 排除规则
- print_kv_table: 双列表格输出
- parse_excluded_apis: 解析命令行 --exclude-api 参数
- is_api_excluded: 判断API是否在用户排除列表中（前缀匹配）
"""

import sys

# 不参与md5比对的API参数（这些参数两次跑的结果本来就不一样）
EXCLUDED_PARAMS = {
    'Distributed.recv':                 {'inputs': [0]},
    'Distributed.irecv':                {'inputs': [0, 'tensor']},
    'Distributed.isend':                {'outputs': [0]},
    'Distributed.all_gather':           {'inputs': [0]},
    'Distributed.gather':               {'inputs': [1]},
    'Distributed.scatter':              {'inputs': [0]},
    'Distributed.reduce_scatter':       {'inputs': [0]},
    'Distributed._reduce_scatter_base': {'inputs': [0]},
    'Distributed._all_gather_base':     {'inputs': [0]},
    'Distributed.all_to_all_single':    {'inputs': [0]},
    'Distributed.all_to_all':           {'inputs': [0]},
    'Distributed.all_gather_into_tensor': {'inputs': [0]},
    'Distributed.reduce_scatter_tensor':  {'inputs': [0]},
    'NPU.npu_fusion_attention':         {'outputs': [4, 5]},
}


def build_excluded_rules():
    """从EXCLUDED_PARAMS构建排除规则列表。

    返回: [(prefix, direction, set_of_str_indices), ...]
    例如: ('Distributed.recv', 'input', {'0'})
    """
    rules = []
    for prefix, config in EXCLUDED_PARAMS.items():
        if 'inputs' in config:
            rules.append((prefix, 'input', set(str(i) for i in config['inputs'])))
        if 'outputs' in config:
            rules.append((prefix, 'output', set(str(i) for i in config['outputs'])))
    return rules


def is_key_excluded(api_name, io_type, index_str, rules):
    """判断指定的(api_name, io_type, index)是否在排除规则中。

    Args:
        api_name: API名称, 如 'NPU.npu_rms_norm.0.backward'
        io_type: 'input' 或 'output'
        index_str: 索引字符串, 如 '0' 或 'tensor'
        rules: build_excluded_rules() 的返回值
    """
    for prefix, direction, indices in rules:
        if direction != io_type:
            continue
        if api_name.startswith(prefix):
            if index_str in indices:
                return True
    return False


def print_kv_table(rank, kv_rows, col_field=20, col_value=120):
    """打印单个rank的双列表格。

    Args:
        rank: rank编号
        kv_rows: [(field_name, value_str), ...]
                   value_str可包含换行符，自动分行显示
        col_field: 字段列宽度
        col_value: 值列宽度
    """
    sep = '+' + '-' * (col_field + 2) + '+' + '-' * (col_value + 2) + '+'
    header = f"| {'Rank ' + str(rank):^{col_field + col_value + 3}} |"

    print()
    print(sep)
    print(header)
    print(sep)
    for field, value in kv_rows:
        value_lines = value.split('\n')
        first = True
        for vl in value_lines:
            f_display = field if first else ''
            print(f"| {f_display:<{col_field}} | {vl:<{col_value}} |")
            first = False
        print(sep)


def parse_excluded_apis():
    """从sys.argv中解析 --exclude-api 参数。

    支持前缀匹配: --exclude-api "Functional.cross_entropy"
    会排除所有以 "Functional.cross_entropy" 开头的API。

    支持多个API: --exclude-api "API_A" "API_B"

    用法:
        python3 script.py <path> --exclude-api "API_NAME"
        python3 script.py <path> --exclude-api "API_A" "API_B"

    返回: set[str]
    """
    excluded = set()
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--exclude-api':
            i += 1
            while i < len(args) and not args[i].startswith('--'):
                excluded.add(args[i])
                i += 1
        else:
            i += 1
    if excluded:
        print(f"排除以下API前缀: {sorted(excluded)}")
    return excluded


def is_api_excluded(api_name, excluded_apis):
    """判断API是否被用户排除（前缀匹配）。

    例如 excluded_apis 包含 "Functional.cross_entropy"，
    则 "Functional.cross_entropy.0.forward" 会被匹配。
    """
    if not excluded_apis:
        return False
    for prefix in excluded_apis:
        if api_name.startswith(prefix):
            return True
    return False
