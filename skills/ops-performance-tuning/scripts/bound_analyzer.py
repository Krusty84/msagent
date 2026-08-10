#!/usr/bin/env python3
"""Bound 初筛工具 — 从 msOpProf PipeUtilization.csv 输出六档 Bound 候选。

用法:
  python3 scripts/bound_analyzer.py --csv PipeUtilization.csv --output bound_report.json

六档 Bound 规则（最终结论仍需结合带宽、时间线和源码证据）:
  MTE2 BOUND / CUBE BOUND / VEC BOUND / FIXP BOUND / MTE3 BOUND / SCALAR BOUND / 无 bound
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


def parse_float(value: str) -> float:
    try:
        return float(value.strip().replace("%", ""))
    except (ValueError, AttributeError):
        return 0.0


def ratio_to_pct(value: float) -> float:
    """兼容 0~1 比例和 0~100 百分数两种版本字段。"""
    return value * 100 if 0 <= value <= 1.5 else value


def load_csv(csv_path: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open(csv_path, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def determine_bound(rows: list[dict[str, str]]) -> dict[str, Any]:
    """从 PipeUtilization.csv 生成六档 Bound 候选。"""
    # 尝试读取 msprof 常见字段
    fields: dict[str, float] = {}
    for row in rows:
        for key, val in row.items():
            v = parse_float(str(val))
            if v > 0:
                fields[key] = max(fields.get(key, 0), v)

    # 尝试多种可能的列名（覆盖 msprof 实际格式 + 不同版本变体）
    # msprof PipeUtilization.csv 格式: aic_mte2_ratio, aiv_vec_ratio 等（小数，0.92=92%）
    mte2_pct = max(
        ratio_to_pct(fields.get("aic_mte2_ratio", 0)),
        fields.get("Mte2Utilization(%)", 0),
        ratio_to_pct(fields.get("mte2_ratio", 0)),
        fields.get("MTE2(%)", 0),
    )
    mte3_pct = max(
        ratio_to_pct(fields.get("aic_mte3_ratio", 0)),
        fields.get("Mte3Utilization(%)", 0),
        ratio_to_pct(fields.get("mte3_ratio", 0)),
        fields.get("MTE3(%)", 0),
    )
    cube_pct = max(
        ratio_to_pct(fields.get("aic_cube_ratio", 0)),
        fields.get("CubeUtilization(%)", 0),
        ratio_to_pct(fields.get("cube_ratio", 0)),
        fields.get("CUBE(%)", 0),
    )
    vec_pct = max(
        ratio_to_pct(fields.get("aiv_vec_ratio", 0)),
        ratio_to_pct(fields.get("aic_vec_ratio", 0)),
        fields.get("VectorUtilization(%)", 0),
        ratio_to_pct(fields.get("vec_ratio", 0)),
        fields.get("VECTOR(%)", 0),
    )
    fixp_pct = max(
        ratio_to_pct(fields.get("aic_fixpipe_ratio", 0)),
        fields.get("FixpipeUtilization(%)", 0),
        ratio_to_pct(fields.get("fixp_ratio", 0)),
        fields.get("FIXP(%)", 0),
    )
    scalar_pct = max(
        ratio_to_pct(fields.get("aic_scalar_ratio", 0)),
        fields.get("ScalarUtilization(%)", 0),
        ratio_to_pct(fields.get("scalar_ratio", 0)),
        fields.get("SCALAR(%)", 0),
    )

    categories = {
        "MTE2": mte2_pct,
        "CUBE": cube_pct,
        "VEC": vec_pct,
        "FIXP": fixp_pct,
        "MTE3": mte3_pct,
        "SCALAR": scalar_pct,
    }

    # Bound 判定逻辑
    bound = "无 bound"
    reason = ""

    max_cat = max(categories, key=categories.get)  # type: ignore[arg-type]
    max_val = categories[max_cat]

    if max_val > 80:
        bound = f"{max_cat} BOUND"
        reason = f"{max_cat} busy 占 {max_val:.1f}%，大于 80%"
    elif max_val > 70:
        # 检查是否占比最大且大于 70%
        bound = f"{max_cat} BOUND"
        reason = f"{max_cat} busy 占 {max_val:.1f}%，占比最大且大于 70%"
    elif max_val < 10 and all(v < 10 for v in categories.values()):
        bound = "无 bound"
        reason = f"所有 busy 均低于 10%，最高 {max_cat}={max_val:.1f}%"

    # 优化方向推荐
    recommendation_map = {
        "MTE2": ("数据搬运优化", [
            "references/optimize-data-copy.md",
            "references/optimize-memory-hierarchy.md",
            "PR: kv_rms_norm_rope_cache (MemBase→RegBase)",
        ]),
        "MTE3": ("数据搬出优化", [
            "references/optimize-data-copy.md",
        ]),
        "CUBE": ("Cube 利用率优化", [
            "references/optimize-ascendc-tiling.md",
            "references/optimize-catlass.md",
            "PR: matmul_story (baseline→SWAT→尾轮均衡→UnitFlag)",
            "PR: grouped_matmul (tiling+数据搬运+MXFP4/MXFP8)",
        ]),
        "VEC": ("Vector 利用率优化", [
            "references/optimize-api-usage.md",
            "references/optimize-pipeline.md",
            "references/optimize-memory-hierarchy.md",
            "PR: gelu_eltwise_regbase (MemBase→RegBase 5步)",
            "PR: simd_vf_story (Broadcast/Elemwise/Reduce VF范式)",
        ]),
        "FIXP": ("Fixpipe 优化", [
            "references/optimize-pipeline.md",
            "PR: flash_attn_lite (CV双槽→双缓冲 v0~v5)",
        ]),
        "SCALAR": ("标量削减", [
            "references/optimize-api-usage.md",
            "PR: scalar_story (icache预取+静态Tensor+指针消除)",
        ]),
    }
    rec = recommendation_map.get(max_cat, ("进一步分析", [
        "references/profile-msopprof.md",
    ]))

    return {
        "bound": bound,
        "reason": reason,
        "categories": {k: round(v, 1) for k, v in categories.items()},
        "recommendation": {"direction": rec[0], "docs": rec[1]},
        "source_file": None,
        "row_count": len(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="六档 Bound 自动判定")
    parser.add_argument("--csv", default=None, help="msprof PipeUtilization.csv 路径")
    parser.add_argument("--msprof-dir", default=None, help="msprof 输出目录，自动搜索 **/PipeUtilization.csv")
    parser.add_argument("--output", required=True, help="bound_report.json 输出路径")
    args = parser.parse_args()

    csv_path = args.csv
    if not csv_path and args.msprof_dir:
        # 自动搜索 msprof 目录下的 PipeUtilization.csv
        msprof_root = Path(args.msprof_dir)
        if not msprof_root.is_dir():
            print(f"ERROR: msprof 目录不存在: {args.msprof_dir}", file=sys.stderr)
            return 1
        candidates = list(msprof_root.rglob("PipeUtilization.csv"))
        if not candidates:
            print(f"ERROR: 在 {args.msprof_dir} 下未找到 PipeUtilization.csv", file=sys.stderr)
            print("提示: 确保 msprof op 采集时使用了 --aic-metrics=PipeUtilization", file=sys.stderr)
            return 1
        # 优先选 roofline 目录下的
        roofline = [c for c in candidates if "roofline" in str(c).lower()]
        chosen = roofline[0] if roofline else candidates[0]
        csv_path = str(chosen)
        print(f"自动选择: {csv_path}")
    if not csv_path:
        print("ERROR: 必须提供 --csv 或 --msprof-dir", file=sys.stderr)
        return 1
    if not Path(csv_path).is_file():
        print(f"ERROR: CSV 文件不存在: {csv_path}", file=sys.stderr)
        return 1

    rows = load_csv(csv_path)
    result = determine_bound(rows)
    result["source_file"] = str(Path(csv_path).resolve())

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")

    print(f"Bound 判定: {result['bound']}")
    print(f"原因: {result['reason']}")
    if "recommendation" in result:
        print(f"优化方向: {result['recommendation']['direction']}")
        print(f"参考文档: {', '.join(result['recommendation']['docs'])}")
    print(f"结果已保存: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
