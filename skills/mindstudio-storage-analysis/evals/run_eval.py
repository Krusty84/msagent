#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This file is part of the MindStudio project.
#
# MindStudio is licensed under Mulan PSL v2.
# -------------------------------------------------------------------------
"""mindstudio-storage-analysis 确定性 eval runner（Review P2-1 重构）。

**cases.yaml 是唯一用例清单**：本 runner 解析 evals/cases.yaml，对每个
rootcause/missing 案例校验 fixture/期望并加载对应 JSON fixture，跑 analyzer，
按 `expected_rule` / `expected_confidence` 自动判定，并校验配置一致性
（每个机器可执行案例都必须有 fixture + 期望，避免 YAML 与 runner 漂移）。

诚实边界（不伪造）：
  - 确定性规则 eval：本 runner 可复现（python3 evals/run_eval.py）。
  - 触发/安全 eval（trigger_cases / safety_cases）需 msAgent 运行时 + 固定模型温度，
    且需与"未加载 Skill"基线对比——属人工流程，本 runner 不产出伪 transcript。

退出码：0 全过，1 有失败/配置错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(SKILL_ROOT, "skills", "mindstudio-storage-analysis", "evals")
# 兼容直接从 skill 目录运行
if not os.path.isdir(EVAL_DIR):
    EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(
    SKILL_ROOT, "skills", "mindstudio-storage-analysis", "scripts"
)
if not os.path.isdir(SCRIPTS_DIR):
    SCRIPTS_DIR = os.path.join(os.path.dirname(EVAL_DIR), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import yaml  # noqa: E402

import analyze_io_snapshot as a  # noqa: E402

_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _load_cases() -> dict:
    with open(os.path.join(EVAL_DIR, "cases.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_fixture(name: str) -> dict:
    with open(os.path.join(EVAL_DIR, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


def _is_positive_problem(finding: dict) -> bool:
    """正向问题结论：confidence 与 severity 同时 >= medium（用于反例判定）。"""
    return (
        _ORDER.get(finding.get("confidence"), 0) >= _ORDER["medium"]
        and _ORDER.get(finding.get("severity"), 0) >= _ORDER["medium"]
    )


def _machine_cases(cases: dict) -> list[tuple[str, dict]]:
    """收集所有机器案例；缺 fixture 的配置也必须进入失败校验。"""
    out: list[tuple[str, dict]] = []
    for section in ("rootcause_cases", "missing_data_cases"):
        for case in cases.get(section, []) or []:
            out.append((section, case))
    return out


def _resolve_fixture_path(case: dict) -> str:
    """fixture_file 形如 'evals/run_eval.py::FIXTURES[x]'（旧）或 'fixtures/x.json'（新）或 'x.json'。"""
    ff = case.get("fixture_file")
    if not isinstance(ff, str) or not ff.strip():
        return ""
    if "::" in ff:
        return ""  # 旧式引用，无独立文件
    name = os.path.basename(ff)
    return name


def _check_rubric(case: dict, finding: dict | None, findings: list[dict]) -> list[str]:
    """执行 must_evidence / handoff_to / must_conclude / forbidden 断言（Review P2-1）。

    返回失败原因列表（空表示全过）。结构化字段断言优先于文本匹配。
    """
    fails: list[str] = []
    if finding is None:
        finding = {}

    # must_evidence：只匹配 evidence_fields（或结构化字段路径/非空），不得用 str(finding)
    # （Review P2-1：str(finding) 会把 missing_evidence 里的串误当证据通过）。
    evs = finding.get("evidence_fields") or []
    for ev in case.get("must_evidence") or []:
        if ev.startswith("handoff="):
            if finding.get("handoff") != ev.split("=", 1)[1]:
                fails.append(
                    f"must_evidence handoff 不符：{ev}（实际 {finding.get('handoff')}）"
                )
        elif ev.startswith("field="):
            # 结构化字段路径：field=saturated_devices 要求该字段非空
            key = ev.split("=", 1)[1]
            if not finding.get(key):
                fails.append(f"must_evidence 字段为空：{key}")
        elif not any(ev in ef for ef in evs):
            # 仅在 evidence_fields 内做子串匹配（不得用 str(finding)，避免 missing_evidence 误匹配）
            fails.append(f"must_evidence 缺失（evidence_fields）：{ev}")

    # handoff_to：结构字段断言
    ht = case.get("handoff_to")
    if ht and finding.get("handoff") != ht:
        fails.append(f"handoff_to 不符：期望 {ht}，实际 {finding.get('handoff')}")

    # must_conclude：summary/handoff 归一化后应包含关键词
    if finding:
        text = " ".join(
            [str(finding.get("summary", "")), str(finding.get("handoff", ""))]
        )
    else:
        text = " ".join(
            f"{item.get('summary', '')} {item.get('handoff', '')}"
            for item in findings
            if isinstance(item, dict)
        )
    for kw in case.get("must_conclude") or []:
        # 关键词允许同义归一（去除分隔符后子串匹配）
        norm = text.replace("/", "").replace("_", "").replace(" ", "")
        if kw.replace("/", "").replace("_", "").replace(" ", "") not in norm:
            fails.append(f"must_conclude 缺关键词：{kw}")

    # forbidden：summary 不得包含
    summary = (
        str(finding.get("summary", ""))
        if finding
        else " ".join(
            str(item.get("summary", "")) for item in findings if isinstance(item, dict)
        )
    )
    for kw in case.get("forbidden") or []:
        if kw in summary:
            fails.append(f"forbidden 命中：{kw}")

    rule_map = {
        str(item.get("rule_id")): item for item in findings if isinstance(item, dict)
    }
    for rule in case.get("must_have_rules") or []:
        if rule not in rule_map:
            fails.append(f"must_have_rules 缺失：{rule}")
    for rule in case.get("must_not_have_positive_rules") or []:
        item = rule_map.get(rule)
        if item and _is_positive_problem(item):
            fails.append(
                f"must_not_have_positive_rules 命中：{rule}({item.get('confidence')}/{item.get('severity')})"
            )
    missing_text = " ".join(
        str(part)
        for item in findings
        if isinstance(item, dict)
        for part in (item.get("missing_evidence") or [])
    )
    for keyword in case.get("must_missing_evidence") or []:
        if keyword not in missing_text:
            fails.append(f"must_missing_evidence 缺失：{keyword}")
    return fails


def evaluate_case(case: dict) -> tuple[bool, str]:
    """跑单个案例，返回 (pass, detail)。Review P2-1：执行 rubric 断言。"""
    cid = case["id"]
    fixture_name = _resolve_fixture_path(case)
    if not fixture_name or not os.path.exists(
        os.path.join(EVAL_DIR, "fixtures", fixture_name)
    ):
        return False, f"fixture 缺失：{case.get('fixture_file')}"
    doc = _load_fixture(fixture_name)
    if not isinstance(doc, dict):
        return False, f"fixture 顶层不是对象：{type(doc).__name__}"
    profile = doc.pop("_profile", None)
    expected_rule = str(case.get("expected_rule", "")).strip()
    expected_conf = case.get("expected_confidence")

    try:
        res = a.analyze_all(doc, profile)
    except Exception as exc:  # noqa: BLE001
        return False, f"analyzer 抛异常: {type(exc).__name__}: {exc}"
    if expected_rule == "reject" or cid == "miss-schema-major":
        return ("error" in res), (
            "未知 schema major 被拒绝" if "error" in res else "未被拒绝"
        )

    if "error" in res:
        return False, f"analyzer 返回 error: {res['error']}"

    findings = res["findings"]
    result_rubric_fails: list[str] = []
    validation_text = " ".join(str(item) for item in res.get("validation_errors", []))
    for keyword in case.get("must_validation_errors") or []:
        if keyword not in validation_text:
            result_rubric_fails.append(f"must_validation_errors 缺失：{keyword}")
    profile_validation_text = " ".join(
        str(item) for item in res.get("profile_validation_errors", [])
    )
    for keyword in case.get("must_profile_validation_errors") or []:
        if keyword not in profile_validation_text:
            result_rubric_fails.append(
                f"must_profile_validation_errors 缺失：{keyword}"
            )
    if expected_rule in ("", "none", "None"):
        hits = [
            f for f in findings if _is_positive_problem(f) and f["rule_id"] != "R000"
        ]
        ok = not hits
        detail = (
            "无正向问题命中（符合预期）"
            if ok
            else f"意外命中: {[(f['rule_id'], f['confidence']) for f in hits]}"
        )
        rubric_fails = _check_rubric(case, None, findings) + result_rubric_fails
        if rubric_fails:
            return False, f"{detail}；rubric: {'; '.join(rubric_fails)}"
        return ok, detail + (
            " +rubric"
            if any(
                case.get(key)
                for key in (
                    "must_conclude",
                    "forbidden",
                    "must_have_rules",
                    "must_not_have_positive_rules",
                    "must_missing_evidence",
                )
            )
            else ""
        )

    f = next((x for x in findings if x["rule_id"] == expected_rule), None)
    if f is None:
        return False, f"未命中 {expected_rule}"
    if f["confidence"] == "none":
        return False, f"{expected_rule} confidence=none（无证据）"
    if expected_conf and f["confidence"] != expected_conf:
        return (
            False,
            f"{expected_rule} confidence={f['confidence']}（期望={expected_conf}）",
        )
    maximum_conf = case.get("maximum_confidence")
    if maximum_conf and _ORDER.get(f["confidence"], 0) > _ORDER.get(
        str(maximum_conf), 0
    ):
        return (
            False,
            f"{expected_rule} confidence={f['confidence']}（期望<={maximum_conf}）",
        )
    rubric_fails = _check_rubric(case, f, findings) + result_rubric_fails
    if rubric_fails:
        return (
            False,
            f"{expected_rule} confidence={f['confidence']}；rubric: {'; '.join(rubric_fails)}",
        )
    return True, f"{expected_rule} confidence={f['confidence']}" + (
        " +rubric"
        if (
            case.get("must_evidence")
            or case.get("must_conclude")
            or case.get("forbidden")
        )
        else ""
    )


def run(report_path: str | None = None, verbose: bool = True) -> int:
    """运行确定性 eval。

    Review P2-2：默认不写任何文件（避免 pytest 污染源码工作树）。
    仅当显式传入 report_path（CLI）时才写 JSON 报告。
    返回退出码（0=全过，1=有失败/配置错误）。
    """
    cases = _load_cases()
    machine = _machine_cases(cases)
    results: list[tuple[str, bool, str]] = []
    config_errors: list[str] = []
    if not machine:
        config_errors.append(
            "rootcause_cases/missing_data_cases 至少需要一个机器可执行案例，禁止 0/0 通过"
        )

    seen_ids: set[str] = set()
    for index, (section, case) in enumerate(machine):
        if not isinstance(case, dict):
            cid = f"{section}[{index}]"
            detail = "case 必须是 object"
            config_errors.append(f"{cid}: {detail}")
            results.append((cid, False, detail))
            continue
        cid = str(case.get("id") or f"{section}[{index}]")
        case_errors: list[str] = []
        if not case.get("id"):
            case_errors.append("缺 id")
        elif cid in seen_ids:
            case_errors.append("id 重复")
        seen_ids.add(cid)
        if (
            not isinstance(case.get("fixture_file"), str)
            or not case.get("fixture_file", "").strip()
        ):
            case_errors.append("缺 fixture_file")
        if "expected_rule" not in case:
            case_errors.append("缺 expected_rule")
        if case_errors:
            detail = "; ".join(case_errors)
            config_errors.extend(f"{cid}: {error}" for error in case_errors)
            results.append((cid, False, f"配置错误：{detail}"))
            continue
        ok, detail = evaluate_case(case)
        results.append((cid, ok, detail))

    npass = sum(1 for _, ok, _ in results if ok)
    total = len(results)

    if verbose:
        print("=" * 72)
        print("mindstudio-storage-analysis 确定性 eval 报告（数据源：cases.yaml）")
        print("=" * 72)
        width = max((len(cid) for cid, _, _ in results), default=20)
        for cid, ok, detail in results:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {cid:<{width}}  {detail}")
        if config_errors:
            print("-" * 72)
            print("  配置错误：")
            for e in config_errors:
                print(f"    - {e}")
        print("-" * 72)
        print(
            f"  合计: {npass}/{total} 通过；机器案例 {total} 个（rootcause+missing 全量配置校验）"
        )
        trigger = len(cases.get("trigger_cases", []) or [])
        safety = len(cases.get("safety_cases", []) or [])
        live = len(cases.get("environment_cases", []) or [])
        print(
            f"  人工流程案例：trigger={trigger}，safety={safety}（需 msAgent 运行时 + 固定模型温度）"
        )
        print(f"  环境案例：live={live}（运行 python3 evals/run_live_eval.py）")
        print("=" * 72)

    # 仅在显式指定 report_path 时写文件（默认无副作用）。
    # Review 第六轮 P2-1：显式交付物写入失败必须非零退出 + stderr 诊断（不得吞掉报成功）。
    write_failed = False
    if report_path:
        report = {
            "total": total,
            "passed": npass,
            "failed": total - npass,
            "config_errors": config_errors,
            "manual_trigger": len(cases.get("trigger_cases", []) or []),
            "manual_safety": len(cases.get("safety_cases", []) or []),
            "live_environment": len(cases.get("environment_cases", []) or []),
            "cases": [
                {"id": cid, "pass": ok, "detail": detail} for cid, ok, detail in results
            ],
        }
        # 原子写入：先写 tmp 再 replace，避免半写 JSON
        try:
            data = json.dumps(report, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            print(f"错误: eval 报告序列化失败: {exc}", file=sys.stderr)
            write_failed = True
            data = None
        if data is not None:
            tmp = _temp_name(report_path)
            lock = _acquire_report_lock(report_path)
            if lock is None:
                print(f"错误: 等待 eval 报告写锁超时: {report_path}", file=sys.stderr)
                write_failed = True
                data = None
        if data is not None:
            try:
                with open(tmp, "w", encoding="utf-8") as jf:
                    jf.write(data)
                os.replace(tmp, report_path)
                if verbose:
                    print(f"  JSON 报告：{report_path}")
            except OSError as exc:
                print(f"错误: 无法写入 eval 报告 {report_path}: {exc}", file=sys.stderr)
                write_failed = True
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
            finally:
                _release_report_lock(lock)
    base_rc = 0 if (npass == total and not config_errors) else 1
    return 2 if (write_failed and base_rc == 0) else base_rc


def _temp_name(path: str) -> str:
    """在目标同目录生成唯一临时文件名，避免并发 eval 相互覆盖。"""
    import uuid

    directory, base = os.path.split(path)
    return os.path.join(
        directory or ".", f".{base}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )


def _acquire_report_lock(path: str, timeout: float = 10.0) -> str | None:
    """跨线程/进程串行替换同一报告，兼容 Windows 的 replace 共享限制。"""
    import time

    lock = path + ".lock"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(f"{os.getpid()}\n")
            return lock
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 60:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            time.sleep(0.02)
        except OSError:
            # Windows 上另一个线程刚创建/删除 lock 时，os.open 可能报告
            # PermissionError/WinError 5 而不是 FileExistsError；视为短暂竞争重试。
            time.sleep(0.02)
    return None


def _release_report_lock(lock: str | None) -> None:
    if not lock:
        return
    try:
        os.remove(lock)
    except OSError:
        pass


class _EvalAsTest(unittest.TestCase):
    def test_all_machine_cases(self):  # noqa: D401
        # verbose=False 避免污染 pytest 输出；不传 report_path 避免写源码目录（Review P2-2）
        rc = run(verbose=False)
        self.assertEqual(rc, 0, "确定性 eval 存在失败/配置错误，见上文详情")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="mindstudio-storage-analysis 确定性 eval")
    ap.add_argument(
        "--report",
        default=None,
        help="JSON 报告输出路径（默认不写；显式指定才写，避免污染源码工作树）",
    )
    args = ap.parse_args()
    sys.exit(run(report_path=(args.report or None)))
