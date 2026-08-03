#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
生成 msprobe dump 配置（probe.json），用于 vllm serve 集成。

vllm 通过 --additional-config 的 dump_config_path 字段指定此 JSON 文件，
格式与 msprobe 原生 config.json 完全一致（无 vllm 专有字段）。
vllm-ascend 在 NPUModelRunner.__init__ 中读取 dump_config_path，
作为 config_path 传给 PrecisionDebugger(config_path) 加载。

dump 触发机制：
  - 每次推理请求（execute_model）触发一次 start→前向→stop→step
  - step 配置项过滤：step=[0] 表示只采集第一次推理请求
  - rank 配置项过滤：rank=[0] 表示只采集 rank 0

默认配置（精度异常定位用）：
  - task=tensor：保存原始 tensor（支持逆变换后比对）
  - level=L0：模块级（对应浮点 vs 量化的 module 对比）
  - step=[0]：只采集一个 token（一个推理请求）
  - rank=[0]：只采集一个设备
  - async_dump=false：同步 dump（确保数据完整）

用法：
  python3 gen_msprobe_config.py \
      --output /workdir/probe_fp.json \
      --dump-path /workdir/dump_fp

生成后通过 vllm serve 集成：
  vllm serve /path/to/model \
      --additional-config '{"dump_config_path": "/workdir/probe_fp.json", ...}' \
      --enforce-eager \
      ...
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_log import add_audit_arg, get_logger


def parse_args():
    p = argparse.ArgumentParser(description="生成 msprobe dump 配置（probe.json，用于 vllm serve）")
    p.add_argument(
        "-o",
        "--output",
        required=True,
        help="输出 probe.json 路径",
    )
    p.add_argument(
        "-d",
        "--dump-path",
        required=True,
        help="dump 数据保存目录（如 /workdir/dump_fp）",
    )
    p.add_argument(
        "--task",
        default="tensor",
        choices=["tensor", "statistics"],
        help="dump 任务类型，默认 tensor（保存原始 tensor，支持逆变换后比对）",
    )
    p.add_argument(
        "--level",
        default="L0",
        choices=["L0", "L1", "mix"],
        help="dump 粒度，L0=模块级（默认），L1=API 级，mix=混合",
    )
    p.add_argument(
        "--step",
        nargs="*",
        type=int,
        default=[0],
        help="采集的 step 列表，默认 [0]（只采集第 0 步，即一个推理请求/一个 token）",
    )
    p.add_argument(
        "--rank",
        nargs="*",
        type=int,
        default=[0],
        help="采集的 rank 列表，默认 [0]（只采集 rank 0）",
    )
    p.add_argument(
        "--data-mode",
        nargs="*",
        default=["all"],
        choices=["all", "input", "output", "forward", "backward"],
        help="数据模式，默认 [all]（all 不可与其他组合）",
    )
    p.add_argument(
        "--summary-mode",
        default="statistics",
        choices=["statistics", "md5", "xor"],
        help="摘要模式（statistics 模式下有效），默认 statistics",
    )
    p.add_argument(
        "--scope",
        nargs=2,
        default=None,
        metavar=("START", "END"),
        help="dump 范围区间（L0 填模块名，L1 填 API 名），如 "
             "--scope Module.model.layers.0.input_layernorm.LayerNorm.forward.0 "
             "Module.model.layers.0.post_attention_layernorm.LayerNorm.forward.0",
    )
    p.add_argument(
        "--module-list",
        nargs="*",
        default=None,
        help="自定义采集列表（list 字段），指定具体模块或 API 名",
    )
    p.add_argument(
        "--async-dump",
        action="store_true",
        help="启用异步 dump（默认同步，确保数据完整）",
    )
    p.add_argument(
        "--dump-enable",
        default=None,
        choices=[True, False],
        type=bool,
        help="dump 开关，配置后支持运行中热更新（修改 probe.json 的 dump_enable 字段即可）",
    )
    add_audit_arg(p)
    return p.parse_args()


def main():
    args = parse_args()
    logger = get_logger(args)

    # dump_path 必须存在或可创建
    os.makedirs(args.dump_path, exist_ok=True)

    # 构建配置（严格遵循 msprobe config.json schema）
    config = {
        "task": args.task,
        "dump_path": os.path.abspath(args.dump_path),
        "rank": args.rank,
        "step": args.step,
        "level": args.level,
        "async_dump": args.async_dump,
    }

    # dump_enable 字段（可选，配置后支持热更新）
    if args.dump_enable is not None:
        config["dump_enable"] = args.dump_enable

    # task 子配置块（scope/list/data_mode/summary_mode 都在 task 子块内）
    task_block = {
        "scope": args.scope if args.scope else [],
        "list": args.module_list if args.module_list else [],
        "data_mode": args.data_mode,
    }

    if args.task == "tensor":
        config["tensor"] = task_block
    elif args.task == "statistics":
        task_block["summary_mode"] = args.summary_mode
        config["statistics"] = task_block

    # 校验
    if args.task == "statistics" and args.summary_mode == "md5":
        print(f"[WARN] statistics+md5 模式只保存哈希值，无法做逆变换后比对")
        print(f"       精度异常定位推荐使用 task=tensor（保存原始 tensor）")

    if "all" in args.data_mode and len(args.data_mode) > 1:
        print(f"[ERROR] data_mode=all 不可与其他模式组合")
        raise SystemExit(1)

    # 写入文件
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"[DONE] probe.json 已生成: {args.output}")
    print(f"       dump_path: {config['dump_path']}")
    print(f"       task={config['task']}, level={config['level']}")
    print(f"       step={config['step']}（{len(args.step)} 个推理请求）")
    print(f"       rank={config['rank']}（{len(args.rank)} 个设备）")
    print(f"       async_dump={config['async_dump']}")

    # 输出 vllm serve 命令模板
    print(f"\n使用方式（vllm serve 集成）:")
    print(f"  vllm serve /path/to/model \\")
    print(f"      --host 0.0.0.0 \\")
    print(f"      --port 8900 \\")
    print(f"      --trust-remote-code \\")
    print(f"      --max-model-len 20480 \\")
    print(f"      --tensor-parallel-size 1 \\")
    print(f"      --distributed-executor-backend mp \\")
    print(f"      --enforce-eager \\")
    print(f"      --additional-config '{{\"dump_config_path\": \"{os.path.abspath(args.output)}\"}}'")
    print(f"\n  注意：")
    print(f"  - --enforce-eager 是必须的（msprobe dump 只在 eager 模式下生效）")
    print(f"  - 发送一个推理请求即可触发 dump（step=[0] 只采集这一次）")
    print(f"  - 多卡场景可通过 --tensor-parallel-size 和 rank=[0] 控制只采集 rank 0")

    logger.log("gen_msprobe_config", {
        "output": args.output,
        "dump_path": config["dump_path"],
        "task": config["task"],
        "level": config["level"],
        "step": config["step"],
        "rank": config["rank"],
    })


if __name__ == "__main__":
    main()
