#!/usr/bin/env python3
"""性能对比报告 — 从 baseline+after json 生成 Δ% 对比表。

用法:
  python3 scripts/delta_report.py \
    --baseline results/baseline_board_xxx.json \
    --after results/after_board_xxx.json \
    --output delta_report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


COMPARABILITY_KEYS = (
    "op", "soc", "mode", "timing_method", "baseline_kind", "cann_version", "device_id",
    "shape", "dtype", "format", "tiling_key", "block_dim", "warmup", "repeat",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成优化前后 Δ% 对比报告")
    parser.add_argument("--baseline", required=True, help="基线 result json")
    parser.add_argument("--after", required=True, help="优化后 result json")
    parser.add_argument("--output", required=True, help="输出 markdown 报告路径")
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))

    differences = []
    for key in COMPARABILITY_KEYS:
        before_value = baseline.get(key)
        after_value = after.get(key)
        if before_value is not None and after_value is not None and before_value != after_value:
            differences.append(f"{key}: {before_value!r} != {after_value!r}")
    if differences:
        print("ERROR: 基线与优化后结果口径不一致：", file=sys.stderr)
        for item in differences:
            print(f"  - {item}", file=sys.stderr)
        return 2

    if baseline.get("precision") != "pass" or after.get("precision") != "pass":
        print("ERROR: 只有精度均为 pass 的结果才能生成性能结论", file=sys.stderr)
        return 2

    b_us = baseline.get("kernel_avg_us", 0)
    a_us = after.get("kernel_avg_us", 0)
    speedup = b_us / a_us if a_us > 0 else 0
    delta_pct = ((a_us - b_us) / b_us * 100) if b_us > 0 else 0

    op = baseline.get("op", after.get("op", "unknown"))
    soc = baseline.get("soc_full", baseline.get("soc", "?"))

    lines = [
        f"# 性能对比报告：{op}",
        "",
        f"| 项目 | 基线 (before) | 优化后 (after) | Δ% | 加速比 |",
        f"|---|---|---|---|---|",
        f"| kernel 耗时 | {b_us:.2f} µs | {a_us:.2f} µs | {delta_pct:+.1f}% | {speedup:.2f}x |",
        f"| 精度 | {baseline.get('precision','?')} | {after.get('precision','?')} | — | — |",
        f"| 芯片 | {baseline.get('soc','?')} | {after.get('soc','?')} | — | — |",
        f"| 模式 | {baseline.get('mode','?')} | {after.get('mode','?')} | — | — |",
        f"| shape | {baseline.get('shape','?')} | {after.get('shape','?')} | — | — |",
        f"| dtype | {baseline.get('dtype','?')} | {after.get('dtype','?')} | — | — |",
        "",
        f"## 结论",
        "",
    ]
    if speedup > 1.0:
        lines.append(f"性能提升 {speedup:.2f}x（{delta_pct:+.1f}%），可进入稳定性复测。")
    elif speedup >= 0.98:
        lines.append(f"性能变化可能位于噪声范围（{speedup:.2f}x），需按原口径增加采样确认。")
    else:
        lines.append(f"性能劣化 {speedup:.2f}x（{delta_pct:+.1f}%），应回滚该轮修改。")

    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告已生成: {args.output}")
    print(f"加速比: {speedup:.2f}x (Δ={delta_pct:+.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
