#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
从 msmodelslim 的 debug_info.safetensors 提取 Fusion 路径的 smooth scales。

背景：
  Fusion 路径（NormLinear/OV/UpDown/LinearLinear 子图）把 s 吸收进相邻层权重，
  不像 NonFusion 那样保存 div.mul_scale。但 msmodelslim 在量化时若开启 --debug，
  会把 s 矩阵保存到 debug_info.safetensors 的 smooth_scales.* 命名空间下。

  subgraph_fusion.py:57 处：
    namespace.debug[f"{layer_name}.scales"] = scales.cpu()

  保存后的 key 形如：
    smooth_scales.model.layers.0.input_layernorm.scales
    smooth_scales.model.layers.0.self_attn.v_proj.scales
    smooth_scales.model.layers.0.mlp.gate_proj.scales
    smooth_scales.model.layers.0.mlp.up_proj.scales
    ...

  对应数学关系（Fusion）：
    - 上游层（W /= s）：input_layernorm / v_proj / up_proj / gate_proj / linear1
      下游 Linear 的输入 = 上游输出 / s
    - 下游层（W *= s）：q_proj / k_proj / o_proj / down_proj / linear2
      不需要逆变换（输入 = 上游输出，与浮点侧一致）

msprobe 后处理只支持 matmul 操作（不支持逐元素除法），
因此把 s 转换为 diag(s) 对角矩阵，用 matmul 实现逐元素乘法：
  x * s = x @ diag(s)

  - 保存值：debug_info 中的 smooth_scales.<layer>.scales = s
  - 脚本保存的 npy 值：diag(s)（对角矩阵）
  - 逆变换（matmul 形式）：x = x' @ diag(s)（对上游层输出做后处理 = 对下游 Linear 输入做后处理）

用法：
  python3 extract_fusion_scales.py \
      --debug-info /path/to/quant_output/debug_info/debug_info.safetensors \
      --output-dir /workdir/fusion_scales

注意：
  前提：量化时必须加 --debug 参数，否则 debug_info.safetensors 不存在或为空。
"""

import argparse
import json
import os
import re
import sys

import numpy as np
import torch
from safetensors import safe_open

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_log import add_audit_arg, get_logger


# Fusion 子图配置：(上游层后缀列表, 下游层后缀列表)
# 上游层：W /= s，下游 Linear 输入 = 上游输出 / s，需要逆变换 x' * s
# 下游层：W *= s，输入 = 上游输出，无需逆变换
FUSION_SUBGRAPHS = {
    "norm_linear": {
        "upstream_suffixes": ["input_layernorm", "post_attention_layernorm"],
        "downstream_suffixes": ["q_proj", "k_proj", "v_proj", "gate_proj", "up_proj"],
    },
    "ov": {
        "upstream_suffixes": ["v_proj"],
        "downstream_suffixes": ["o_proj"],
    },
    "up_down": {
        "upstream_suffixes": ["gate_proj", "up_proj"],
        "downstream_suffixes": ["down_proj"],
    },
    "linear_linear": {
        "upstream_suffixes": ["linear1", "fc1"],
        "downstream_suffixes": ["linear2", "fc2"],
    },
}


def parse_args():
    p = argparse.ArgumentParser(description="从 debug_info.safetensors 提取 Fusion smooth scales")
    p.add_argument(
        "-d",
        "--debug-info",
        required=True,
        help="debug_info.safetensors 文件路径（量化时 --debug 产生）",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="输出目录（每个 scale 生成一个 .npy 文件）",
    )
    add_audit_arg(p)
    return p.parse_args()


def match_upstream(key: str) -> str:
    """
    判断 key 是否对应 Fusion 上游层（W /= s 的层）。
    返回匹配到的子图类型，若不匹配返回 None。

    key 形如：smooth_scales.model.layers.0.input_layernorm.scales
    """
    # 去掉 smooth_scales. 前缀和 .scales 后缀
    layer_name = key
    if layer_name.startswith("smooth_scales."):
        layer_name = layer_name[len("smooth_scales."):]
    if layer_name.endswith(".scales"):
        layer_name = layer_name[:-len(".scales")]

    # 检查每个 Fusion 子图的上游后缀
    for subgraph_type, config in FUSION_SUBGRAPHS.items():
        for suffix in config["upstream_suffixes"]:
            # 匹配 .input_layernorm 或 .post_attention_layernorm 等
            if layer_name.endswith(f".{suffix}") or layer_name == suffix:
                return subgraph_type
    return None


def main():
    args = parse_args()
    logger = get_logger(args)

    if not os.path.exists(args.debug_info):
        print(f"[ERROR] 文件不存在: {args.debug_info}")
        print(f"        请确认量化时是否加了 --debug 参数")
        raise SystemExit(1)

    # 1. 扫描 debug_info.safetensors 中的 smooth_scales
    print("[1/3] 扫描 smooth_scales ...")
    fusion_scales = []  # [{key, layer_name, subgraph_type, tensor, npy_path}]

    with safe_open(args.debug_info, framework="pt", device="cpu") as f:
        all_keys = list(f.keys())
        print(f"       debug_info.safetensors 共 {len(all_keys)} 个 key")

        smooth_keys = [k for k in all_keys if k.startswith("smooth_scales.")]
        print(f"       其中 smooth_scales.* 共 {len(smooth_keys)} 个")

        if not smooth_keys:
            print("\n[WARN] 未找到 smooth_scales.* 张量")
            print("        可能原因：")
            print("        1. 量化时未加 --debug 参数（debug 字典为空）")
            print("        2. 量化配置未启用 SmoothQuant/IterSmooth（无 Fusion 子图）")
            print("        3. 所有 Linear 走 NonFusion 路径（用 div.mul_scale，不需要此脚本）")
            return

        for key in smooth_keys:
            tensor = f.get_tensor(key)
            layer_name = key[len("smooth_scales."):-len(".scales")]
            subgraph_type = match_upstream(key)

            fusion_scales.append({
                "key": key,
                "layer_name": layer_name,
                "subgraph_type": subgraph_type,
                "tensor": tensor,
            })
            tag = f"[{subgraph_type}]" if subgraph_type else "[下游/不匹配]"
            print(f"       {tag} {layer_name}  shape={tuple(tensor.shape)} dtype={tensor.dtype}")

    # 2. 筛选上游层（需要逆变换的层）
    print("\n[2/3] 筛选 Fusion 上游层（W /= s，下游 Linear 输入需逆变换）...")
    upstream_scales = [s for s in fusion_scales if s["subgraph_type"] is not None]
    downstream_scales = [s for s in fusion_scales if s["subgraph_type"] is None]

    print(f"       上游层（需逆变换）: {len(upstream_scales)} 个")
    print(f"       下游层（无需逆变换）: {len(downstream_scales)} 个")

    if not upstream_scales:
        print("\n[WARN] 未找到 Fusion 上游层")
        print("        可能所有 Linear 走 NonFusion 路径，请改用 convert_suppression_to_npy.py")
        return

    # 3. 保存为 diag(s) 对角矩阵 npy
    print("\n[3/3] 保存为 diag(s) 对角矩阵 npy ...")
    os.makedirs(args.output_dir, exist_ok=True)

    index = []
    for entry in upstream_scales:
        layer_name = entry["layer_name"]
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", layer_name)
        npy_path = os.path.join(args.output_dir, f"{safe_name}.npy")

        # msprobe 后处理只支持 matmul，把 s 转为 diag(s) 对角矩阵
        # 数学：x = x' * s = x' @ diag(s)
        s_tensor = entry["tensor"].float()
        diag_s = torch.diag(s_tensor).numpy()
        np.save(npy_path, diag_s)

        index.append({
            "layer_name": layer_name,
            "subgraph_type": entry["subgraph_type"],
            "scale_npy_path": os.path.abspath(npy_path),
            "original_shape": list(s_tensor.shape),
            "diag_shape": list(diag_s.shape),
            "dtype": str(s_tensor.dtype),
            "math": "保存值 = diag(s)；逆变换: x = x' @ diag(s)",
        })
        print(f"       [{entry['subgraph_type']}] {layer_name} -> {npy_path}  diag_shape={diag_s.shape}")

    # 保存索引
    index_path = os.path.join(args.output_dir, "fusion_index.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({
            "source": "debug_info.safetensors/smooth_scales.*",
            "requirement": "量化时必须加 --debug 参数",
            "math": "保存值 = diag(s)；逆变换: x = x' @ diag(s)（matmul 形式）",
            "reason": "msprobe 后处理只支持 matmul，逐元素乘法 x*s 用 x@diag(s) 等价替换",
            "scales": index,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n       索引文件: {index_path}")
    print(f"\n[DONE] Fusion scales 已提取并转换为 diag(s) 对角矩阵 npy 格式")
    print(f"       后续用于 gen_postprocess_config.py 生成 Fusion 逆变换规则")
    print(f"       逆变换公式（matmul 形式）: x = x' @ diag(s)（对上游层输出做后处理）")

    logger.log("extract_fusion_scales", {
        "input": args.debug_info,
        "output_dir": args.output_dir,
        "index_path": index_path,
        "upstream_count": len(upstream_scales),
        "downstream_count": len(downstream_scales),
    })


if __name__ == "__main__":
    main()
