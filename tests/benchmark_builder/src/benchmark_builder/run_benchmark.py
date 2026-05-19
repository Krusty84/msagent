from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

from .claude_cli import ClaudeCliAgent, ClaudeCliJudge
from .codex_cli import CodexCliAgent, CodexCliJudge
from .judge import MockLLMJudge, normalized_judge_score
from .metrics import build_case_metrics
from .msagent_cli import MsagentCliAgent, MsagentCliJudge
from .mock_agent import MockSlowCardAgent
from .schema import load_suite
from .scoring import RuleBasedTraceScorer


def run(
    config: Path,
    out_dir: Path,
    agent_kind: str = "codex-cli",
    judge_kind: str = "codex-cli",
    model: str | None = None,
    judge_model: str | None = None,
    timeout_seconds: int = 900,
    msagent_agent: str | None = None,
) -> dict[str, Any]:
    suite = load_suite(config)
    out_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = out_dir / "runtime"
    agent = _build_agent(
        agent_kind,
        workspace=Path.cwd(),
        artifact_dir=runtime_dir / "agent",
        model=model,
        timeout_seconds=timeout_seconds,
        msagent_agent=msagent_agent,
    )
    scorer = RuleBasedTraceScorer()
    judge = None
    if judge_kind != "none":
        judge = _build_judge(
            judge_kind,
            workspace=Path.cwd(),
            artifact_dir=runtime_dir / "judge",
            model=judge_model or model,
            timeout_seconds=timeout_seconds,
            msagent_agent=msagent_agent,
        )

    traces_dir = out_dir / "traces"
    metrics_dir = out_dir / "metrics"
    traces_dir.mkdir(parents=True, exist_ok=True)
    judge_dir = out_dir / "judge"
    if judge is not None:
        judge_dir.mkdir(parents=True, exist_ok=True)
    metrics_dir.mkdir(parents=True, exist_ok=True)

    scores = []
    metrics_items = []
    for case in suite.cases:
        input_path = case.resolve_input_path()
        if not input_path.exists():
            raise FileNotFoundError(f"Missing input data for {case.id}: {input_path}")

        trace = agent.run(case, input_path)
        trace_path = traces_dir / f"{case.id}.trace.json"
        trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")

        correctness = scorer.score(case, trace)
        judge_result = None
        judge_path = None
        if judge is not None:
            judge_result = judge.judge(case, trace, correctness)
            judge_path = judge_dir / f"{case.id}.judge.json"
            judge_path.write_text(
                json.dumps(judge_result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        case_metrics = build_case_metrics(case.id, trace, judge_result)
        metrics_path = metrics_dir / f"{case.id}.metrics.json"
        metrics_path.write_text(
            json.dumps(case_metrics, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        metrics_items.append(case_metrics)

        if judge_result is None:
            final_score = round(float(correctness["f1"]), 4)
        else:
            final_score = round(
                0.7 * float(correctness["f1"]) + 0.3 * normalized_judge_score(judge_result),
                4,
            )
        score = {
            **correctness,
            "correctness_score": correctness["score"],
            "judge_score": judge_result["overall_score"] if judge_result is not None else None,
            "score": final_score,
            "trace_path": str(trace_path),
            "metrics_path": str(metrics_path),
        }
        if judge_path is not None:
            score["judge_path"] = str(judge_path)
        scores.append(score)

    judge_scores = [item["judge_score"] for item in scores if item["judge_score"] is not None]
    report = {
        "suite": suite.name,
        "case_count": len(scores),
        "judge_enabled": judge is not None,
        "average_score": round(mean(item["score"] for item in scores), 4),
        "average_correctness_score": round(mean(item["correctness_score"] for item in scores), 4),
        "average_judge_score": round(mean(judge_scores), 4) if judge_scores else None,
        "token_usage": _sum_token_usage(metrics_items),
        "duration_ms": _sum_durations(metrics_items),
        "tool_calls": _sum_tool_calls(metrics_items),
        "scores": scores,
    }

    (out_dir / "scores.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(_render_markdown_report(report), encoding="utf-8")
    return report


def _build_agent(
    agent_kind: str,
    *,
    workspace: Path,
    artifact_dir: Path,
    model: str | None,
    timeout_seconds: int,
    msagent_agent: str | None,
) -> Any:
    if agent_kind == "codex-cli":
        return CodexCliAgent(
            workspace=workspace,
            artifact_dir=artifact_dir,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    if agent_kind == "claude-cli":
        return ClaudeCliAgent(
            workspace=workspace,
            artifact_dir=artifact_dir,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    if agent_kind == "msagent-cli":
        return MsagentCliAgent(
            workspace=workspace,
            artifact_dir=artifact_dir,
            model=model,
            timeout_seconds=timeout_seconds,
            msagent_agent=msagent_agent,
        )
    if agent_kind == "heuristic":
        return MockSlowCardAgent()
    raise ValueError(f"Unknown agent kind: {agent_kind}")


def _build_judge(
    judge_kind: str,
    *,
    workspace: Path,
    artifact_dir: Path,
    model: str | None,
    timeout_seconds: int,
    msagent_agent: str | None,
) -> Any:
    if judge_kind == "codex-cli":
        return CodexCliJudge(
            workspace=workspace,
            artifact_dir=artifact_dir,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    if judge_kind == "claude-cli":
        return ClaudeCliJudge(
            workspace=workspace,
            artifact_dir=artifact_dir,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    if judge_kind == "msagent-cli":
        return MsagentCliJudge(
            workspace=workspace,
            artifact_dir=artifact_dir,
            model=model,
            timeout_seconds=timeout_seconds,
            msagent_agent=msagent_agent,
        )
    if judge_kind == "heuristic":
        return MockLLMJudge()
    if judge_kind == "none":
        return None
    raise ValueError(f"Unknown judge kind: {judge_kind}")


def _render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Benchmark Report: {report['suite']}",
        "",
        f"- Cases: {report['case_count']}",
        f"- Judge: {'enabled' if report.get('judge_enabled') else 'disabled'}",
        f"- Average score: {report['average_score']:.4f}",
        f"- Average correctness score: {report['average_correctness_score']:.4f}",
        _format_judge_score(report.get("average_judge_score")),
        f"- Total tokens: {report['token_usage']['total_tokens']}",
        f"- Total duration: {report['duration_ms']['total']} ms",
        f"- Agent tool calls: {report['tool_calls']['agent']['count']}",
        "",
        "| Case | Final | Correctness | Judge | Exact | Precision | Recall | Predicted | Ground Truth |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for item in report["scores"]:
        judge_score = item["judge_score"]
        lines.append(
            "| {case_id} | {score:.4f} | {correctness_score:.4f} | {judge_score} | "
            "{exact_match:.0f} | {precision:.2f} | {recall:.2f} | {predicted} | {ground_truth} |".format(
                case_id=item["case_id"],
                score=item["score"],
                correctness_score=item["correctness_score"],
                judge_score=f"{judge_score:.2f}" if judge_score is not None else "n/a",
                exact_match=1 if item["exact_match"] else 0,
                precision=item["precision"],
                recall=item["recall"],
                predicted=", ".join(item["predicted_slow_cards"]) or "[]",
                ground_truth=", ".join(item["ground_truth"]) or "[]",
            )
        )
    lines.append("")
    return "\n".join(lines)


def _format_judge_score(value: Any) -> str:
    if value is None:
        return "- Average judge score: n/a"
    return f"- Average judge score: {float(value):.2f}/5"


def _sum_token_usage(metrics_items: list[dict[str, Any]]) -> dict[str, int]:
    total: dict[str, Any] = {
        "available": True,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "unavailable_cases": [],
    }
    for item in metrics_items:
        usage = item.get("token_usage", {}).get("total", {})
        if not usage.get("available", True):
            total["available"] = False
            total["unavailable_cases"].append(item.get("case_id"))
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            total[key] += int(usage.get(key, 0))
    return total


def _sum_durations(metrics_items: list[dict[str, Any]]) -> dict[str, int]:
    total = {"agent": 0, "judge": 0, "total": 0}
    for item in metrics_items:
        duration = item.get("duration_ms", {})
        for key in total:
            total[key] += int(duration.get(key, 0))
    return total


def _sum_tool_calls(metrics_items: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {
        "agent": {
            "count": 0,
            "by_tool": {},
        },
    }
    by_tool = total["agent"]["by_tool"]
    for item in metrics_items:
        agent_calls = item.get("tool_calls", {}).get("agent", {})
        total["agent"]["count"] += int(agent_calls.get("count", 0))
        for tool, count in agent_calls.get("by_tool", {}).items():
            by_tool[tool] = int(by_tool.get(tool, 0)) + int(count)
    total["agent"]["by_tool"] = dict(sorted(by_tool.items()))
    return total


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a trace-based benchmark suite.")
    parser.add_argument("--config", type=Path, required=True, help="Path to benchmark YAML.")
    parser.add_argument("--out", type=Path, required=True, help="Output directory for traces and scores.")
    parser.add_argument(
        "--agent",
        choices=["codex-cli", "claude-cli", "msagent-cli", "heuristic"],
        default="codex-cli",
        help=(
            "Agent adapter to run. codex-cli, claude-cli, and msagent-cli are real CLI agents; "
            "heuristic is local-only."
        ),
    )
    parser.add_argument(
        "--judge",
        choices=["codex-cli", "claude-cli", "msagent-cli", "heuristic", "none"],
        default="codex-cli",
        help=(
            "Judge adapter to run. codex-cli, claude-cli, and msagent-cli are real LLM judges; "
            "heuristic is local-only; none skips judging."
        ),
    )
    parser.add_argument("--model", help="Model for the selected real CLI agent.")
    parser.add_argument("--judge-model", help="Model for the selected real CLI judge. Defaults to --model.")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--msagent-agent",
        help="Built-in msAgent persona to use for msagent-cli runs. Defaults to MSAGENT_AGENT or Hermes.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = run(
        args.config,
        args.out,
        agent_kind=args.agent,
        judge_kind=args.judge,
        model=args.model,
        judge_model=args.judge_model,
        timeout_seconds=args.timeout_seconds,
        msagent_agent=args.msagent_agent,
    )
    print(
        f"Ran {report['case_count']} cases from {report['suite']}; "
        f"average_score={report['average_score']:.4f}"
    )


if __name__ == "__main__":
    main()
