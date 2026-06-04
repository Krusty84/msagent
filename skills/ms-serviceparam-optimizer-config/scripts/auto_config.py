#!/usr/bin/env python3
"""自动配置脚本 - 根据场景自动修改 config.toml"""

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Any, Optional


# 场景配置模板
SCENARIOS = {
    "quick-test": {
        "description": "快速测试场景",
        "n_particles": 5,
        "iters": 3,
        "ttft_penalty": 1,
        "tpot_penalty": 1,
        "ttft_slo": 2.0,
        "tpot_slo": 0.1,
        "target_field_ranges": {
            "max_batch_size": {"min": 10, "max": 100},
            "max_num_seqs": {"min": 8, "max": 32},
        }
    },
    "standard": {
        "description": "标准寻优场景",
        "n_particles": 15,
        "iters": 10,
        "ttft_penalty": 1,
        "tpot_penalty": 1,
        "ttft_slo": 2.0,
        "tpot_slo": 0.05,
        "target_field_ranges": {
            "max_batch_size": {"min": 10, "max": 400},
            "max_num_seqs": {"min": 8, "max": 64},
        }
    },
    "deep-optimize": {
        "description": "深度寻优场景",
        "n_particles": 30,
        "iters": 20,
        "ttft_penalty": 3,
        "tpot_penalty": 3,
        "ttft_slo": 1.0,
        "tpot_slo": 0.03,
        "target_field_ranges": {
            "max_batch_size": {"min": 10, "max": 1000},
            "max_num_seqs": {"min": 8, "max": 128},
        }
    },
    "ttft-priority": {
        "description": "首token时延优先",
        "n_particles": 15,
        "iters": 10,
        "ttft_penalty": 10,
        "tpot_penalty": 0,
        "ttft_slo": 0.5,
        "tpot_slo": 0.1,
        "target_field_ranges": {
            "max_batch_size": {"min": 10, "max": 200},
            "max_prefill_batch_size": {"min": 0.3, "max": 0.7},
        }
    },
    "tpot-priority": {
        "description": "非首token时延优先",
        "n_particles": 15,
        "iters": 10,
        "ttft_penalty": 0,
        "tpot_penalty": 10,
        "ttft_slo": 2.0,
        "tpot_slo": 0.02,
        "target_field_ranges": {
            "max_batch_size": {"min": 50, "max": 400},
            "max_prefill_batch_size": {"min": 0.1, "max": 0.3},
        }
    },
    "throughput": {
        "description": "吞吐优先",
        "n_particles": 20,
        "iters": 10,
        "ttft_penalty": 0,
        "tpot_penalty": 0,
        "ttft_slo": 5.0,
        "tpot_slo": 0.2,
        "success_rate_penalty": 10,
        "target_field_ranges": {
            "max_batch_size": {"min": 100, "max": 1000},
            "max_num_seqs": {"min": 64, "max": 256},
        }
    }
}


def parse_time_budget(time_str: str) -> int:
    """解析时间预算字符串，返回分钟数"""
    if not time_str:
        return None
    
    time_str = time_str.lower().strip()
    
    if time_str.endswith('h'):
        return int(time_str[:-1]) * 60
    elif time_str.endswith('m'):
        return int(time_str[:-1])
    elif time_str.endswith('d'):
        return int(time_str[:-1]) * 24 * 60
    else:
        try:
            return int(time_str)
        except ValueError:
            return None


def calculate_optimal_params(time_budget_minutes: int, single_test_minutes: int = 10) -> Dict[str, int]:
    """根据时间预算计算最优的 n_particles 和 iters
    
    注意：每个种子会拉起两次服务和测试（预热 + 正式测试），
    因此实际单组耗时 = 2 × single_test_minutes
    """
    # 每个种子实际耗时 = 2 × (服务启动 + 测评)
    actual_single_test_minutes = single_test_minutes * 2
    
    # 预留 20% 缓冲时间
    effective_budget = int(time_budget_minutes * 0.8)
    
    # 计算可运行的总组数
    total_groups = effective_budget // actual_single_test_minutes
    
    # 推荐配置：iters 约为 n_particles 的 1/2
    # n_particles * iters ≈ total_groups
    # 设 iters = n_particles / 2
    # 则 n_particles * (n_particles / 2) ≈ total_groups
    # n_particles ≈ sqrt(2 * total_groups)
    
    import math
    n_particles = min(int(math.sqrt(2 * total_groups)), 50)
    iters = max(min(n_particles // 2, 20), 3)
    n_particles = max(min(n_particles, 100), 5)
    
    print(f"时间预算: {time_budget_minutes}分钟")
    print(f"单次测试预估: {single_test_minutes}分钟")
    print(f"实际单组耗时 (×2): {actual_single_test_minutes}分钟")
    print(f"可运行总组数: ~{total_groups}")
    
    return {"n_particles": n_particles, "iters": iters}


def update_config_value(content: str, key: str, value: Any) -> str:
    """更新配置文件中的单个值"""
    # 处理不同类型的值
    if isinstance(value, str):
        value_str = f'"{value}"'
    elif isinstance(value, bool):
        value_str = str(value).lower()
    elif isinstance(value, (int, float)):
        value_str = str(value)
    else:
        value_str = str(value)
    
    # 匹配 key = value 或 key=value 格式
    pattern = rf'^{re.escape(key)}\s*=\s*.+$'
    replacement = f'{key} = {value_str}'
    
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        # 如果键不存在，添加到文件开头
        content = f'{key} = {value_str}\n{content}'
    
    return content


def update_target_field(content: str, field_name: str, updates: Dict[str, Any]) -> str:
    """更新 target_field 中的参数"""
    # 查找 [[xxx.target_field]] 块
    pattern = rf'(\[\[.*?\.target_field\]\][^\[]*?name\s*=\s*["\']{re.escape(field_name)}["\'][^\[]*?)'
    
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        print(f"⚠ 未找到参数: {field_name}")
        return content
    
    block = match.group(1)
    new_block = block
    
    for key, value in updates.items():
        if isinstance(value, str):
            value_str = f'"{value}"'
        elif isinstance(value, bool):
            value_str = str(value).lower()
        else:
            value_str = str(value)
        
        # 更新块内的值
        key_pattern = rf'^{re.escape(key)}\s*=\s*.+$'
        key_replacement = f'{key} = {value_str}'
        
        if re.search(key_pattern, new_block, re.MULTILINE):
            new_block = re.sub(key_pattern, key_replacement, new_block, flags=re.MULTILINE)
    
    content = content.replace(block, new_block)
    return content


def apply_scenario_config(content: str, scenario: str, args) -> str:
    """应用场景配置"""
    config = SCENARIOS[scenario].copy()
    
    # 根据时间预算调整参数
    if args.time_budget:
        time_minutes = parse_time_budget(args.time_budget)
        if time_minutes:
            optimal = calculate_optimal_params(time_minutes)
            config.update(optimal)
            print(f"根据时间预算 {args.time_budget} 调整: n_particles={optimal['n_particles']}, iters={optimal['iters']}")
    
    # 应用命令行覆盖的参数
    if args.ttft_slo is not None:
        config["ttft_slo"] = args.ttft_slo
    if args.tpot_slo is not None:
        config["tpot_slo"] = args.tpot_slo
    
    # 更新基础参数
    basic_params = ["n_particles", "iters", "ttft_penalty", "tpot_penalty", 
                    "ttft_slo", "tpot_slo", "success_rate_penalty"]
    
    for param in basic_params:
        if param in config:
            content = update_config_value(content, param, config[param])
    
    # 更新 target_field 范围
    if "target_field_ranges" in config:
        for field_name, ranges in config["target_field_ranges"].items():
            content = update_target_field(content, field_name, ranges)
    
    # 更新引擎相关配置
    if args.engine == "vllm":
        content = update_vllm_config(content, args)
    elif args.engine == "mindie":
        content = update_mindie_config(content, args)
    
    # 更新测评工具配置
    if args.benchmark:
        content = update_benchmark_config(content, args)
    
    return content


def update_vllm_config(content: str, args) -> str:
    """更新 VLLM 相关配置"""
    if args.model:
        content = update_config_section(content, "vllm.command", "model", args.model)
    if args.served_name:
        content = update_config_section(content, "vllm.command", "served_model_name", args.served_name)
    if args.host:
        content = update_config_section(content, "vllm.command", "host", args.host)
    if args.port:
        content = update_config_section(content, "vllm.command", "port", args.port)
    
    # 更新 vllm_benchmark 的对应参数
    if args.model:
        content = update_config_section(content, "vllm_benchmark.command", "model", args.model)
    if args.served_name:
        content = update_config_section(content, "vllm_benchmark.command", "served_model_name", args.served_name)
    if args.host:
        content = update_config_section(content, "vllm_benchmark.command", "host", args.host)
    if args.port:
        content = update_config_section(content, "vllm_benchmark.command", "port", str(args.port))
    
    return content


def update_mindie_config(content: str, args) -> str:
    """更新 MindIE 相关配置"""
    # MindIE 主要通过 target_field 配置
    return content


def update_benchmark_config(content: str, args) -> str:
    """更新测评工具配置"""
    # 根据测评工具类型更新
    return content


def update_config_section(content: str, section: str, key: str, value: Any) -> str:
    """更新特定配置段的值"""
    # 查找配置段
    section_pattern = rf'\[{re.escape(section)}\](.*?)(?=\[|$)'
    match = re.search(section_pattern, content, re.DOTALL)
    
    if not match:
        return content
    
    section_content = match.group(1)
    
    # 更新段内的值
    if isinstance(value, str):
        value_str = f'"{value}"'
    else:
        value_str = str(value)
    
    key_pattern = rf'^{re.escape(key)}\s*=\s*.+$'
    key_replacement = f'{key} = {value_str}'
    
    if re.search(key_pattern, section_content, re.MULTILINE):
        new_section = re.sub(key_pattern, key_replacement, section_content, flags=re.MULTILINE)
        content = content.replace(section_content, new_section)
    else:
        # 键不存在，添加到段末尾
        new_section = section_content.rstrip() + f'\n{key} = {value_str}\n'
        content = content.replace(section_content, new_section)
    
    return content


def generate_target_field_block(name: str, config_position: str, dtype: str,
                                 min_val=None, max_val=None, value=None,
                                 dtype_param=None, enum_values=None,
                                 factories_config=None) -> str:
    """生成 target_field 配置块
    
    Args:
        name: 参数名
        config_position: 配置位置（通常为 "env"）
        dtype: 参数类型
        min_val: 最小值（搜索参数）
        max_val: 最大值（搜索参数）
        value: 固定值（固定参数）
        dtype_param: 类型参数（ratio/factories/times）
        enum_values: 枚举值列表
        factories_config: factories 配置 JSON
    
    Returns:
        TOML 格式的配置块字符串
    """
    lines = [f'[[target_field]]']
    lines.append(f'name = "{name}"')
    lines.append(f'config_position = "{config_position}"')
    
    # 根据类型生成不同字段
    if dtype == "enum" and enum_values:
        lines.append(f'dtype = "enum"')
        
        # 处理枚举值：检测 JSON 对象并添加 shell 单引号
        import json
        try:
            enum_list = json.loads(enum_values)
            processed_values = []
            for v in enum_list:
                if isinstance(v, str) and '{' in v and '}' in v:
                    # 包含 JSON 对象，需要用单引号包裹 JSON 部分
                    # 例如: --config {"key": "value"} -> --config '{"key": "value"}'
                    import re
                    # 匹配 JSON 对象部分并用单引号包裹
                    processed = re.sub(r'(\{.*\})', r"'\1'", v)
                    processed_values.append(processed)
                else:
                    processed_values.append(v)
            # 重新序列化为 TOML 格式
            toml_value = json.dumps(processed_values, ensure_ascii=False)
            lines.append(f'dtype_param = {toml_value}')
        except (json.JSONDecodeError, TypeError):
            lines.append(f'dtype_param = {enum_values}')
        
        # 对于字符串枚举，必须指定 value，否则 Pydantic 会使用默认的浮点数 0.0 导致类型错误
        if value is not None:
            # 处理 value 中的 JSON 格式
            if isinstance(value, str) and '{' in value and '}' in value:
                import re
                value = re.sub(r'(\{.*\})', r"'\1'", value)
            lines.append(f'value = {value if isinstance(value, int) else repr(value)}')
        else:
            # 解析 enum_values 并使用第一个非空值作为默认值
            try:
                enum_list = json.loads(enum_values)
                if enum_list:
                    # 优先选择第一个非空值作为默认值
                    default_val = None
                    for v in enum_list:
                        if v:  # 非空值
                            default_val = v
                            break
                    # 如果所有值都为空，则使用第一个值
                    if default_val is None:
                        default_val = enum_list[0]
                    # 处理 JSON 格式的值
                    if isinstance(default_val, str) and '{' in default_val and '}' in default_val:
                        import re
                        default_val = re.sub(r'(\{.*\})', r"'\1'", default_val)
                    if isinstance(default_val, str):
                        lines.append(f'value = {repr(default_val)}')
                    else:
                        lines.append(f'value = {default_val}')
            except (json.JSONDecodeError, TypeError):
                pass
    elif dtype == "ratio":
        lines.append(f'min = {min_val if min_val is not None else 0}')
        lines.append(f'max = {max_val if max_val is not None else 1}')
        lines.append(f'dtype = "ratio"')
        if dtype_param:
            lines.append(f'dtype_param = "{dtype_param}"')
        if value is not None:
            lines.append(f'value = {value}')
    elif dtype == "factories":
        lines.append(f'min = 0')
        lines.append(f'max = 0')
        lines.append(f'dtype = "factories"')
        if factories_config:
            import json
            if isinstance(factories_config, str):
                config_dict = json.loads(factories_config)
            else:
                config_dict = factories_config
            lines.append(f'dtype_param = {{target_name = "{config_dict.get("target_name")}", product = {config_dict.get("product")}, dtype = "{config_dict.get("dtype", "int")}"}}')
        if value is not None:
            lines.append(f'value = {value}')
    elif dtype == "times":
        lines.append(f'dtype = "times"')
        if dtype_param:
            import json
            if isinstance(dtype_param, str):
                config_dict = json.loads(dtype_param)
            else:
                config_dict = dtype_param
            lines.append(f'dtype_param = {{target_name = "{config_dict.get("target_name")}", product = {config_dict.get("product")}, dtype = "{config_dict.get("dtype", "int")}"}}')
        if value is not None:
            lines.append(f'value = {value}')
    elif dtype == "range":
        # range 类型：需要 min, max, dtype_param(步长)
        if min_val is None or max_val is None:
            raise ValueError("range 类型必须指定 --min 和 --max")
        if dtype_param is None:
            raise ValueError("range 类型必须指定 --dtype-param 作为步长")
        lines.append(f'min = {min_val}')
        lines.append(f'max = {max_val}')
        lines.append(f'dtype = "range"')
        lines.append(f'dtype_param = {dtype_param}')
        if value is not None:
            lines.append(f'value = {value}')
    else:
        # int, float, bool, str
        if min_val is not None:
            lines.append(f'min = {min_val}')
        if max_val is not None:
            lines.append(f'max = {max_val}')
        lines.append(f'dtype = "{dtype}"')
        if value is not None:
            if dtype == "str":
                lines.append(f'value = "{value}"')
            elif dtype == "bool":
                lines.append(f'value = {str(value).lower()}')
            else:
                lines.append(f'value = {value}')
    
    return '\n'.join(lines)


def find_last_target_field_position(content: str, engine: str) -> int:
    """找到引擎最后一个 [[engine.target_field]] 块的结束位置
    
    Returns:
        最后一个 target_field 块结束的位置，如果没找到返回 -1
    """
    # 精确匹配 [[engine.target_field]]，不匹配 [[engine_xxx.target_field]]
    pattern = rf'\[\[{re.escape(engine)}\.target_field\]\]'
    matches = list(re.finditer(pattern, content))
    
    if not matches:
        return -1
    
    # 找到最后一个匹配
    last_match = matches[-1]
    
    # 从最后一个 [[engine.target_field]] 开始，找到该块的结束位置
    # 块结束于下一个 [[xxx]] 或 [xxx] 或 # --- 注释分隔符 或文件末尾
    start_pos = last_match.start()
    remaining = content[start_pos:]
    
    # 查找下一个配置段的开始（[[xxx]] 或 [xxx] 或 # --- 注释分隔符）
    next_section = re.search(r'\n(?=\[\[|\[(?!\[)|# -+)', remaining[1:])  # 跳过当前 [[
    
    if next_section:
        return start_pos + 1 + next_section.start()
    else:
        # 没有下一个段落，返回文件末尾位置（去除尾部空行）
        return len(content.rstrip()) + 1


def find_engine_command_end(content: str, engine: str) -> int:
    """找到 [engine.command] 段落的结束位置（在下一个段落或注释分隔符之前）
    
    Returns:
        段落结束位置，如果没找到返回 -1
    """
    # 找到 [engine.command] 段落
    pattern = rf'^\[{re.escape(engine)}\.command\]'
    match = re.search(pattern, content, re.MULTILINE)
    if not match:
        return -1
    
    start_pos = match.start()
    remaining = content[start_pos:]
    
    # 找到下一个段落分隔符（[[xxx]]/ [xxx] 或注释分隔行 # ----）
    next_section = re.search(r'\n(?=\[\[|\[(?!\[)|# -+)', remaining[1:])
    
    if next_section:
        return start_pos + 1 + next_section.start()
    else:
        return len(content.rstrip())


def find_engine_section_position(content: str, engine: str) -> int:
    """找到 [engine] 或 [engine.command] 段落的位置
    
    Returns:
        段落开始位置，如果没找到返回 -1
    """
    # 精确匹配 [engine] 或 [engine.xxx]，避免匹配 [engine_benchmark]
    pattern = rf'^\[{re.escape(engine)}(?:\.[^\]]+)?\]'
    match = re.search(pattern, content, re.MULTILINE)
    return match.start() if match else -1


def find_target_field_by_name(content: str, engine: str, param_name: str) -> Optional[tuple]:
    """查找指定参数名的 target_field 块

    Returns:
        (start_pos, end_pos) 元组，如果没找到返回 None
    """
    # 找到参数所在的 target_field 块
    pattern = rf'(\[\[{re.escape(engine)}\.target_field\]\][^\[]*?name\s*=\s*["\']{re.escape(param_name)}["\'][^\[]*?)'
    match = re.search(pattern, content, re.DOTALL)

    if match:
        start_pos = match.start()
        block = match.group(1)

        # 找到块的结束位置
        next_block = re.search(r'\n(?=\[\[|\[(?!\[))', content[start_pos + len(block):])
        if next_block:
            end_pos = start_pos + len(block) + next_block.start()
        else:
            end_pos = len(content.rstrip()) + 1

        return (start_pos, end_pos)

    return None


def add_target_field(content: str, engine: str, block: str, force: bool = False) -> str:
    """添加 target_field 配置块到配置文件

    Args:
        content: 配置文件内容
        engine: 引擎名（vllm, mindie 等）
        block: 配置块内容
        force: 是否强制覆盖已存在的参数

    Returns:
        更新后的配置内容
    """
    # 替换 [[target_field]] 为 [[engine.target_field]]
    engine_block = block.replace('[[target_field]]', f'[[{engine}.target_field]]')

    # 提取参数名
    name_match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', engine_block)
    if name_match:
        param_name = name_match.group(1)

        # 检查参数是否已存在
        existing = find_target_field_by_name(content, engine, param_name)
        if existing:
            if force:
                # 强制模式：删除旧块，添加新块
                start_pos, end_pos = existing
                content = content[:start_pos] + engine_block + '\n' + content[end_pos:]
                print(f"  ✓ 已覆盖已有参数: {param_name}")
                return content
            else:
                # 非强制模式：检查是否有需要更新的字段
                print(f"  ⚠ 参数 {param_name} 已存在于配置中，使用 --force 覆盖")
                # 这里应该改为更新现有块而非直接返回
                start_pos, end_pos = existing
                existing_block = content[start_pos:end_pos]
                updated_block = update_existing_target_field(existing_block, engine_block)
                content = content[:start_pos] + updated_block + content[end_pos:]
                print(f"  ✓ 已更新已有参数块: {param_name}")
                return content

    # 策略：找到该引擎最后一个 [[engine.target_field]] 块的位置，在其后插入
    last_field_pos = find_last_target_field_position(content, engine)

    if last_field_pos > 0:
        # 在最后一个 target_field 块之后插入
        content = content[:last_field_pos] + '\n' + engine_block + '\n' + content[last_field_pos:]
    else:
        # 没有找到已有的 target_field，在 [engine.command] 段落后插入
        command_end_pos = find_engine_command_end(content, engine)
        if command_end_pos > 0:
            # 在 [engine.command] 段落末尾插入（注释分隔符之前）
            content = content[:command_end_pos] + '\n' + engine_block + '\n' + content[command_end_pos:]
        else:
            # 查找 [engine] 段落
            section_pos = find_engine_section_position(content, engine)
            if section_pos >= 0:
                remaining = content[section_pos:]
                next_line = remaining.find('\n')
                if next_line >= 0:
                    after_header = section_pos + next_line + 1
                    next_section = re.search(r'^(?=\[|# -+)', content[after_header:], re.MULTILINE)
                    if next_section:
                        insert_pos = after_header + next_section.start()
                    else:
                        insert_pos = len(content.rstrip())
                    content = content[:insert_pos] + '\n' + engine_block + '\n' + content[insert_pos:]
                else:
                    content = content.rstrip() + '\n\n' + engine_block + '\n'
            else:
                # 在文件末尾插入
                content = content.rstrip() + '\n\n' + engine_block + '\n'

    return content


def update_existing_target_field(existing_block: str, new_block: str) -> str:
    """更新已存在的 target_field 块

    Args:
        existing_block: 现有的配置块内容
        new_block: 新的配置块内容

    Returns:
        更新后的配置块
    """
    # 解析新块中的字段
    new_fields = {}
    for line in new_block.split('\n'):
        line = line.strip()
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            new_fields[key] = value

    # 更新现有块中的字段
    result = existing_block
    for key, new_value in new_fields.items():
        if key == 'name':
            # name 字段不更新
            continue

        # 构建替换模式
        pattern = rf'^{re.escape(key)}\s*=\s*.+$'
        replacement = f'{key} = {new_value}'

        if re.search(pattern, result, re.MULTILINE):
            result = re.sub(pattern, replacement, result, flags=re.MULTILINE)
        else:
            # 字段不存在，添加到块末尾
            result = result.rstrip() + '\n' + f'{key} = {new_value}'

    return result


def update_others_with_param(content: str, engine: str, param_name: str, cli_arg: str = None, force: bool = False) -> str:
    """在 others 中更新参数引用

    Args:
        content: 配置文件内容
        engine: 引擎名（vllm, mindie 等）
        param_name: 参数名（如 TEST）
        cli_arg: CLI 参数名（如 --test），如果为 None 则自动从 param_name 生成
                  如果为空字符串 "" 则只添加 $PARAM_NAME（用于枚举值本身包含完整参数的情况）
        force: 是否强制更新已存在的参数引用

    Returns:
        更新后的配置内容
    """
    env_ref = f"${param_name}"

    # 处理 cli_arg
    if cli_arg is None:
        # 默认将参数名转为小写并加上 -- 前缀
        cli_arg = f"--{param_name.lower().replace('_', '-')}"
        param_str = f"{cli_arg} {env_ref}"
    elif cli_arg == "":
        # 空字符串表示只添加变量引用（用于枚举值本身包含完整参数）
        param_str = env_ref
    else:
        param_str = f"{cli_arg} {env_ref}"

    # 查找 [engine.command] 段落中的 others
    section_pattern = rf'(\[{re.escape(engine)}\.command\].*?)(others\s*=\s*")(.*?)("\s*)(?=\n|$)'
    match = re.search(section_pattern, content, re.DOTALL)

    if match:
        prefix = match.group(1)
        others_key = match.group(2)
        others_value = match.group(3)
        suffix = match.group(4)

        # 检查是否已经包含该参数引用
        if env_ref in others_value:
            if not force:
                print(f"  ⚠ others 中已包含 {env_ref}，跳过（使用 --force 强制规范化）")
                return content

            # 强制模式：移除已有的同名引用后，追加规范格式
            ref_pattern = rf'(?<!\S)(?:--[^\s"]+\s+)?{re.escape(env_ref)}(?!\S)'
            normalized = re.sub(ref_pattern, '', others_value)
            normalized = re.sub(r'\s+', ' ', normalized).strip()
            new_others_value = f"{normalized} {param_str}".strip()
            print(f"  ✓ 已规范化 [{engine}.command].others 中的参数: {param_str}")
        else:
            # 在 others 末尾添加参数
            new_others_value = f"{others_value.rstrip()} {param_str}".strip()
            print(f"  ✓ 已在 [{engine}.command].others 中添加 {param_str}")

        new_section = prefix + others_key + new_others_value + suffix

        # 替换原来的匹配内容
        content = content[:match.start()] + new_section + content[match.end():]
    else:
        print(f"  ⚠ 未找到 [{engine}.command].others，请手动添加 {param_str}")

    return content


def add_search_param(content: str, args) -> str:
    """添加搜索参数"""
    block = generate_target_field_block(
        name=args.param_name,
        config_position=args.config_position or "env",
        dtype=args.dtype,
        min_val=args.min,
        max_val=args.max,
        value=args.value,
        dtype_param=args.dtype_param,
        enum_values=args.enum_values,
        factories_config=args.factories_config
    )
    content = add_target_field(content, args.engine, block, force=args.force)

    # 自动在 others 中添加参数引用
    if hasattr(args, 'cli_arg') and args.cli_arg is not None:
        content = update_others_with_param(content, args.engine, args.param_name, args.cli_arg, force=args.force)
    else:
        content = update_others_with_param(content, args.engine, args.param_name, force=args.force)

    return content


def add_fixed_param(content: str, args) -> str:
    """添加固定参数"""
    block = generate_target_field_block(
        name=args.param_name,
        config_position=args.config_position or "env",
        dtype=args.dtype,
        value=args.value
    )
    return add_target_field(content, args.engine, block, force=args.force)


def set_vllm_command(content: str, args) -> str:
    """配置 VLLM 命令参数"""
    updates = {}
    if args.model:
        updates['model'] = args.model
    if args.served_name:
        updates['served_model_name'] = args.served_name
    if args.host:
        updates['host'] = args.host
    if args.port:
        updates['port'] = str(args.port)
    if args.others:
        updates['others'] = args.others
    
    for key, value in updates.items():
        content = update_config_section(content, "vllm.command", key, value)
    
    return content


def set_evalscope_config(content: str, args) -> str:
    """配置 evalscope 测评参数"""
    updates = {}
    if args.url:
        updates['url'] = args.url
    if args.model:
        updates['model'] = args.model
    if args.tokenizer_path:
        updates['tokenizer_path'] = args.tokenizer_path
    if args.dataset:
        updates['dataset'] = args.dataset
    if args.outputs_dir:
        updates['outputs_dir'] = args.outputs_dir
    if args.others:
        updates['others'] = args.others
    
    for key, value in updates.items():
        content = update_config_section(content, "evalscopeperf.command", key, value)
    
    return content


def set_vllm_benchmark_config(content: str, args) -> str:
    """配置 vllm_benchmark 测评参数"""
    updates = {}
    if args.model:
        updates['model'] = args.model
    if args.served_name:
        updates['served_model_name'] = args.served_name
    if args.host:
        updates['host'] = args.host
    if args.port:
        updates['port'] = str(args.port)
    if args.dataset_name:
        updates['dataset_name'] = args.dataset_name
    if args.num_prompts:
        updates['num_prompts'] = args.num_prompts
    if args.others:
        updates['others'] = args.others
    
    for key, value in updates.items():
        content = update_config_section(content, "vllm_benchmark.command", key, value)
    
    return content


def main():
    parser = argparse.ArgumentParser(
        description="自动配置 msServiceProfiler 寻优工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 设置场景
  %(prog)s --scenario standard --engine vllm
  
  # 添加搜索参数
  %(prog)s --add-search-param --engine vllm --param-name MAX_BATCH_SIZE \
      --min 10 --max 400 --dtype int
  
  # 添加固定参数
  %(prog)s --add-fixed-param --engine vllm --param-name MODEL_PATH \
      --value "/model" --dtype str
  
  # 配置 VLLM 命令
  %(prog)s --set-vllm-command --model /path/to/model --served-name my-model
  
  # 配置 evalscope
  %(prog)s --set-evalscope --url "http://127.0.0.1:8000/v1/chat/completions" --model my-model
        """
    )
    
    # 操作模式
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        help="寻优场景模板"
    )
    mode_group.add_argument(
        "--add-search-param",
        action="store_true",
        help="添加搜索参数（带范围）"
    )
    mode_group.add_argument(
        "--add-fixed-param",
        action="store_true",
        help="添加固定参数（不带范围）"
    )
    mode_group.add_argument(
        "--set-vllm-command",
        action="store_true",
        help="配置 VLLM 命令参数"
    )
    mode_group.add_argument(
        "--set-evalscope",
        action="store_true",
        help="配置 evalscope 测评参数"
    )
    mode_group.add_argument(
        "--set-vllm-benchmark",
        action="store_true",
        help="配置 vllm_benchmark 测评参数"
    )
    
    # 通用参数
    parser.add_argument(
        "--engine",
        choices=["mindie", "vllm", "evalscopeperf"],
        default="mindie",
        help="推理框架 (默认: mindie)"
    )
    parser.add_argument(
        "--config-path",
        default="ms_serviceparam_optimizer/ms_serviceparam_optimizer/config.toml",
        help="配置文件路径"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览修改内容，不实际写入"
    )
    
    # 场景相关参数
    parser.add_argument("--benchmark", choices=["ais_bench", "vllm_benchmark"], help="测评工具")
    parser.add_argument("--host", default="127.0.0.1", help="服务主机")
    parser.add_argument("--port", type=int, default=8000, help="服务端口")
    parser.add_argument("--ttft-slo", type=float, help="首token时延限制(秒)")
    parser.add_argument("--tpot-slo", type=float, help="非首token时延限制(秒)")
    parser.add_argument("--time-budget", help="时间预算(如: 8h, 30m)")
    
    # 模型参数
    parser.add_argument("--model", help="模型路径")
    parser.add_argument("--served-name", help="模型服务名")
    
    # 参数配置相关
    parser.add_argument("--param-name", help="参数名")
    parser.add_argument("--config-position", default="env", help="配置位置 (默认: env)")
    parser.add_argument("--dtype", choices=["int", "float", "bool", "str", "enum", "ratio", "factories", "times", "range"],
                      default="int", help="参数类型")
    parser.add_argument("--min", type=float, help="最小值")
    parser.add_argument("--max", type=float, help="最大值")
    parser.add_argument("--value", help="固定值")
    parser.add_argument("--dtype-param", help="类型参数 (ratio/factories/times)")
    parser.add_argument("--enum-values", help="枚举值列表 JSON (如: [1,2,4,8])")
    parser.add_argument("--factories-config", help="factories 配置 JSON")
    parser.add_argument("--cli-arg", help="CLI 参数名（如 --test），用于在 others 中添加引用")
    
    # VLLM 命令参数
    parser.add_argument("--others", help="其他 VLLM 参数")
    
    # evalscope 参数
    parser.add_argument("--url", help="服务 URL")
    parser.add_argument("--tokenizer-path", help="tokenizer 路径")
    parser.add_argument("--dataset", default="random", help="数据集")
    parser.add_argument("--outputs-dir", dest="outputs_dir", help="evalscope 输出目录")
    
    # vllm_benchmark 参数
    parser.add_argument("--dataset-name", help="vllm_benchmark 数据集名称")
    parser.add_argument("--num-prompts", type=int, help="vllm_benchmark prompt 数量")
    
    args = parser.parse_args()
    
    # 读取配置文件
    config_path = Path(args.config_path)
    if not config_path.exists():
        print(f"✗ 配置文件不存在: {config_path}")
        # 尝试在当前目录查找
        alt_path = Path.cwd() / args.config_path
        if alt_path.exists():
            config_path = alt_path
            print(f"找到配置文件: {config_path}")
        else:
            sys.exit(1)
    
    print(f"读取配置文件: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 根据模式执行不同操作
    if args.scenario:
        print(f"\n应用场景: {SCENARIOS[args.scenario]['description']}")
        new_content = apply_scenario_config(content, args.scenario, args)
        
        # 显示场景变更摘要
        print("\n配置变更:")
        print(f"  - n_particles: {SCENARIOS[args.scenario].get('n_particles')}")
        print(f"  - iters: {SCENARIOS[args.scenario].get('iters')}")
        print(f"  - engine: {args.engine}")
        
    elif args.add_search_param:
        if not args.param_name:
            print("✗ 需要指定 --param-name")
            sys.exit(1)
        if args.value is None:
            print("✗ 需要指定 --value （参数默认值，对于枚举类型应为枚举值之一）")
            sys.exit(1)
        print(f"\n添加搜索参数: {args.param_name}")
        new_content = add_search_param(content, args)
        print(f"  - 类型: {args.dtype}")
        print(f"  - 默认值: {args.value}")
        if args.min is not None and args.max is not None:
            print(f"  - 范围: {args.min} ~ {args.max}")
        
    elif args.add_fixed_param:
        if not args.param_name:
            print("✗ 需要指定 --param-name")
            sys.exit(1)
        print(f"\n添加固定参数: {args.param_name}")
        new_content = add_fixed_param(content, args)
        print(f"  - 类型: {args.dtype}")
        print(f"  - 值: {args.value}")
        
    elif args.set_vllm_command:
        print("\n配置 VLLM 命令参数:")
        new_content = set_vllm_command(content, args)
        if args.model:
            print(f"  - model: {args.model}")
        if args.served_name:
            print(f"  - served_model_name: {args.served_name}")
        if args.host:
            print(f"  - host: {args.host}")
        if args.port:
            print(f"  - port: {args.port}")
        
    elif args.set_evalscope:
        print("\n配置 evalscope 测评参数:")
        new_content = set_evalscope_config(content, args)
        if args.url:
            print(f"  - url: {args.url}")
        if args.model:
            print(f"  - model: {args.model}")
        if args.tokenizer_path:
            print(f"  - tokenizer_path: {args.tokenizer_path}")
        if args.dataset:
            print(f"  - dataset: {args.dataset}")
        if args.outputs_dir:
            print(f"  - outputs_dir: {args.outputs_dir}")
        if args.others:
            print(f"  - others: {args.others}")
    elif args.set_vllm_benchmark:
        print("\n配置 vllm_benchmark 测评参数:")
        new_content = set_vllm_benchmark_config(content, args)
        if args.model:
            print(f"  - model: {args.model}")
        if args.served_name:
            print(f"  - served_model_name: {args.served_name}")
        if args.host:
            print(f"  - host: {args.host}")
        if args.port:
            print(f"  - port: {args.port}")
        if args.dataset_name:
            print(f"  - dataset_name: {args.dataset_name}")
        if args.num_prompts:
            print(f"  - num_prompts: {args.num_prompts}")
        if args.others:
            print(f"  - others: {args.others}")
    else:
        print("✗ 请指定操作模式")
        sys.exit(1)
    
    if args.dry_run:
        print("\n[DRY RUN] 预览修改后的配置:")
        print("=" * 50)
        # 显示前 50 行
        lines = new_content.split('\n')[:50]
        for line in lines:
            print(line)
        if len(new_content.split('\n')) > 50:
            print("... (仅显示前 50 行)")
        print("=" * 50)
        print("实际未写入文件")
    else:
        # 备份原文件
        backup_path = config_path.with_suffix('.toml.bak')
        with open(backup_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"\n原配置已备份: {backup_path}")
        
        # 写入新配置
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"✓ 配置已更新: {config_path}")
        
        print("\n下一步:")
        print(f"  msserviceprofiler optimizer -e {args.engine}")


if __name__ == "__main__":
    main()
