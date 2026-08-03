#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
以调试模式运行 msmodelslim 量化，复现量化过程以获取旋转和抑制中间量。

msmodelslim CLI 调用形式：
  msmodelslim quant \
    --model_type MiniMax-M3 \
    --model_path <model_path> \
    --save_path <save_path> \
    --device npu:0,1,2,3 \
    --quant_type w8a8 \
    --trust_remote_code True \
    --debug

产物（在 <save_path> 下）：
  - quant_model_weights.safetensors（量化权重）
  - quant_model_description.json
  - debug_info/debug_info.safetensors（因 --debug 触发，含 smooth_scales）
  - optional/quarot.safetensors（仅当 yaml 配置含 QuaRot processor 时产出）

本脚本封装上述 CLI 调用，并提供：
  1. 量化前的产物存在性检查（避免重复量化）
  2. 量化后的产物校验（确认 quarot.safetensors / debug_info.safetensors 已产出）
  3. 审计日志记录

用法：
  python3 run_quantization.py \
    --model-type MiniMax-M3 \
    --model-path /path/to/model \
    --save-path /workdir/quant_model \
    --device npu:0,1,2,3 \
    --quant-type w8a8 \
    --trust-remote-code \
    --debug \
    [--config-path /path/to/best_practice.yaml] \
    [--audit-log /workdir/audit.jsonl]

  # 若已有量化产物且含 quarot.safetensors + debug_info.safetensors，则跳过量化
  python3 run_quantization.py \
    --model-type MiniMax-M3 \
    --model-path /path/to/model \
    --save-path /workdir/quant_model \
    --quant-type w8a8 \
    --trust-remote-code \
    --debug \
    --skip-if-exists
"""

import argparse
import os
import subprocess
import sys
from typing import List, Optional

# 支持直接 import audit_log（同目录脚本）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_log import AuditLogger, add_audit_arg, get_logger


def parse_args():
    p = argparse.ArgumentParser(description="以调试模式运行 msmodelslim 量化")
    p.add_argument("--model-type", required=True, help="模型类型，如 MiniMax-M3")
    p.add_argument("--model-path", required=True, help="原始浮点模型路径")
    p.add_argument("--save-path", required=True, help="量化产物保存路径")
    p.add_argument("--device", default="npu", help="量化设备，如 npu / npu:0,1,2,3 / cpu")
    p.add_argument("--quant-type", help="量化类型（如 w8a8），与 --config-path 互斥")
    p.add_argument("--config-path", help="自定义量化配置 yaml 路径，与 --quant-type 互斥")
    p.add_argument("--trust-remote-code", action="store_true", help="是否信任远程代码")
    p.add_argument("--debug", action="store_true", default=True,
                   help="启用调试模式（产出 debug_info.safetensors）。默认开启")
    p.add_argument("--no-debug", dest="debug", action="store_false",
                   help="关闭调试模式（不推荐，Fusion 路径需要 debug_info）")
    p.add_argument("--tag", default=None, help="场景标签")
    p.add_argument("--skip-if-exists", action="store_true",
                   help="若产物已存在且完整，则跳过量化")
    p.add_argument("--msmodelslim-cmd", default="msmodelslim",
                   help="msmodelslim 命令路径（默认从 PATH 查找）")
    add_audit_arg(p)
    return p.parse_args()


def build_command(args) -> List[str]:
    """构造 msmodelslim quant 命令。"""
    cmd = [args.msmodelslim_cmd, "quant",
           "--model_type", args.model_type,
           "--model_path", args.model_path,
           "--save_path", args.save_path,
           "--device", args.device]

    if args.config_path:
        cmd.extend(["--config_path", args.config_path])
    elif args.quant_type:
        cmd.extend(["--quant_type", args.quant_type])
    else:
        print("[ERROR] 必须指定 --quant-type 或 --config-path 之一")
        raise SystemExit(1)

    if args.trust_remote_code:
        cmd.extend(["--trust_remote_code", "True"])

    if args.debug:
        cmd.append("--debug")

    if args.tag:
        cmd.extend(["--tag", args.tag])

    return cmd


def check_products(save_path: str, require_debug: bool, require_quarot: bool) -> dict:
    """
    检查量化产物是否完整。

    返回 dict，含各产物路径和是否存在标志。
    """
    products = {
        "quant_weights": {
            "path": os.path.join(save_path, "quant_model_weights.safetensors"),
            "exists": False,
        },
        "quant_description": {
            "path": os.path.join(save_path, "quant_model_description.json"),
            "exists": False,
        },
        "debug_info": {
            "path": os.path.join(save_path, "debug_info", "debug_info.safetensors"),
            "exists": False,
        },
        "quarot": {
            "path": os.path.join(save_path, "optional", "quarot.safetensors"),
            "exists": False,
        },
    }

    for key, info in products.items():
        # quant_model_weights 可能分片（.0.safetensors / .1.safetensors）
        if key == "quant_weights":
            parent = os.path.dirname(info["path"])
            basename = os.path.basename(info["path"]).replace(".safetensors", "")
            if os.path.exists(info["path"]):
                info["exists"] = True
            else:
                # 检查分片文件
                shards = [f for f in os.listdir(parent) if f.startswith(basename)
                          and f.endswith(".safetensors")] if os.path.isdir(parent) else []
                info["exists"] = len(shards) > 0
                if shards:
                    info["path"] = os.path.join(parent, f"{shards[0]} (+{len(shards)-1} 分片)" if len(shards) > 1 else shards[0])
        else:
            info["exists"] = os.path.exists(info["path"])

    return products


def main():
    args = parse_args()
    logger = get_logger(args)

    # 1. 检查产物是否已存在
    require_debug = args.debug
    # QuaRot 产物是否需要：若用户指定 config-path 含 quarot，或 quant-type 匹配的实践含 quarot
    # 这里保守地检查：若 quarot.safetensors 存在则视为 QuaRot 场景
    products = check_products(args.save_path, require_debug, require_quarot=False)

    print(f"[1/3] 检查量化产物 @ {args.save_path}")
    for key, info in products.items():
        status = "OK" if info["exists"] else "MISSING"
        print(f"       {key}: {status} ({info['path']})")

    quant_weights_ok = products["quant_weights"]["exists"]
    debug_info_ok = products["debug_info"]["exists"]
    quarot_ok = products["quarot"]["exists"]

    if args.skip_if_exists and quant_weights_ok:
        print(f"\n[2/3] 产物已存在，跳过量化")
        if require_debug and not debug_info_ok:
            print(f"[WARN] 需要 debug_info.safetensors 但缺失！请重新量化并加 --debug")
            logger.log("quantize_skip", {
                "save_path": args.save_path,
                "warn": "debug_info missing but required",
            }, event="warn")
        if not quarot_ok:
            print(f"[INFO] optional/quarot.safetensors 不存在（可能非 QuaRot 场景）")
        logger.log("quantize_skip", {
            "save_path": args.save_path,
            "products": {k: v["exists"] for k, v in products.items()},
        })
        print(f"\n[3/3] 跳过完成")
        return

    # 2. 构造并执行量化命令
    cmd = build_command(args)
    print(f"\n[2/3] 执行量化命令：")
    print(f"       {' '.join(cmd)}")
    logger.log("quantize_start", {
        "cmd": cmd,
        "model_type": args.model_type,
        "model_path": args.model_path,
        "save_path": args.save_path,
        "debug": args.debug,
    })

    try:
        result = subprocess.run(cmd, check=True)
        exit_code = result.returncode
    except subprocess.CalledProcessError as e:
        exit_code = e.returncode
        print(f"\n[ERROR] 量化失败，退出码 {exit_code}")
        logger.log("quantize_end", {
            "exit_code": exit_code,
            "error": str(e),
        }, event="error")
        raise SystemExit(exit_code)
    except FileNotFoundError:
        print(f"\n[ERROR] 未找到 msmodelslim 命令：{args.msmodelslim_cmd}")
        print(f"       请确认 msmodelslim 已安装：pip install msmodelslim")
        logger.log("quantize_end", {
            "error": f"msmodelslim command not found: {args.msmodelslim_cmd}",
        }, event="error")
        raise SystemExit(127)

    # 3. 校验产物
    print(f"\n[3/3] 校验量化产物 ...")
    products = check_products(args.save_path, require_debug, require_quarot=False)
    for key, info in products.items():
        status = "OK" if info["exists"] else "MISSING"
        print(f"       {key}: {status}")

    quant_weights_ok = products["quant_weights"]["exists"]
    debug_info_ok = products["debug_info"]["exists"]
    quarot_ok = products["quarot"]["exists"]

    if not quant_weights_ok:
        print(f"[ERROR] 量化权重未产出：{products['quant_weights']['path']}")
        logger.log("quantize_end", {
            "exit_code": exit_code,
            "products": {k: v["exists"] for k, v in products.items()},
            "error": "quant_weights missing",
        }, event="error")
        raise SystemExit(1)

    if require_debug and not debug_info_ok:
        print(f"[WARN] 调试模式已开启但 debug_info.safetensors 未产出")
        print(f"       可能原因：yaml 配置未触发 DebugInfoPersistence")
        logger.log("quantize_end", {
            "exit_code": exit_code,
            "products": {k: v["exists"] for k, v in products.items()},
            "warn": "debug_info missing despite --debug",
        }, event="warn")
    else:
        print(f"\n[DONE] 量化完成，产物校验通过")
        logger.log("quantize_end", {
            "exit_code": exit_code,
            "products": {k: v["exists"] for k, v in products.items()},
            "save_path": args.save_path,
        })

    if not quarot_ok:
        print(f"[INFO] optional/quarot.safetensors 未产出")
        print(f"       若需 QuaRot 场景，请在 yaml 配置中添加 type: quarot processor")


if __name__ == "__main__":
    main()
