#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
扫描量化产物中的 SmoothQuant 抑制因子 div.mul_scale，转换为 diag(s) 对角矩阵 npy 格式。

msmodelslim 的 NonFusion SmoothQuant 路径会把每个被抑制 Linear 的 1/s 保存到
quant_model_weights.safetensors 中，key 格式为 `<prefix>.div.mul_scale`。

msprobe tensor 后处理只支持 matmul 操作（不支持逐元素除法），
因此把 s 转换为 diag(s) 对角矩阵，用 matmul 实现逐元素乘法：
  x * s = x @ diag(s)

数学关系：
  - 保存值：div.mul_scale = 1/s
  - 推理时激活：x' = x * div.mul_scale = x/s
  - 逆变换（逐元素）：x = x' * s = x' / div.mul_scale
  - 逆变换（matmul）：x = x' @ diag(s)   ← msprobe 后处理用此形式

用法：
  python3 convert_suppression_to_npy.py \
      --quant-weights /path/to/quant_model_weights.safetensors \
      --output-dir /workdir/suppression_scales
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


DIV_MUL_SCALE_SUFFIX = ".div.mul_scale"
# 内部 Linear 的实际路径后缀（Wrapper 包装后，Linear 名变为 <prefix>.linear）
LINEAR_SUFFIX = ".linear"


def parse_args():
    p = argparse.ArgumentParser(description="抑制因子格式转换：safetensors → npy")
    p.add_argument(
        "-q",
        "--quant-weights",
        required=True,
        help="quant_model_weights.safetensors 文件路径",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="输出目录（每个 prefix 生成一个 .npy 文件）",
    )
    add_audit_arg(p)
    return p.parse_args()


def main():
    args = parse_args()
    logger = get_logger(args)

    # 1. 扫描 div.mul_scale
    print("[1/2] 扫描 div.mul_scale ...")
    if not os.path.exists(args.quant_weights):
        print(f"[ERROR] 文件不存在: {args.quant_weights}")
        raise SystemExit(1)

    scales = {}
    with safe_open(args.quant_weights, framework="pt", device="cpu") as f:
        for key in f.keys():
            if key.endswith(DIV_MUL_SCALE_SUFFIX):
                # key 形如: model.layers.0.mlp.gate_proj.div.mul_scale
                # Wrapper 包装后内部 Linear 路径: model.layers.0.mlp.gate_proj.linear
                prefix = key[: -len(DIV_MUL_SCALE_SUFFIX)]
                tensor = f.get_tensor(key)
                scales[prefix] = {
                    "scale_tensor": tensor,
                    "linear_prefix": prefix + LINEAR_SUFFIX,
                }
                print(f"       {key}  shape={tuple(tensor.shape)} dtype={tensor.dtype}")

    if not scales:
        print("[WARN] 未找到任何 div.mul_scale，可能不是 NonFusion SmoothQuant 路径")
        print("       （Fusion 路径下 scales 已吸收进权重，无需逆变换）")
        logger.log("convert_suppression", {
            "input": args.quant_weights,
            "scales_count": 0,
            "warn": "no div.mul_scale found",
        }, event="warn")
        return

    print(f"\n       共扫描到 {len(scales)} 个抑制因子")

    # 2. 保存为 npy
    print("\n[2/2] 保存为 npy ...")
    os.makedirs(args.output_dir, exist_ok=True)

    # 保存索引文件（记录 prefix -> npy 文件路径映射）
    index = []
    for prefix, info in scales.items():
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", prefix)
        npy_path = os.path.join(args.output_dir, f"{safe_name}.npy")

        # msprobe 后处理只支持 matmul，把 s 转为 diag(s) 对角矩阵
        # 数学：x = x' * s = x' @ diag(s)
        # div.mul_scale = 1/s，所以 s = 1 / div.mul_scale
        div_mul_scale = info["scale_tensor"].float()
        s = 1.0 / div_mul_scale
        diag_s = torch.diag(s).numpy()
        np.save(npy_path, diag_s)

        index.append({
            "prefix": prefix,
            "linear_prefix": info["linear_prefix"],
            "scale_npy_path": os.path.abspath(npy_path),
            "original_shape": list(info["scale_tensor"].shape),
            "diag_shape": list(diag_s.shape),
            "dtype": str(info["scale_tensor"].dtype),
            "math": "保存值 = diag(s) = diag(1/div.mul_scale)；逆变换: x = x' @ diag(s)",
        })
        print(f"       {prefix} -> {npy_path}  diag_shape={diag_s.shape}")

    # 保存索引
    index_path = os.path.join(args.output_dir, "suppression_index.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({
            "math": "保存值 = diag(s) = diag(1/div.mul_scale)；逆变换: x = x' @ diag(s)（matmul 形式）",
            "reason": "msprobe 后处理只支持 matmul，逐元素乘法 x*s 用 x@diag(s) 等价替换",
            "scales": index,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n       索引文件: {index_path}")
    print(f"\n[DONE] 抑制因子已转换为 diag(s) 对角矩阵 npy 格式")
    print(f"       后续用于 gen_postprocess_config.py 生成 msprobe tensor 后处理配置")
    print(f"       逆变换公式（matmul 形式）: x = x' @ diag(s)（对内部 Linear 的输入做后处理）")

    logger.log("convert_suppression", {
        "input": args.quant_weights,
        "output_dir": args.output_dir,
        "index_path": index_path,
        "scales_count": len(scales),
    })


if __name__ == "__main__":
    main()
