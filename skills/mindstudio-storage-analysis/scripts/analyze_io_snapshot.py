#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#    http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------
"""
IO Snapshot 确定性分析器（mindstudio-storage-analysis）。

输入 collect_io_snapshot.py 产出的 IO Snapshot（dict 或 JSON 文件），
输出结构化 findings 列表，每条包含：
    rule_id / severity / confidence / evidence_fields / missing_evidence
    / summary / recommended_next_checks

设计要点：
  - mte2_ratio 完全移出 NPU 传导链的"必需证据"。MTE2 是 AI Core 内部/邻近
    存储层的数据搬运（GM→UB/L1），高占比只代表算子内数据搬运压力，不能证明
    Host 存储/DataLoader 供给不足。高 mte2 + Host IO 正常时应转交计算分析。
  - NPU 传导链用 step throughput / device Free / DataLoader wait / batch ready
    与 Host IO 异常的"同窗相关性"，三档置信度。
  - R200 拆成两层：仅"识别为网络挂载"不构成瓶颈，必须有同窗
    RTT/execute/retrans/major-timeout 性能证据才能确认。
  - 阈值不写成跨设备绝对真理：优先支持设备基线/用户规格/对照实验，通用阈值仅弱提示。
  - 未知 schema major 版本拒绝确定性分析（返回明确错误）。

用法:
    python3 analyze_io_snapshot.py io_snapshot.json
    python3 analyze_io_snapshot.py io_snapshot.json --mode all
    python3 analyze_io_snapshot.py io_snapshot.json -o findings.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Preserve the historical import surface while the implementation lives in modules.
from _analysis.common import (  # noqa: F401
    RULE_IDS,
    SUPPORTED_MAJOR,
    _ORDER,
    _PROVIDER_NAMES,
    _SCHEMA_VERSION_RE,
    _canonical_dev,
    _delta,
    _f,
    _finding,
    _finding_evidence_interval,
    _format_device_pid_map,
    _major,
    _parse_iso,
    _parsed,
    _profile_host_overlap_rules,
    _profile_interval,
    _profile_window_matches_snapshot,
    _provider,
    _provider_interval,
    _snapshot_duration,
    _snapshot_interval,
    _status,
    _validate_schema_version,
    interval_overlap_ratio,
    intervals_have_common_overlap,
    intervals_overlap_or_are_adjacent,
)

from _analysis.contract import (  # noqa: F401
    _BOOT_ID_PATTERN,
    _CERTIFIED_PROFILE_PROVENANCE,
    _CERTIFIED_PROFILE_SCOPE,
    _DEVICE_BASELINE_FIELDS,
    _INTEGER_FIELDS,
    _IOSTAT_EVIDENCE_FIELDS,
    _IOSTAT_FIELD_SAMPLE_COUNTS,
    _IOSTAT_PAIRED_SAMPLE_BASES,
    _MAX_JSON_DEPTH,
    _MAX_JSON_FILE_BYTES,
    _MAX_JSON_NODES,
    _NUMERIC_FIELDS,
    _PERCENT_FIELDS,
    _VALID_EXPERIMENT_RESULTS,
    _audit_string,
    _audit_target,
    _audit_window,
    _certified_profile_metrics,
    _normalize_controlled_experiment,
    _normalize_device_baselines,
    _normalize_experiment_observation,
    _normalize_overlap_provenance,
    _normalize_profile,
    _normalize_profile_with_errors,
    _process_map_identity_error,
    _profile_metric_is_certified,
    _profile_provenance_error,
    _strict_float,
    _strict_json_int,
    _target_pid_scope,
    _validate_json_resources,
    _validate_numeric_dict,
    normalize_and_validate,
    validate_analysis_request,
)

from _analysis.local import (  # noqa: F401
    _AVGQU_HIGH,
    _AWAIT_BY_TYPE,
    _BANDWIDTH_KBPS_HEURISTIC,
    _IOPS_HEURISTIC,
    _MIN_SAMPLES_HIGH,
    _UTIL_HIGH,
    _UTIL_SUSTAINED_MEAN,
    _await_threshold,
    _classify_r100_disk,
    _collect_disks,
    _compute_disk_rates,
    _disks_from_iostat,
    analyze_r000,
    analyze_r100,
)

from _analysis.path_scope import (  # noqa: F401
    _NON_DATA_PATH_PREFIXES,
    _NON_DATA_PATH_SUFFIXES,
    _canonicalize_path,
    _is_data_relevant_path,
)

from _analysis.network import (  # noqa: F401
    _GLUSTER_SMALL_READ_SYSCALLS_PER_SECOND,
    _IOPS_HIGH,
    _analyze_gluster_r200,
    _bind_gluster_metrics,
    _bind_nfs_metrics,
    _gluster_activity,
    _gluster_identity,
    _gluster_small_read_candidates,
    _is_glusterfs_mount,
    _nfs_identity,
    _nfs_identity_dict,
    _norm_fstype_group,
    _norm_nfs_source,
    _path_under_mount,
    _required_gluster_mounts,
    _required_nfs_identities,
    analyze_r200,
    analyze_r300,
)

from _analysis.contention import (  # noqa: F401
    _NON_CONTENTION_DEVICES,
    _NON_CONTENTION_FSTYPES,
    _active_io_pids,
    _common_mapping_interval,
    _is_contention_storage_mapping,
    _mapping_observation_interval,
    analyze_r400,
)

from _analysis.npu import (  # noqa: F401
    _host_io_ruled_out,
    _host_io_ruled_out_in_profile_window,
    _is_confirmed_host_issue,
    _profile_overlaps_finding,
    _project_host_assessments_to_target,
    _target_binding_is_certified,
    _target_device_context,
    _target_device_mapping_intervals,
    analyze_r500_with_host,
)


def _temp_name(path: str) -> str:
    """生成同目录下含 PID 和随机串的唯一临时文件名。"""
    import uuid

    d, base = os.path.split(path)
    return os.path.join(d or ".", f".{base}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def analyze_all(snapshot: dict, profile: dict | None = None) -> dict:
    """运行全部规则，返回 {schema_version, findings: [...], summary}。

    经 validate_analysis_request 统一入口；schema/顶层 fatal 转为结构化 error。
    validation_errors / profile_validation_errors 提升到顶层，让 agent/用户可见。
    """
    snapshot, profile, verr, pverr, fatal = validate_analysis_request(snapshot, profile)
    if fatal:
        return {"error": fatal, "schema_version": "unknown", "findings": []}
    return _analyze_validated(snapshot, profile, verr, pverr)


def _analyze_validated(
    snapshot: dict,
    profile: dict,
    verr: list[str] | None = None,
    pverr: list[str] | None = None,
) -> dict:
    """分析已经完成统一规范化的请求，避免 CLI/all 二次清洗丢失诊断。"""
    verr = list(verr or [])
    pverr = list(pverr or [])
    sv = snapshot.get("schema_version", "1.0")

    # 先算 R100，再算 R200/R300/R400（R400 依赖 R100 的饱和设备集）。
    r100 = analyze_r100(snapshot)
    r200 = analyze_r200(snapshot)
    r300 = analyze_r300(snapshot)
    r400 = analyze_r400(snapshot, r100)

    findings = [analyze_r000(snapshot), r100, r200, r300, r400]

    # NPU 传导链的 Host IO 入口是 R100~R400 的并集。
    findings.append(analyze_r500_with_host(snapshot, profile or {}, [r100, r200, r300, r400]))

    high = [f for f in findings if f.get("severity") == "high" and f.get("confidence") in ("high", "medium")]
    summary = (
        "未发现高置信度存储瓶颈。"
        if not high
        else (f"发现 {len(high)} 个高优先级问题：" + "; ".join(f["rule_id"] for f in high))
    )
    result = {
        "schema_version": sv,
        "analyzed_at": None,
        "findings": findings,
        "summary": summary,
        "high_priority_count": len(high),
    }
    # 将 validation_errors 与 profile_validation_errors 提升到顶层。
    if verr:
        result["validation_errors"] = verr
    if pverr:
        result["profile_validation_errors"] = pverr
    return result


def load_snapshot(path: str) -> dict:
    size = os.path.getsize(path)
    if size > _MAX_JSON_FILE_BYTES:
        raise ValueError(f"JSON file is {size} bytes; limit is {_MAX_JSON_FILE_BYTES} bytes")
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    _validate_json_resources(payload)
    return payload


def _r500_host_findings_for_standalone(snapshot: dict) -> list[dict]:
    """单规则模式（--mode R500）下计算 Host 根因集合，复用 analyze_all 的同一逻辑。"""
    r100 = analyze_r100(snapshot)
    r200 = analyze_r200(snapshot)
    r300 = analyze_r300(snapshot)
    r400 = analyze_r400(snapshot, r100)
    return [r100, r200, r300, r400]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="IO Snapshot 确定性分析器（mindstudio-storage-analysis）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("snapshot", help="IO Snapshot JSON 文件路径")
    parser.add_argument(
        "--mode",
        "-m",
        default="all",
        choices=["all", "R000", "R100", "R200", "R300", "R400", "R500"],
        help="分析模式（默认 all）",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="可选的 NPU profiler 指标 JSON（含 device_free_percent/mte2_ratio）",
    )
    parser.add_argument("-o", "--output", default=None, help="输出 findings JSON 文件路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认即 JSON）")
    args = parser.parse_args(argv)

    try:
        snapshot = load_snapshot(args.snapshot)
    except FileNotFoundError:
        print(f"错误: Snapshot 文件不存在: {args.snapshot}", file=sys.stderr)
        return 1
    except (ValueError, RecursionError, UnicodeDecodeError) as e:
        print(f"错误: Snapshot JSON 解析失败: {args.snapshot}: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        # 目录路径、权限及其他 OS 错误稳定返回非零且不泄露堆栈。
        print(f"错误: 无法读取 Snapshot {args.snapshot}: {e}", file=sys.stderr)
        return 1

    profile = None
    if args.profile:
        try:
            profile = load_snapshot(args.profile)
        except (OSError, ValueError, RecursionError, UnicodeDecodeError) as e:
            print(f"错误: profile 解析失败: {e}", file=sys.stderr)
            return 1

    # 所有 mode 共用 validate_analysis_request 处理 schema、时间和顶层错误。
    snapshot, profile, verr, pverr, fatal = validate_analysis_request(snapshot, profile)
    if args.profile and not ({"device_free_percent", "mte2_ratio"} & profile.keys()):
        pverr = sorted(set(pverr) | {"显式 --profile 未提供可用的 device_free_percent 或 mte2_ratio"})
    profile_invalid = bool(args.profile and pverr)
    if fatal:
        result = {"error": fatal, "schema_version": "unknown", "findings": []}
    elif args.mode == "all":
        result = _analyze_validated(snapshot, profile, verr, pverr)
    else:
        func = {
            "R000": lambda: analyze_r000(snapshot),
            "R100": lambda: analyze_r100(snapshot),
            "R200": lambda: analyze_r200(snapshot),
            "R300": lambda: analyze_r300(snapshot),
            "R400": lambda: analyze_r400(snapshot, analyze_r100(snapshot)),
            "R500": lambda: analyze_r500_with_host(
                snapshot, profile or {}, _r500_host_findings_for_standalone(snapshot)
            ),
        }[args.mode]
        result = func()
        # 单规则结果也携带 validation_errors。
        if isinstance(result, dict):
            if verr:
                result["validation_errors"] = verr
            if pverr:
                result["profile_validation_errors"] = pverr

    # 顶层 error、unsupported schema 和输出写入失败均返回非零。
    out_text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        tmp = _temp_name(args.output)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(out_text)
            os.replace(tmp, args.output)
            print(f"findings 已写入: {args.output}", file=sys.stderr)
        except OSError as exc:
            print(f"错误: 无法写入 {args.output}: {exc}", file=sys.stderr)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return 3
    else:
        print(out_text)
    if isinstance(result, dict) and "error" in result:
        return 2
    if profile_invalid:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
