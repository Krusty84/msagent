#!/usr/bin/env python3
"""Render storage-analysis artifacts and Agent commentary as one offline HTML report."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any


_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_JSON_DEPTH = 80
_MAX_JSON_NODES = 500_000
_MAX_TABLE_ROWS = 50
_MAX_TEXT = 8_000

_PROVIDERS = (
    "mounts_provider",
    "block_devices",
    "iostat",
    "pidstat",
    "process_io_map",
    "memory",
    "df",
    "glusterfs",
    "nfs",
)

_RULE_NAMES = {
    "R000": "证据是否完整",
    "R100": "本地存储是否承压",
    "R200": "网络存储是否异常",
    "R300": "远程文件与小文件访问",
    "R400": "多个进程是否争抢 IO",
    "R500": "存储问题是否影响 NPU",
}

_STATUS_LABELS = {
    "ok": "已采集",
    "missing": "缺失",
    "permission_denied": "权限不足",
    "command_failed": "命令失败",
    "parse_failed": "解析失败",
    "empty": "结果为空",
    "unsupported": "当前环境不支持",
    "partial": "部分可用",
    "unknown": "未知",
}

_SEVERITY_LABELS = {
    "high": "高优先级",
    "medium": "中优先级",
    "low": "低优先级",
    "info": "信息",
}

_CONFIDENCE_LABELS = {
    "high": "高置信度",
    "medium": "中置信度",
    "low": "低置信度",
    "none": "尚无置信度",
}

_MSPROF_LIMITATIONS = {
    "op_summary is aggregate data and does not provide a device timeline":
        "op_summary 是汇总数据，不包含设备时间线",
    "exported task gaps are not device idle time":
        "导出任务之间的间隔不等于 NPU 空闲时间",
    "per-task cycle ratios are not aggregated into a workload mte2_ratio":
        "不能把逐任务 cycle ratio 直接合成为整个任务的 MTE2 比例",
}

_ICONS = {
    "overview": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 19V9m6 10V5m6 14v-7m4 7H2"/><path d="m3 7 6-4 6 5 6-5"/></svg>',
    "agent": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3a6 6 0 0 0-6 6v2a4 4 0 0 0-2 3.5A3.5 3.5 0 0 0 7.5 18H9l3 3 3-3h1.5a3.5 3.5 0 0 0 3.5-3.5A4 4 0 0 0 18 11V9a6 6 0 0 0-6-6Z"/><path d="M9 11h.01M15 11h.01M9 15c1.7 1.2 4.3 1.2 6 0"/></svg>',
    "findings": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 3h14v18H5z"/><path d="M8 7h8M8 11h8M8 15h5"/><path d="m15 18 2 2 4-5"/></svg>',
    "metrics": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 12 17 7M7 16h10"/></svg>',
    "target": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>',
    "quality": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m12 3 8 4v5c0 5-3.4 8-8 9-4.6-1-8-4-8-9V7l8-4Z"/><path d="m8.5 12 2.2 2.2 4.8-5"/></svg>',
}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"无法读取 {label}: {exc}") from exc
    if size > _MAX_JSON_BYTES:
        raise ValueError(f"{label} 超过 {_MAX_JSON_BYTES // (1024 * 1024)} MiB 限制")
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream, parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} 顶层必须是 JSON 对象")
    _check_complexity(value, label)
    return value


def _check_complexity(value: Any, label: str) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise ValueError(f"{label} JSON 节点过多")
        if depth > _MAX_JSON_DEPTH:
            raise ValueError(f"{label} JSON 嵌套超过 {_MAX_JSON_DEPTH} 层")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _atomic_write(path: Path, text: str) -> None:
    if not path.parent.exists():
        raise ValueError(f"输出目录不存在: {path.parent}")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _h(value: Any, *, limit: int = _MAX_TEXT) -> str:
    if value is None or value == "":
        return "—"
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    return html.escape(text, quote=True)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    number = _number(value)
    if number is None:
        return "—"
    if abs(number) >= 1000:
        rendered = f"{number:,.{digits}f}"
    else:
        rendered = f"{number:.{digits}f}"
    return f"{rendered}{suffix}"


def _bounded_text(value: Any, field: str, maximum: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"agent_report.{field} 必须是字符串")
    if len(value) > maximum:
        raise ValueError(f"agent_report.{field} 超过 {maximum} 字符")
    return value.strip()


def _normalize_agent_report(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    summary = _bounded_text(value.get("summary"), "summary")
    limitations_value = value.get("limitations", [])
    if not isinstance(limitations_value, list) or len(limitations_value) > 30:
        raise ValueError("agent_report.limitations 必须是最多 30 项的字符串列表")
    limitations = [
        _bounded_text(item, f"limitations[{index}]", 1_000)
        for index, item in enumerate(limitations_value)
    ]
    recommendations_value = value.get("recommendations", [])
    if not isinstance(recommendations_value, list) or len(recommendations_value) > 30:
        raise ValueError("agent_report.recommendations 必须是最多 30 项的列表")
    recommendations: list[dict[str, Any]] = []
    for index, item in enumerate(recommendations_value):
        if isinstance(item, str):
            recommendations.append(
                {
                    "priority": "medium",
                    "title": f"建议 {index + 1}",
                    "detail": _bounded_text(item, f"recommendations[{index}]", 3_000),
                    "source_rule_ids": [],
                    "requires_confirmation": False,
                }
            )
            continue
        if not isinstance(item, dict):
            raise ValueError(f"agent_report.recommendations[{index}] 必须是对象或字符串")
        priority = str(item.get("priority", "medium")).lower()
        if priority not in {"high", "medium", "low", "info"}:
            raise ValueError(
                f"agent_report.recommendations[{index}].priority 非法: {priority}"
            )
        rule_ids = item.get("source_rule_ids", [])
        if not isinstance(rule_ids, list) or len(rule_ids) > 6 or not all(
            isinstance(rule_id, str) and rule_id in _RULE_NAMES for rule_id in rule_ids
        ):
            raise ValueError(
                f"agent_report.recommendations[{index}].source_rule_ids 必须是 R000-R500 列表"
            )
        confirmation = item.get("requires_confirmation", False)
        if not isinstance(confirmation, bool):
            raise ValueError(
                f"agent_report.recommendations[{index}].requires_confirmation 必须是布尔值"
            )
        recommendations.append(
            {
                "priority": priority,
                "title": _bounded_text(
                    item.get("title") or f"建议 {index + 1}",
                    f"recommendations[{index}].title",
                    200,
                ),
                "detail": _bounded_text(
                    item.get("detail"), f"recommendations[{index}].detail", 3_000
                ),
                "source_rule_ids": rule_ids,
                "requires_confirmation": confirmation,
            }
        )
    return {
        "summary": summary,
        "recommendations": recommendations,
        "limitations": limitations,
    }


def _artifact(path: Path, label: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "label": label,
        "name": path.name,
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def _icon(name: str) -> str:
    return _ICONS[name]


def _section(
    section_id: str, title: str, subtitle: str, icon: str, content: str
) -> str:
    return f"""
    <section class="report-section" id="{_h(section_id)}">
      <div class="section-heading">
        <div class="section-title-wrap">
          <div class="section-icon" aria-hidden="true">{_icon(icon)}</div>
          <div><h2>{_h(title)}</h2><p class="section-subtitle">{_h(subtitle)}</p></div>
        </div>
      </div>
      {content}
    </section>"""


def _badge(value: str, kind: str) -> str:
    css_value = value if value.replace("_", "").isalnum() else "unknown"
    if kind == "severity":
        label = _SEVERITY_LABELS.get(value, value)
    elif kind == "confidence":
        label = _CONFIDENCE_LABELS.get(value, value)
    else:
        label = _STATUS_LABELS.get(value, value)
    return f'<span class="badge {kind}-{_h(css_value)}">{_h(label)}</span>'


def _tags(values: Any, empty: str = "无") -> str:
    items = [item for item in _list(values) if isinstance(item, (str, int, float))]
    if not items:
        return f'<span class="data-tag">{_h(empty)}</span>'
    return "".join(f'<span class="data-tag">{_h(item)}</span>' for item in items[:20])


def _bullet_list(values: Any, empty: str = "无") -> str:
    items = [item for item in _list(values) if isinstance(item, (str, int, float))]
    if not items:
        return f"<ul><li>{_h(empty)}</li></ul>"
    return "<ul>" + "".join(f"<li>{_h(item)}</li>" for item in items[:20]) + "</ul>"


def _render_pipeline(
    targets: dict[str, Any] | None,
    msprof: dict[str, Any] | None,
    agent: dict[str, Any] | None,
) -> str:
    steps = [
        ("01 / DISCOVER", "discover_io_target.py", "寻找 PID 和数据路径", targets is not None),
        ("02 / COLLECT", "collect_io_snapshot.py", "采集 Host IO 事实", True),
        ("03 / ANALYZE", "analyze_io_snapshot.py", "执行 R000—R500", True),
        ("04 / AUXILIARY", "summarize_msprof.py", "摘要 NPU 侧线索", msprof is not None),
        ("05 / AGENT", "总结与建议", "解释规则结果", agent is not None),
        ("06 / REPORT", "render_io_report.py", "生成本 HTML", True),
    ]
    cards = []
    for step, title, description, present in steps:
        state_class = "state-ok" if present else "state-skipped"
        state_label = "已纳入报告" if present else "本次未提供 / 已跳过"
        cards.append(
            f"""<article class="pipeline-card">
              <div class="pipeline-step">{_h(step)}</div>
              <h3>{_h(title)}</h3><p>{_h(description)}</p>
              <span class="pipeline-state {state_class}">{state_label}</span>
            </article>"""
        )
    return '<div class="pipeline">' + "".join(cards) + "</div>"


def _structure_agent_summary(summary: str) -> dict[str, Any]:
    """Split the recommended Chinese summary shape without changing its wording."""
    result: dict[str, Any] = {
        "context": "",
        "conclusion": summary,
        "evidence": [],
        "synthesis": "",
    }
    conclusion_match = re.search(r"结论[：:]\s*", summary)
    evidence_match = re.search(r"证据[：:]\s*", summary)
    if not conclusion_match or not evidence_match or evidence_match.start() <= conclusion_match.end():
        return result

    result["context"] = summary[: conclusion_match.start()].strip()
    result["conclusion"] = summary[
        conclusion_match.end() : evidence_match.start()
    ].strip()
    evidence_text = summary[evidence_match.end() :].strip()
    numbered = re.split(r"(?:^|[；;]\s*)\d+[)）]\s*", evidence_text)
    evidence = [item.strip(" ；;") for item in numbered if item.strip(" ；;")]
    if evidence:
        last = evidence[-1]
        synthesis_match = re.search(r"(?:因此|综合判断[：:])", last)
        if synthesis_match and synthesis_match.start() > 0:
            evidence[-1] = last[: synthesis_match.start()].strip(" ；;")
            result["synthesis"] = last[synthesis_match.start() :].strip()
        result["evidence"] = [item for item in evidence if item]
    return result


def _render_agent(agent: dict[str, Any] | None) -> str:
    if agent is None:
        return '<div class="empty">本次没有提供 <code>agent_report.json</code>。页面仍完整展示确定性规则结论；Agent 可在分析后补充自然语言总结与建议并重新生成。</div>'
    summary = str(agent.get("summary") or "Agent 未填写总结。")
    structured = _structure_agent_summary(summary)
    limitations = agent.get("limitations") or []
    limit_html = _bullet_list(limitations, "Agent 未补充额外限制")
    recommendations = []
    priority_labels = {"high": "P0", "medium": "P1", "low": "P2", "info": "参考"}
    for index, item in enumerate(agent.get("recommendations", []), start=1):
        rule_ids = item.get("source_rule_ids") or []
        tags = _tags(rule_ids, "未绑定规则")
        confirmation = (
            '<span class="badge severity-medium">执行前必须确认</span>'
            if item.get("requires_confirmation")
            else '<span class="badge severity-info">只读或无需系统变更</span>'
        )
        recommendations.append(
            f"""<article class="recommendation priority-{_h(item['priority'])}">
              <div class="recommendation-index"><span>{index:02d}</span><strong>{_h(priority_labels[item['priority']])}</strong></div>
              <div class="recommendation-content">
                <h3>{_h(item['title'])}</h3><p>{_h(item['detail'])}</p>
                <div class="badge-row recommendation-meta">
                  {confirmation}{tags}
                </div>
              </div>
            </article>"""
        )
    recommendation_html = (
        '<div class="recommendation-list">' + "".join(recommendations) + "</div>"
        if recommendations
        else '<div class="empty" style="margin-top:16px">Agent 没有补充额外建议；可直接查看每条规则自带的下一步检查。</div>'
    )
    context_html = (
        f'<p class="agent-context">{_h(structured["context"])}</p>'
        if structured["context"]
        else ""
    )
    evidence_html = ""
    if structured["evidence"]:
        evidence_html = (
            '<div class="agent-evidence"><h4>判断依据</h4><ol>'
            + "".join(f'<li>{_h(item)}</li>' for item in structured["evidence"])
            + "</ol></div>"
        )
    synthesis_html = (
        f'<div class="agent-synthesis"><strong>综合判断</strong><p>{_h(structured["synthesis"])}</p></div>'
        if structured["synthesis"]
        else ""
    )
    return f"""
      <div class="agent-focus">
        <article class="agent-reading">
          <div class="agent-kicker">核心结论</div>
          <h3>{_h(structured['conclusion'])}</h3>
          {context_html}{evidence_html}{synthesis_html}
        </article>
        <aside class="agent-boundaries">
          <div class="boundary-label">证据边界</div>
          <p>这一部分由 Agent 根据 Findings 组织，确定性事实以 R000—R500 为准。</p>
          <h4>当前限制</h4>{limit_html}
        </aside>
      </div>
      <div class="recommendation-heading"><div><span>下一步</span><h3>建议与验证动作</h3></div><p>按优先级阅读，涉及采集或系统变化的动作仍需确认。</p></div>
      {recommendation_html}"""


def _render_findings(findings: dict[str, Any]) -> str:
    cards: list[str] = []
    for finding in _list(findings.get("findings")):
        if not isinstance(finding, dict):
            continue
        rule_id = str(finding.get("rule_id") or "UNKNOWN")
        severity = str(finding.get("severity") or "info").lower()
        confidence = str(finding.get("confidence") or "none").lower()
        evidence = _tags(finding.get("evidence_fields"), "暂无直接证据字段")
        missing = _bullet_list(finding.get("missing_evidence"), "没有列出缺失证据")
        checks = _bullet_list(
            finding.get("recommended_next_checks"), "没有额外检查建议"
        )
        cards.append(
            f"""<article class="finding-card severity-{_h(severity)}">
              <header class="finding-head">
                <div class="rule-title"><span class="rule-id">{_h(rule_id)}</span><span class="rule-name">{_h(_RULE_NAMES.get(rule_id, '规则结论'))}</span></div>
                <div class="badge-row">{_badge(severity, 'severity')}{_badge(confidence, 'confidence')}</div>
              </header>
              <div class="finding-body">
                <p class="finding-summary">{_h(finding.get('summary'))}</p>
                <div class="finding-columns">
                  <div class="mini-block"><h4>证据字段</h4><div class="tag-list">{evidence}</div></div>
                  <div class="mini-block"><h4>缺失证据</h4>{missing}</div>
                  <div class="mini-block" style="grid-column:1 / -1"><h4>规则建议的下一步检查</h4>{checks}</div>
                </div>
              </div>
            </article>"""
        )
    return (
        '<div class="finding-grid">' + "".join(cards) + "</div>"
        if cards
        else '<div class="empty">findings.json 中没有可展示的规则结果。</div>'
    )


def _render_disk_table(snapshot: dict[str, Any]) -> str:
    parsed = _dict(_dict(snapshot.get("iostat")).get("parsed"))
    disks = _dict(parsed.get("disks"))
    rows = []
    for name, metrics_value in sorted(disks.items())[:_MAX_TABLE_ROWS]:
        metrics = _dict(metrics_value)
        util = _number(metrics.get("util_percent"))
        util_width = min(max(util or 0.0, 0.0), 100.0)
        read_mib = (_number(metrics.get("rkB_per_s")) or 0.0) / 1024.0
        write_mib = (_number(metrics.get("wkB_per_s")) or 0.0) / 1024.0
        read_iops = _number(metrics.get("r_per_s")) or 0.0
        write_iops = _number(metrics.get("w_per_s")) or 0.0
        await_value = metrics.get("await", metrics.get("r_await_ms"))
        rows.append(
            f"""<tr>
              <td><strong class="mono">{_h(name)}</strong><br>{_h(metrics.get('device_type'))}</td>
              <td><strong>{_fmt(util, 1, '%')}</strong><div class="bar-track"><div class="bar-fill" style="--bar-width:{util_width:.2f}%"></div></div></td>
              <td>{_fmt(read_mib, 2)} / {_fmt(write_mib, 2)}</td>
              <td>{_fmt(read_iops + write_iops, 1)}</td>
              <td>{_fmt(await_value, 2, ' ms')}</td>
              <td>{_fmt(metrics.get('avgqu_sz'), 2)}</td>
              <td>{_h(metrics.get('sample_count'))}</td>
            </tr>"""
        )
    body = "".join(rows) or '<tr><td colspan="7">没有结构化 iostat 设备指标。</td></tr>'
    return f"""<div class="panel wide"><div class="panel-title"><h3>本地磁盘</h3><span>IOSTAT / R100</span></div><div class="table-wrap"><table>
      <thead><tr><th>设备</th><th>忙碌程度</th><th>读取 / 写入 MiB/s</th><th>总 IOPS</th><th>等待</th><th>队列</th><th>样本</th></tr></thead><tbody>{body}</tbody>
    </table></div></div>"""


def _render_nfs_table(snapshot: dict[str, Any]) -> str:
    parsed = _dict(_dict(snapshot.get("nfs")).get("parsed"))
    rows = []
    for metric_value in _list(parsed.get("mount_metrics"))[:_MAX_TABLE_ROWS]:
        metric = _dict(metric_value)
        rows.append(
            f"""<tr>
              <td><strong>{_h(metric.get('mount_point'))}</strong><br><span class="mono">{_h(metric.get('source'))}</span></td>
              <td>{_h(metric.get('fstype'))}</td>
              <td>{_fmt(metric.get('ops'), 0)}</td>
              <td>{_fmt(metric.get('avg_rtt_ms'), 2, ' ms')}</td>
              <td>{_fmt(metric.get('avg_execute_ms'), 2, ' ms')}</td>
              <td>{_fmt(metric.get('retrans'), 0)} / {_fmt((_number(metric.get('retrans_ratio')) or 0) * 100, 2, '%')}</td>
              <td>{_fmt(metric.get('major_timeouts'), 0)}</td>
            </tr>"""
        )
    body = "".join(rows) or '<tr><td colspan="7">本次没有可展示的 NFS 窗口指标。</td></tr>'
    return f"""<div class="panel"><div class="panel-title"><h3>NFS 网络存储</h3><span>MOUNTSTATS / R200—R300</span></div><div class="table-wrap"><table>
      <thead><tr><th>挂载</th><th>类型</th><th>操作数</th><th>RTT</th><th>完成耗时</th><th>重传</th><th>严重超时</th></tr></thead><tbody>{body}</tbody>
    </table></div></div>"""


def _render_glusterfs_table(snapshot: dict[str, Any]) -> str:
    parsed = _dict(_dict(snapshot.get("glusterfs")).get("parsed"))
    rows = []
    for metric_value in _list(parsed.get("mount_metrics"))[:_MAX_TABLE_ROWS]:
        metric = _dict(metric_value)
        process_io = _dict(metric.get("process_io"))
        rchar = _number(process_io.get("rchar"))
        read_bytes = _number(process_io.get("read_bytes"))
        syscr = _number(process_io.get("syscr"))
        avg_read = _number(process_io.get("avg_rchar_per_syscall"))
        if avg_read is None and rchar is not None and syscr and syscr > 0:
            avg_read = rchar / syscr
        rows.append(
            f"""<tr>
              <td><strong>{_h(metric.get('mount_point'))}</strong><br><span class="mono">{_h(metric.get('source'))}</span></td>
              <td>{'是' if metric.get('target_scoped') is True else '否'}</td>
              <td>{_h(process_io.get('stable_pid_count'))}</td>
              <td>{_fmt((rchar or 0) / (1024 * 1024), 2, ' MiB')}</td>
              <td>{_fmt((read_bytes or 0) / (1024 * 1024), 2, ' MiB')}</td>
              <td>{_fmt(syscr, 0)}</td>
              <td>{_fmt(avg_read, 1, ' B')}</td>
              <td>{'可用' if metric.get('client_latency_available') is True else '未提供'}</td>
            </tr>"""
        )
    body = (
        "".join(rows)
        or '<tr><td colspan="8">本次没有可展示的 GlusterFS 目标活动指标。</td></tr>'
    )
    return f"""<div class="panel wide"><div class="panel-title"><h3>GlusterFS FUSE 主网络存储</h3><span>目标进程树活动 / R200—R300</span></div><div class="table-wrap"><table>
      <thead><tr><th>挂载</th><th>目标作用域</th><th>稳定 PID</th><th>逻辑读取</th><th>块层读取</th><th>读调用</th><th>平均每调用</th><th>客户端延迟</th></tr></thead><tbody>{body}</tbody>
    </table></div><div class="panel-body"><p class="muted">进程 IO 只证明目标活动与小读取候选，不等同于 Gluster client/brick 延迟。</p></div></div>"""


def _render_process_table(snapshot: dict[str, Any]) -> str:
    parsed = _dict(_dict(snapshot.get("pidstat")).get("parsed"))
    reports = parsed.get("reports")
    rows = []
    for process_value in _list(parsed.get("processes"))[:_MAX_TABLE_ROWS]:
        process = _dict(process_value)
        rows.append(
            f"""<tr>
              <td><strong class="mono">{_h(process.get('pid'))}</strong></td>
              <td>{_h(process.get('command'))}</td>
              <td>{_fmt(process.get('kbr_per_s'), 1, ' KiB/s')}</td>
              <td>{_fmt(process.get('kbw_per_s'), 1, ' KiB/s')}</td>
              <td>{_h(process.get('active_sample_count'))} / {_h(process.get('sample_count'))}</td>
            </tr>"""
        )
    body = "".join(rows) or '<tr><td colspan="5">本次没有可展示的进程 IO 指标。</td></tr>'
    return f"""<div class="panel"><div class="panel-title"><h3>进程 IO</h3><span>PIDSTAT / { _h(reports) } REPORTS</span></div><div class="table-wrap"><table>
      <thead><tr><th>PID</th><th>程序</th><th>读取</th><th>写入</th><th>活跃样本</th></tr></thead><tbody>{body}</tbody>
    </table></div></div>"""


def _render_msprof(msprof: dict[str, Any] | None) -> str:
    if msprof is None:
        return '<div class="empty">本次未提供 msprof op_summary 诊断摘要。R500 仍以正式 profiler 时间线证据为准。</div>'
    proxies = _dict(msprof.get("diagnostic_proxies"))
    provenance = _dict(msprof.get("provenance"))
    ratios = _dict(proxies.get("mte2_ratio_by_column"))
    ratio_rows = []
    for column, stats_value in sorted(ratios.items())[:20]:
        stats = _dict(stats_value)
        ratio_rows.append(
            f"<tr><td class=\"mono\">{_h(column)}</td><td>{_h(stats.get('sample_count'))}</td><td>{_fmt(stats.get('min'), 4)}</td><td>{_fmt(stats.get('arithmetic_mean'), 4)}</td><td>{_fmt(stats.get('max'), 4)}</td></tr>"
        )
    ratio_body = "".join(ratio_rows) or '<tr><td colspan="5">没有 MTE2 ratio 列。</td></tr>'
    limitations = [
        _MSPROF_LIMITATIONS.get(str(item), str(item))
        for item in _list(provenance.get("limitations"))
    ]
    return f"""<div class="data-grid">
      <div class="metric-card"><div class="metric-label">TASK GAP PROXY / 非正式空闲证据</div><div class="metric-value">{_fmt(proxies.get('op_summary_task_gap_proxy_percent'), 2, '%')}</div><div class="metric-foot">设备 {_h(provenance.get('device_id'))} · {_h(provenance.get('task_count'))} 个任务</div></div>
      <div class="mini-block"><h4>使用边界</h4>{_bullet_list(limitations, '摘要器已标记为非认证数据')}</div>
      <div class="panel wide"><div class="panel-title"><h3>MTE2 侧面指标</h3><span>不能单独证明 Host IO 导致 NPU 空闲</span></div><div class="table-wrap"><table><thead><tr><th>列</th><th>样本</th><th>最小</th><th>平均</th><th>最大</th></tr></thead><tbody>{ratio_body}</tbody></table></div></div>
    </div>"""


def _render_targets(targets: dict[str, Any] | None) -> str:
    if targets is None:
        return '<div class="empty">目标由用户直接提供，或本次没有保存 target_candidates.json。</div>'
    recommendation = _dict(targets.get("recommendation"))
    confirmation = bool(recommendation.get("requires_confirmation", True))
    confidence = str(recommendation.get("confidence") or "none")
    confidence_label = _CONFIDENCE_LABELS.get(confidence, confidence)
    reasons = _tags(recommendation.get("reasons"), "没有推荐原因")
    if confirmation:
        return f"""<div class="panel"><div class="panel-body">
          <div class="badge-row" style="justify-content:flex-start"><span class="badge severity-medium">等待用户确认训练目标</span><span class="badge confidence-{_h(confidence)}">{_h(confidence_label)}</span></div>
          <p style="margin:14px 0 0">发现了得分接近或路径不明确的候选。采集前应由 Agent 在交互中请用户选择；最终报告不展开候选进程和分数。</p>
          <div class="tag-list" style="margin-top:12px">{reasons}</div>
        </div></div>"""

    pid = recommendation.get("pid")
    path = recommendation.get("path")
    selected = next(
        (
            _dict(item)
            for item in _list(targets.get("process_candidates"))
            if _dict(item).get("pid") == pid
        ),
        {},
    )
    selected_path = next(
        (
            _dict(item)
            for item in _list(selected.get("path_candidates"))
            if _dict(item).get("path") == path
        ),
        {},
    )
    mount = _dict(selected_path.get("mount"))
    filesystem = mount.get("fstype") or "未知"
    program = selected.get("command") or "未知程序"
    cmdline = selected.get("cmdline")
    command_html = (
        f'<p class="mono" style="margin:12px 0 0">{_h(cmdline, limit=1200)}</p>'
        if cmdline
        else ""
    )
    return f"""<div class="panel"><div class="panel-body">
      <div class="badge-row" style="justify-content:flex-start"><span class="badge severity-info">已确定分析目标</span><span class="badge confidence-{_h(confidence)}">{_h(confidence_label)}</span></div>
      <h3 style="margin:14px 0 0">{_h(program)} · PID <span class="mono">{_h(pid)}</span></h3>
      <p style="margin:10px 0 0">数据集路径：<strong class="mono">{_h(path)}</strong> · 文件系统：<strong>{_h(filesystem)}</strong></p>
      {command_html}
    </div></div>"""


def _render_quality(snapshot: dict[str, Any], artifacts: list[dict[str, Any]]) -> str:
    provider_cards = []
    provider_names = _provider_names(snapshot)
    for name in provider_names:
        provider = _dict(snapshot.get(name))
        status = str(provider.get("status") or "unknown")
        note = provider.get("error") or provider.get("stderr") or provider.get("source") or ""
        provider_cards.append(
            f"""<article class="provider"><div class="provider-name">{_h(name)}</div><div style="margin-top:9px">{_badge(status, 'status')}</div><div class="provider-note">{_h(note, limit=300)}</div></article>"""
        )
    availability = _dict(snapshot.get("availability"))
    artifact_rows = []
    for item in artifacts:
        size_kib = item["size"] / 1024.0
        artifact_rows.append(
            f"""<div class="artifact"><strong>{_h(item['label'])}</strong><code>{_h(item['name'])}<br>sha256: {_h(item['sha256'])}</code><span>{size_kib:,.1f} KiB</span></div>"""
        )
    return f"""<div class="provider-grid">{''.join(provider_cards)}</div>
      <div class="data-grid" style="margin-top:14px">
        <div class="mini-block"><h4>完全缺失</h4>{_bullet_list(availability.get('missing'), '无')}</div>
        <div class="mini-block"><h4>部分可用 / 不支持</h4>{_bullet_list(availability.get('partial'), '无')}</div>
        <div class="mini-block wide"><h4>采集错误</h4>{_bullet_list(availability.get('errors'), '无')}</div>
        <div class="panel wide"><div class="panel-title"><h3>输入产物追溯</h3><span>文件名 / 大小 / SHA-256</span></div><div class="panel-body artifact-list">{''.join(artifact_rows)}</div></div>
      </div>"""


def _provider_names(snapshot: dict[str, Any]) -> tuple[str, ...]:
    schema_parts = str(snapshot.get("schema_version", "")).split(".")
    glusterfs_contract = (
        len(schema_parts) == 2
        and schema_parts[0] == "1"
        and schema_parts[1].isdigit()
        and int(schema_parts[1]) >= 5
    )
    if "glusterfs" in snapshot or glusterfs_contract:
        return _PROVIDERS
    return tuple(name for name in _PROVIDERS if name != "glusterfs")


def render_report(
    *,
    snapshot: dict[str, Any],
    findings: dict[str, Any],
    template_text: str,
    title: str,
    artifacts: list[dict[str, Any]],
    targets: dict[str, Any] | None = None,
    msprof: dict[str, Any] | None = None,
    agent: dict[str, Any] | None = None,
) -> str:
    target = _dict(snapshot.get("target"))
    window = _dict(snapshot.get("window"))
    host = _dict(snapshot.get("host"))
    duration = snapshot.get("duration_seconds")
    finding_items = [item for item in _list(findings.get("findings")) if isinstance(item, dict)]
    high_count = sum(
        1
        for item in finding_items
        if item.get("severity") == "high" and item.get("confidence") in {"high", "medium"}
    )
    provider_names = _provider_names(snapshot)
    provider_ok = sum(
        1 for name in provider_names if _dict(snapshot.get(name)).get("status") == "ok"
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    summary = findings.get("summary") or "规则分析没有提供总摘要。"

    hero = f"""<main class="shell">
      <section class="hero" id="overview">
        <div class="eyebrow">STORAGE / HOST IO / NPU FEEDING</div>
        <h1>{_h(title)}</h1><p class="hero-summary">{_h(summary)}</p>
        <div class="hero-meta"><span class="meta-chip">主机 <strong>{_h(host.get('hostname'))}</strong></span><span class="meta-chip">窗口 <strong>{_h(window.get('start'))} → {_h(window.get('end'))}</strong></span><span class="meta-chip">Schema <strong>{_h(snapshot.get('schema_version'))}</strong></span></div>
        <div class="hero-grid">
          <article class="metric-card"><div class="metric-label">目标进程 / PID</div><div class="metric-value mono">{_h(target.get('pid'))}</div><div class="metric-foot">数据路径：{_h(target.get('path'))}</div></article>
          <article class="metric-card"><div class="metric-label">高优先级问题</div><div class="metric-value">{high_count}</div><div class="metric-foot">共 {len(finding_items)} 条规则结论</div></article>
          <article class="metric-card"><div class="metric-label">动态采集窗口</div><div class="metric-value">{_fmt(duration, 1, ' 秒')}</div><div class="metric-foot">必须与 workload 活跃时间一致</div></article>
          <article class="metric-card"><div class="metric-label">成功数据源</div><div class="metric-value">{provider_ok} / {len(provider_names)}</div><div class="metric-foot">缺失与失败会降低结论置信度</div></article>
        </div>
      </section>"""

    sections = [
        _section(
            "agent",
            "Agent 总结与建议",
            "先看核心结论，再核对判断依据、证据边界和下一步动作。",
            "agent",
            _render_agent(agent),
        ),
        _section(
            "target-discovery",
            "本次分析目标",
            "这里只展示最终选中的训练程序、数据集路径和必要运行信息；候选详情保留在机器可读产物中。",
            "target",
            _render_targets(targets),
        ),
        _section(
            "findings",
            "R000—R500 确定性结论",
            "相同的 Snapshot 应得到相同的规则结果。先看严重度与置信度，再看证据和缺失项。",
            "findings",
            _render_findings(findings),
        ),
        _section(
            "metrics",
            "服务器关键指标",
            "把 Snapshot 中最常用的本地盘、GlusterFS、辅助 NFS 和进程 IO 指标转换成表格与数据条。",
            "metrics",
            f'<div class="data-grid">{_render_disk_table(snapshot)}{_render_glusterfs_table(snapshot) if "glusterfs" in provider_names else ""}{_render_nfs_table(snapshot)}{_render_process_table(snapshot)}</div>',
        ),
        _section(
            "npu-context",
            "NPU 侧辅助线索",
            "summarize_msprof.py 只提供侧面线索；它的输出不能直接替代 R500 所需的正式设备时间线。",
            "metrics",
            _render_msprof(msprof),
        ),
        _section(
            "quality",
            "证据质量与产物追溯",
            "没有采到不等于没有问题。这里展示每个 provider 的真实状态，以及输入文件的校验摘要。",
            "quality",
            _render_quality(snapshot, artifacts),
        ),
        _section(
            "pipeline",
            "数据是怎样变成报告的",
            "每个程序只负责一件事；可选产物未提供时，页面会明确标记，不会伪装成正常。",
            "overview",
            _render_pipeline(targets, msprof, agent),
        ),
    ]
    report_body = hero + "".join(sections) + "</main>"
    return Template(template_text).substitute(
        report_title=_h(title),
        generated_at=_h(generated_at),
        report_body=report_body,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="将存储分析 JSON 与 Agent 建议渲染为单文件离线 HTML 报告"
    )
    parser.add_argument("--snapshot", required=True, type=Path, help="io_snapshot.json")
    parser.add_argument("--findings", required=True, type=Path, help="findings.json")
    parser.add_argument("--targets", type=Path, help="可选 target_candidates.json")
    parser.add_argument("--msprof", type=Path, help="可选 op_summary_diagnostics.json")
    parser.add_argument("--agent-report", type=Path, help="可选 Agent 总结与建议 JSON")
    parser.add_argument("--title", default="存储与 NPU 供数诊断报告")
    parser.add_argument("-o", "--output", required=True, type=Path, help="输出 HTML 文件")
    args = parser.parse_args(argv)
    if not args.title.strip() or len(args.title) > 200:
        parser.error("--title must contain 1-200 characters")

    template_path = Path(__file__).resolve().parents[1] / "assets" / "io_report_template.html"
    try:
        snapshot = _read_json(args.snapshot.resolve(), "io_snapshot.json")
        findings = _read_json(args.findings.resolve(), "findings.json")
        targets = _read_json(args.targets.resolve(), "target_candidates.json") if args.targets else None
        msprof = _read_json(args.msprof.resolve(), "op_summary_diagnostics.json") if args.msprof else None
        raw_agent = _read_json(args.agent_report.resolve(), "agent_report.json") if args.agent_report else None
        agent = _normalize_agent_report(raw_agent)
        template_text = template_path.read_text(encoding="utf-8")
        input_paths = [
            (args.snapshot.resolve(), "IO Snapshot"),
            (args.findings.resolve(), "规则 Findings"),
        ]
        if args.targets:
            input_paths.append((args.targets.resolve(), "目标候选"))
        if args.msprof:
            input_paths.append((args.msprof.resolve(), "msprof 辅助摘要"))
        if args.agent_report:
            input_paths.append((args.agent_report.resolve(), "Agent 总结"))
        artifacts = [_artifact(path, label) for path, label in input_paths]
        report = render_report(
            snapshot=snapshot,
            findings=findings,
            targets=targets,
            msprof=msprof,
            agent=agent,
            template_text=template_text,
            title=args.title.strip(),
            artifacts=artifacts,
        )
        _atomic_write(args.output.resolve(), report)
    except (OSError, ValueError, KeyError) as exc:
        print(f"HTML 报告生成失败：{exc}", file=os.sys.stderr)
        return 1
    print(f"HTML 报告已写入: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
