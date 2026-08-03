#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
将旋转矩阵从 safetensors 格式转换为 npy 格式。

msmodelslim 保存的旋转矩阵格式为 safetensors（optional/quarot.safetensors 的 global_rotation key），
而 msprobe tensor 后处理只支持 pt/npy 格式，需要转换。

用法：
  python3 convert_rotation_to_npy.py \
      --quarot-safetensors /path/to/quant_model/optional/quarot.safetensors \
      --rotation-key global_rotation \
      --output /workdir/rotation.npy
"""

import argparse
import os
import sys

import numpy as np
import torch
from safetensors import safe_open

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_log import add_audit_arg, get_logger


def parse_args():
    p = argparse.ArgumentParser(description="旋转矩阵格式转换：safetensors → npy")
    p.add_argument(
        "-q",
        "--quarot-safetensors",
        required=True,
        help="quarot.safetensors 文件路径",
    )
    p.add_argument(
        "-k",
        "--rotation-key",
        default="global_rotation",
        help="旋转矩阵在 safetensors 中的 key（默认 global_rotation）",
    )
    p.add_argument(
        "-o",
        "--output",
        required=True,
        help="输出 npy 文件路径",
    )
    add_audit_arg(p)
    return p.parse_args()

def main():
    args = parse_args()
    logger = get_logger(args)

    # 1. 加载旋转矩阵
    print("[1/3] 加载旋转矩阵 ...")
    if not os.path.exists(args.quarot_safetensors):
        print(f"[ERROR] 文件不存在: {args.quarot_safetensors}")
        raise SystemExit(1)

    with safe_open(args.quarot_safetensors, framework="pt", device="cpu") as f:
        if args.rotation_key not in f.keys():
            available = list(f.keys())
            print(f"[ERROR] safetensors 中无 '{args.rotation_key}' key，可用 keys: {available}")
            raise SystemExit(1)
        R = f.get_tensor(args.rotation_key)

    print(f"       {args.quarot_safetensors}")
    print(f"       key={args.rotation_key}, shape={tuple(R.shape)}, dtype={R.dtype}")

    # 2. 验证正交性
    print("\n[2/3] 验证正交性 ...")
    ortho_err = None
    if R.shape[0] != R.shape[1]:
        print(f"[WARN] R 不是方阵: {R.shape}，跳过正交性检查")
    else:
        ortho_err = (R @ R.T - torch.eye(R.shape[0])).abs().max().item()
        status = "PASS" if ortho_err < 1e-5 else "WARN: 非正交"
        print(f"       max|R@R^T - I| = {ortho_err:.2e} ({status})")
        if ortho_err >= 1e-5:
            print(f"[WARN] R 非正交，逆变换可能不准确（本 Skill 假设 R 正交）")

    # 3. 转换为 npy 并保存
    print("\n[3/3] 保存为 npy ...")
    R_np = R.float().numpy()
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    np.save(args.output, R_np)

    print(f"       {args.output}")
    print(f"       shape={R_np.shape}, dtype={R_np.dtype}")
    print(f"\n[DONE] 旋转矩阵已转换为 npy 格式")
    print(f"       后续用于 gen_postprocess_config.py 生成 msprobe tensor 后处理配置")

    logger.log("convert_rotation", {
        "input": args.quarot_safetensors,
        "output": args.output,
        "key": args.rotation_key,
        "shape": list(R_np.shape),
        "dtype": str(R_np.dtype),
        "ortho_err": ortho_err,
    })


if __name__ == "__main__":
    main()
