#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
生成 msprobe tensor 后处理配置（YAML 格式，统一用 matmul 操作）。

msprobe 的 tensor 后处理子系统（msprobe.core.compare.tensor_postprocess）原生
只支持 matmul 操作（左乘 left / 右乘 right），不支持 mul/div/add 等逐元素运算。

本脚本把所有逆变换统一为 matmul 形式：
  1. QuaRot 逆旋转：原生就是 matmul
     - 不论 right/left 旋转，逆变换都是 x = x' @ R^T（side=right）
       因为 R 是正交矩阵，R^T = R^{-1}，右乘 R^T 等价于"右旋空间 → 原始空间"
  2. NonFusion 逆抑制：把 s 转为 diag(s)，用 matmul 实现逐元素乘法
     - 逐元素形式：x = x' * s
     - matmul 形式：x = x' @ diag(s)   → side=right, mat=diag(s)
  3. Fusion 逆抑制：同 NonFusion，把 s 转为 diag(s)
     - matmul 形式：x = x' @ diag(s)   → side=right, mat=diag(s)

【方案 C：从 dump.json 按模块顺序推导空间归属】
提供 --dump-json 时启用方案 C。脚本从 dump.json 的 data 字段提取模块执行顺序
（Python dict 3.7+ 保证插入顺序），按以下规则推导每个模块的空间归属：

  初始空间 = "original"
  遇到 right_output（如 embed_tokens，pre_run right）：output 在右旋空间
    → 生成 output 逆变换，之后进入右旋空间
  遇到 right_input（如 q_proj，主循环 right 旋转权重）：input 在右旋空间
    → 生成 input 逆变换，之后回到原始空间（R 抵消）
  遇到 left_output（如 o_proj，主循环 left 旋转权重）：output 在右旋空间
    → 生成 output 逆变换，之后进入右旋空间
  遇到非旋转模块（如 RMSNorm、activation、add）：保持空间不变
    - 若当前在右旋空间 → input 和 output 都在右旋空间，均生成逆变换
    - 若当前在原始空间 → 无需逆变换

方案 C 解决了方案 B 无法处理"中间非旋转模块在右旋空间"的问题：
例如 embed_tokens 输出右旋空间后，紧接的 RMSNorm 也在右旋空间，
方案 B 会漏掉 RMSNorm 的逆变换，导致比对结果错误。

未提供 --dump-json 时回退到方案 B：仅对 rotate_map 中列出的模块生成规则。

用法：
  python3 gen_postprocess_config.py \
      --rotation-npy /workdir/rotation.npy \
      --rotate-map /workdir/rotate_map.json \
      [--dump-json /workdir/dump_quant/step0/rank0/dump.json] \
      --suppression-index /workdir/suppression_scales/suppression_index.json \
      --fusion-index /workdir/fusion_scales/fusion_index.json \
      --output /workdir/postprocess_config

可按需只指定其中一项或多项。
"""

import argparse
import json
import os
import re
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_log import add_audit_arg, get_logger


def parse_args():
    p = argparse.ArgumentParser(description="生成 msprobe tensor 后处理配置（YAML，统一 matmul）")
    p.add_argument(
        "-r",
        "--rotation-npy",
        help="rotation.npy 文件路径（由 convert_rotation_to_npy.py 生成）。QuaRot 场景必填",
    )
    p.add_argument(
        "-M",
        "--rotate-map",
        help="旋转作用范围 JSON 文件路径。格式："
             '{"right_input": [...], "right_output": [...], "left_output": [...]}。'
             "agent 根据 msmodelslim.get_rotate_map 源码生成",
    )
    p.add_argument(
        "-d",
        "--dump-json",
        help="量化侧 dump.json 文件路径（通常位于 dump_quant/step0/rank0/dump.json）。"
             "若提供，启用方案 C：从 dump.json 的 data 字段提取模块执行顺序，"
             "按 rotate_map 推导每个模块的空间归属，对在右旋空间的模块"
             "（包括 RMSNorm 等中间非旋转模块）生成 input+output 逆变换规则。"
             "若不提供，回退到方案 B：仅对 rotate_map 中列出的模块生成规则",
    )
    p.add_argument(
        "-s",
        "--suppression-index",
        help="suppression_index.json 文件路径（由 convert_suppression_to_npy.py 生成）。"
             "NonFusion SmoothQuant 场景必填",
    )
    p.add_argument(
        "-f",
        "--fusion-index",
        help="fusion_index.json 文件路径（由 extract_fusion_scales.py 生成）。"
             "Fusion SmoothQuant 场景必填（需量化时加 --debug）",
    )
    p.add_argument(
        "-o",
        "--output",
        required=True,
        help="输出路径（不含扩展名，会生成 .json 和 .yaml 两个文件）",
    )
    add_audit_arg(p)
    return p.parse_args()


def load_rotate_map(rotate_map_path: str) -> dict:
    """
    加载旋转作用范围配置。

    【新格式】（按空间归属分类，数学上完备）：
    {
        "right_input": ["q_proj", "k_proj", "v_proj", "gate_proj", "up_proj", "gate", "lm_head"],
        "right_output": ["embed_tokens"],
        "left_output": ["o_proj", "down_proj", "merge_linear_2"]
    }

    含义：
    - right_input: right 旋转权重模块，其 input 在右旋空间 → 对 input 做 x = x' @ R^T
    - right_output: pre_run 的 right 旋转模块，其 output 在右旋空间 → 对 output 做 x = x' @ R^T
    - left_output: left 旋转权重模块（包括 pre_run left），其 output 在右旋空间 → 对 output 做 x = x' @ R^T

    【兼容旧格式】（right/left/right_special/left_special）会自动转换为新格式。
    """
    if not os.path.exists(rotate_map_path):
        print(f"[ERROR] 文件不存在: {rotate_map_path}")
        raise SystemExit(1)

    with open(rotate_map_path, "r", encoding="utf-8") as f:
        rotate_map = json.load(f)

    # 兼容旧格式转换
    if "right_input" not in rotate_map and "right" in rotate_map:
        print("[INFO] 检测到旧格式 rotate_map，自动转换为新格式 ...")
        old = rotate_map
        rotate_map = {
            "right_input": old.get("right", []),
            "right_output": old.get("right_special", []),
            "left_output": old.get("left", []) + old.get("left_special", []),
        }
        print(f"       right_input: {rotate_map['right_input']}")
        print(f"       right_output: {rotate_map['right_output']}")
        print(f"       left_output: {rotate_map['left_output']}")

    # 校验格式
    required_keys = {"right_input", "right_output", "left_output"}
    missing = required_keys - set(rotate_map.keys())
    if missing:
        print(f"[ERROR] rotate_map 缺少必要字段: {missing}")
        raise SystemExit(1)

    print(f"       right_input 模块（对 input 逆变换）: {rotate_map['right_input']}")
    print(f"       right_output 模块（对 output 逆变换）: {rotate_map['right_output']}")
    print(f"       left_output 模块（对 output 逆变换）: {rotate_map['left_output']}")

    return rotate_map


def extract_dump_modules_in_order(dump_json_path: str) -> List[str]:
    """
    从 dump.json 中提取模块名列表（按执行顺序，去重）。

    msprobe dump.json 结构：
      {
        "data": {
          "Module.model.layers.0.self_attn.q_proj.forward": {...},
          "Module.model.layers.0.self_attn.o_proj.forward": {...},
          ...
        },
        ...
      }

    data 字段是 Python dict（3.7+ 保证插入顺序 = 执行顺序），key 为完整模块名
    （形如 `Module.xxx.forward` 或 `Module.xxx.forward.0`）。
    本函数去掉 `.forward` / `.backward` / `.forward.N` 后缀，返回去重后的模块名列表。
    """
    if not dump_json_path or not os.path.exists(dump_json_path):
        return []

    with open(dump_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    data_dict = data.get("data", {})
    if not isinstance(data_dict, dict):
        print(f"[WARN] dump.json 的 data 字段不是 dict: {type(data_dict)}")
        return []

    modules = []
    seen = set()
    for full_key in data_dict.keys():
        module_name = strip_io_suffix(full_key)
        if module_name and module_name not in seen:
            seen.add(module_name)
            modules.append(module_name)

    return modules


def strip_io_suffix(name: str) -> str:
    """
    去掉模块名末尾的 `.forward` / `.backward` / `.forward.N` / `.backward.N` 后缀。

    示例：
      "Module.model.layers.0.q_proj.forward"     → "Module.model.layers.0.q_proj"
      "Module.model.layers.0.q_proj.forward.0"   → "Module.model.layers.0.q_proj"
      "Module.model.layers.0.q_proj.backward.1"  → "Module.model.layers.0.q_proj"
      "Module.model.layers.0.q_proj"             → "Module.model.layers.0.q_proj"
    """
    parts = name.split(".")
    if len(parts) >= 2 and parts[-2] in ("forward", "backward"):
        # 末尾是 .forward.N 或 .backward.N
        parts = parts[:-2]
    elif parts and parts[-1] in ("forward", "backward"):
        # 末尾是 .forward 或 .backward
        parts.pop()
    return ".".join(parts)


def match_suffix(module_name: str, suffix: str) -> bool:
    """
    检查 module_name 是否以 `.suffix` 结尾，或等于 suffix。

    例如 module_name="Module.model.layers.0.q_proj", suffix="q_proj" → True
    """
    return module_name == suffix or module_name.endswith("." + suffix)


def find_matched_suffix(module_name: str, rotate_map: dict) -> Optional[str]:
    """
    在 rotate_map 的三个分类中查找 module_name 匹配的后缀（最长匹配优先）。

    返回 (suffix, category) 或 None。category 取值：
    "right_input" / "right_output" / "left_output"
    """
    candidates = []
    for category in ("right_input", "right_output", "left_output"):
        for suffix in rotate_map.get(category, []):
            if match_suffix(module_name, suffix):
                candidates.append((suffix, category))
    if not candidates:
        return None
    # 最长后缀优先（避免 "gate" 误匹配 "gate_proj"）
    candidates.sort(key=lambda x: len(x[0]), reverse=True)
    return candidates[0]


def derive_space_rules(dump_modules_in_order: List[str],
                       rotate_map: dict,
                       rotation_npy_path: str) -> List[dict]:
    """
    【方案 C 核心】从 dump.json 模块执行顺序推导每个模块的空间归属，
    对在右旋空间的模块生成 input+output 逆变换规则。

    旋转空间传递规则：
      初始空间 = "original"
      遇到 right_output（如 embed_tokens，pre_run right）：output 在右旋空间
        → 生成 output 逆变换，之后进入右旋空间
      遇到 right_input（如 q_proj，主循环 right 旋转权重）：input 在右旋空间
        → 生成 input 逆变换，之后回到原始空间（R 抵消）
      遇到 left_output（如 o_proj，主循环 left 旋转权重）：output 在右旋空间
        → 生成 output 逆变换，之后进入右旋空间
      遇到非旋转模块（如 RMSNorm、activation、add）：保持空间不变
        - 若当前在右旋空间 → input 和 output 都在右旋空间，均生成逆变换
        - 若当前在原始空间 → 无需逆变换

    所有逆变换统一用 side=right, mat=R^T（R 正交，R^T = R^{-1}）。
    """
    abs_path = os.path.abspath(rotation_npy_path)
    rules = []
    space = "original"  # 初始空间
    stats = {"right_output": 0, "right_input": 0, "left_output": 0, "middle": 0}

    for module_name in dump_modules_in_order:
        matched = find_matched_suffix(module_name, rotate_map)
        if matched is None:
            # 非旋转模块：保持空间不变
            if space == "rotated":
                # 中间模块在右旋空间，input 和 output 都需要逆变换
                stats["middle"] += 1
                rules.append({
                    "module_name": module_name,
                    "module_category": "middle_in_rotated_space",
                    "transform": "rotation",
                    "side": "right",
                    "apply_to": "input",
                    "math": "x = x' @ R^T",
                    "tensor_path": abs_path,
                })
                rules.append({
                    "module_name": module_name,
                    "module_category": "middle_in_rotated_space",
                    "transform": "rotation",
                    "side": "right",
                    "apply_to": "output",
                    "math": "x = x' @ R^T",
                    "tensor_path": abs_path,
                })
            continue

        suffix, category = matched
        if category == "right_output":
            # pre_run right 模块（如 embed_tokens）：output 在右旋空间
            rules.append({
                "module_name": module_name,
                "module_category": "right_output",
                "matched_suffix": suffix,
                "transform": "rotation",
                "side": "right",
                "apply_to": "output",
                "math": "x = x' @ R^T",
                "tensor_path": abs_path,
            })
            space = "rotated"
            stats["right_output"] += 1
        elif category == "right_input":
            # right 旋转权重模块：input 在右旋空间，output 回到原始空间
            rules.append({
                "module_name": module_name,
                "module_category": "right_input",
                "matched_suffix": suffix,
                "transform": "rotation",
                "side": "right",
                "apply_to": "input",
                "math": "x = x' @ R^T",
                "tensor_path": abs_path,
            })
            space = "original"
            stats["right_input"] += 1
        elif category == "left_output":
            # left 旋转权重模块：input 在原始空间，output 在右旋空间
            rules.append({
                "module_name": module_name,
                "module_category": "left_output",
                "matched_suffix": suffix,
                "transform": "rotation",
                "side": "right",
                "apply_to": "output",
                "math": "x = x' @ R^T",
                "tensor_path": abs_path,
            })
            space = "rotated"
            stats["left_output"] += 1

    print(f"       [方案 C] 空间推导统计：")
    print(f"         right_output 模块（input 逆变换）: {stats['right_output']}")
    print(f"         right_input 模块（input 逆变换）: {stats['right_input']}")
    print(f"         left_output 模块（output 逆变换）: {stats['left_output']}")
    print(f"         中间非旋转模块在右旋空间（input+output 逆变换）: {stats['middle']}")

    return rules


def get_rotate_rules_fallback(rotate_map: dict, rotation_npy_path: str) -> List[dict]:
    """
    【方案 B 回退】未提供 dump.json 时，根据 rotate_map 生成规则。

    这种情况下无法推导中间非旋转模块的空间归属，只能对 rotate_map 中列出的模块生成规则。
    注意：此回退方案会漏掉在右旋空间的中间模块（如 RMSNorm）。
    """
    abs_path = os.path.abspath(rotation_npy_path)
    rules = []

    for suffix in rotate_map["right_input"]:
        rules.append({
            "module_suffix": suffix,
            "module_pattern": f".*\\.{re.escape(suffix)}$",
            "transform": "rotation",
            "side": "right",
            "apply_to": "input",
            "math": "x = x' @ R^T",
            "tensor_path": abs_path,
        })

    for suffix in rotate_map["right_output"] + rotate_map["left_output"]:
        rules.append({
            "module_suffix": suffix,
            "module_pattern": f".*\\.{re.escape(suffix)}$",
            "transform": "rotation",
            "side": "right",
            "apply_to": "output",
            "math": "x = x' @ R^T",
            "tensor_path": abs_path,
        })

    print(f"       [WARN] 未提供 --dump-json，使用方案 B 回退（无法处理中间非旋转模块）")
    print(f"         共 {len(rules)} 条规则（仅 rotate_map 中列出的模块）")
    return rules


def load_suppression_rules(suppression_index_path: str) -> List[dict]:
    """
    从 suppression_index.json 加载 NonFusion 抑制逆变换规则。
    统一用 matmul 形式：x = x' @ diag(s)
    注意：作用对象是 Wrapper 内部的 Linear（模块名后缀 .linear）。
    """
    if not os.path.exists(suppression_index_path):
        print(f"[ERROR] 文件不存在: {suppression_index_path}")
        raise SystemExit(1)

    with open(suppression_index_path, "r", encoding="utf-8") as f:
        index_data = json.load(f)

    scales = index_data.get("scales", [])
    rules = []
    for entry in scales:
        linear_prefix = entry["linear_prefix"]
        escaped_pattern = linear_prefix.replace(".", "\\.")
        rules.append({
            "module_pattern": f".*{escaped_pattern}$",
            "transform": "suppression",
            "suppression_type": "non_fusion",
            "apply_to": "input",
            "side": "right",
            "math": "x = x' @ diag(s)（等价于逐元素 x' * s）",
            "tensor_path": entry["scale_npy_path"],
        })

    return rules


def load_fusion_rules(fusion_index_path: str) -> List[dict]:
    """
    从 fusion_index.json 加载 Fusion 抑制逆变换规则。
    统一用 matmul 形式：x = x' @ diag(s)
    注意：作用对象是上游层（对输出做后处理 = 对下游 Linear 输入做后处理）。
    """
    if not os.path.exists(fusion_index_path):
        print(f"[ERROR] 文件不存在: {fusion_index_path}")
        raise SystemExit(1)

    with open(fusion_index_path, "r", encoding="utf-8") as f:
        index_data = json.load(f)

    scales = index_data.get("scales", [])
    rules = []
    for entry in scales:
        layer_name = entry["layer_name"]
        escaped_pattern = layer_name.replace(".", "\\.")
        rules.append({
            "module_pattern": f".*{escaped_pattern}$",
            "transform": "suppression",
            "suppression_type": "fusion",
            "apply_to": "output",
            "side": "right",
            "math": "x = x' @ diag(s)（等价于逐元素 x' * s）",
            "tensor_path": entry["scale_npy_path"],
            "subgraph_type": entry.get("subgraph_type", "unknown"),
        })

    return rules


def generate_msprobe_config(rules: List[dict], output_path: str):
    """
    生成 msprobe tensor_postprocess 的配置文件。

    生成两个文件：
    - <output>.json：规则说明（含数学公式和 module_pattern，便于理解）
    - <output>.yaml：msprobe 原生 YAML 模板（需用 inspect_dump.py 提取实际 data_name 后替换）

    msprobe 的 YAML schema：
      mode: matmul
      target_tensor_map:
        "/path/to/mat.npy":
          data_names:
            - "Module.model.layers.0.xxx.forward.0"
          side: right  # 或 left
    """
    # 生成规则说明 JSON（便于用户理解）
    config = {
        "mode": "matmul",
        "description": "msprobe tensor 后处理配置（统一用 matmul 实现）",
        "math_basis": {
            "rotation": "x = x' @ R^T（逆旋转，R 正交，side=right）",
            "suppression": "x = x' @ diag(s)（用对角矩阵乘法等价替换逐元素乘法）",
        },
        "rules": rules,
    }

    json_path = output_path if output_path.endswith(".json") else output_path + ".json"
    os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    # 同时生成 msprobe 原生 YAML 格式（需要用户补充 data_name）
    yaml_path = json_path.replace(".json", ".yaml")

    # 按 tensor_path 分组
    # 规则可能含 module_name（方案 C，精确匹配）或 module_pattern（方案 B 回退，正则）
    tensor_map = {}
    for rule in rules:
        tp = rule["tensor_path"]
        if tp not in tensor_map:
            tensor_map[tp] = []
        if "module_name" in rule:
            # 方案 C：精确模块名，直接生成 data_name 模板
            mn = rule["module_name"]
            data_name_template = f"{mn}.forward.<N>（需替换为实际 data_name）"
            identifier = mn
        else:
            # 方案 B 回退：正则 pattern
            pattern = rule["module_pattern"]
            data_name_template = f"Module.<{pattern}>.forward.<N>（需替换为实际 data_name）"
            identifier = pattern
        tensor_map[tp].append({
            "identifier": identifier,
            "side": rule["side"],
            "transform": rule["transform"],
            "apply_to": rule["apply_to"],
            "math": rule["math"],
            "data_name_template": data_name_template,
        })

    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("# msprobe tensor 后处理配置（统一 matmul 形式）\n")
        f.write("# 生成方式：python3 gen_postprocess_config.py ...\n")
        f.write("#\n")
        f.write("# 注意：msprobe 用完整 data_name 字符串匹配（不支持正则），\n")
        f.write("#       以下 data_names 为模板，需用 inspect_dump.py 提取实际 data_name 后替换。\n\n")
        f.write("mode: matmul\n")
        f.write("target_tensor_map:\n")
        for tp, entries in tensor_map.items():
            f.write(f'  "{tp}":\n')
            f.write(f'    data_names:\n')
            for entry in entries:
                f.write(f'      # {entry["transform"]} / {entry["math"]} / apply_to={entry["apply_to"]}\n')
                f.write(f'      - "{entry["data_name_template"]}"\n')
            # 同一 tensor_path 的 side 可能不同（rotation right 和 suppression 都用 right，rotation left 用 left）
            # 这里按 side 分组生成
            side = entries[0]["side"] if entries else "right"
            f.write(f'    side: {side}\n\n')

    print(f"\n       规则说明（JSON）: {json_path}")
    print(f"       msprobe YAML 模板: {yaml_path}")
    print(f"       注意：YAML 中的 data_name 是模板，需用 inspect_dump.py 提取实际值后替换")


def main():
    args = parse_args()
    logger = get_logger(args)

    if not args.rotation_npy and not args.suppression_index and not args.fusion_index:
        print("[ERROR] 必须至少指定 --rotation-npy / --suppression-index / --fusion-index 之一")
        raise SystemExit(1)

    rotation_rules = []
    suppression_rules = []
    fusion_rules = []

    # 1. 旋转逆变换规则
    if args.rotation_npy:
        print("[1/3] 生成逆旋转规则 ...")
        if not os.path.exists(args.rotation_npy):
            print(f"[ERROR] 文件不存在: {args.rotation_npy}")
            raise SystemExit(1)
        if not args.rotate_map:
            print("[ERROR] 使用 --rotation-npy 时必须同时指定 --rotate-map")
            raise SystemExit(1)
        print(f"       rotation_npy: {args.rotation_npy}")
        rotate_map = load_rotate_map(args.rotate_map)

        if args.dump_json:
            # 方案 C：从 dump.json 按模块执行顺序推导空间归属
            print(f"       [方案 C] 从 dump.json 推导空间归属: {args.dump_json}")
            dump_modules = extract_dump_modules_in_order(args.dump_json)
            print(f"       dump 中共 {len(dump_modules)} 个模块（按执行顺序）")
            rotation_rules = derive_space_rules(dump_modules, rotate_map, args.rotation_npy)
            print(f"       共 {len(rotation_rules)} 条规则（统一 side=right, mat=R^T）")
        else:
            # 方案 B 回退：仅对 rotate_map 中列出的模块生成规则
            rotation_rules = get_rotate_rules_fallback(rotate_map, args.rotation_npy)
    else:
        print("[1/3] 跳过逆旋转规则（未指定 --rotation-npy）")

    # 2. NonFusion 抑制逆变换规则
    if args.suppression_index:
        print(f"\n[2/3] 加载 NonFusion 逆抑制规则 ...")
        suppression_rules = load_suppression_rules(args.suppression_index)
        print(f"       共 {len(suppression_rules)} 条规则")
        print(f"       对内部 Linear 的输入做 x' @ diag(s)")
    else:
        print(f"\n[2/3] 跳过 NonFusion 逆抑制规则（未指定 --suppression-index）")

    # 3. Fusion 抑制逆变换规则
    if args.fusion_index:
        print(f"\n[3/3] 加载 Fusion 逆抑制规则 ...")
        fusion_rules = load_fusion_rules(args.fusion_index)
        print(f"       共 {len(fusion_rules)} 条规则")
        print(f"       对上游层输出做 x' @ diag(s)")
        print(f"       前提：量化时必须加 --debug，否则 debug_info.safetensors 为空")
    else:
        print(f"\n[3/3] 跳过 Fusion 逆抑制规则（未指定 --fusion-index）")

    all_rules = rotation_rules + suppression_rules + fusion_rules
    print(f"\n       总计 {len(all_rules)} 条后处理规则（统一用 matmul, side=right）")
    print(f"       - 逆旋转: {len(rotation_rules)} 条")
    print(f"       - 逆抑制[NonFusion]: {len(suppression_rules)} 条")
    print(f"       - 逆抑制[Fusion]: {len(fusion_rules)} 条")

    # 4. 生成配置
    generate_msprobe_config(all_rules, args.output)

    print(f"\n[DONE] msprobe tensor 后处理配置已生成")
    print(f"       统一用 matmul 操作（msprobe 原生支持，无需扩展）")
    print(f"       逆旋转和逆抑制都用 side=right, mat=R^T 或 diag(s)")
    print(f"\n       后续步骤：")
    print(f"       1. 用 inspect_dump.py 从量化侧 dump 中提取实际 data_name")
    print(f"       2. 替换 YAML 模板中的 data_name 占位符")
    print(f"       3. 把 YAML 文件放到 msprobe 的 tensor_postprocess/ 目录下")
    print(f"       4. 运行 msprobe compare 时自动加载后处理配置（在比对时实时做逆变换）")

    logger.log("gen_postprocess_config", {
        "output": args.output,
        "rotation_rules": len(rotation_rules),
        "suppression_rules": len(suppression_rules),
        "fusion_rules": len(fusion_rules),
        "total_rules": len(all_rules),
        "use_plan_c": bool(args.dump_json),
    })


if __name__ == "__main__":
    main()
