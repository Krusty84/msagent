from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

from .schema import BenchmarkCase
from .trace import TraceBuilder


class MockSlowCardAgent:
    """Deterministic slow-card agent stub for developing the benchmark harness.

    Replace this class with a real agent adapter when the trace contract and
    scorer are stable.
    """

    agent_info = {
        "name": "mock-slow-card-agent",
        "model": "heuristic-v0",
        "token_usage_mode": "estimated",
    }

    def run(self, case: BenchmarkCase, input_path: Path) -> dict[str, Any]:
        trace = TraceBuilder(case_id=case.id, prompt=case.prompt, agent=self.agent_info)
        if (input_path / "metrics.csv").exists():
            return self._run_metric_bundle(trace, input_path)
        if (input_path / "cluster_analysis_output" / "cluster_step_trace_time.csv").exists():
            return self._run_ascend_profile(trace, input_path)
        raise FileNotFoundError(f"Unsupported input data layout: {input_path}")

    def _run_metric_bundle(self, trace: TraceBuilder, input_path: Path) -> dict[str, Any]:
        trace.thought(
            "Check time-correlated telemetry first, then validate the diagnosis "
            "against logs, events, and topology."
        )

        metrics_path = input_path / "metrics.csv"
        trace.tool_call("read_metrics", {"path": str(metrics_path)})
        started = perf_counter()
        metrics_summary = self._read_metrics(metrics_path)
        slow_cards = self._predict_slow_cards(metrics_path)
        trace.tool_result("read_metrics", metrics_summary, self._elapsed_ms(started))
        trace.observation(
            "Identified slow-card candidates by comparing each card's sustained "
            "throughput and step time against peer medians."
        )

        logs_path = input_path / "logs.txt"
        if logs_path.exists():
            trace.tool_call("read_logs", {"path": str(logs_path)})
            started = perf_counter()
            logs_summary = self._read_text(logs_path)
            trace.tool_result("read_logs", logs_summary, self._elapsed_ms(started))
            trace.observation("Checked logs for warnings, retries, slow phases, and error signatures.")

        events_path = input_path / "events.jsonl"
        if events_path.exists():
            trace.tool_call("inspect_events", {"path": str(events_path)})
            started = perf_counter()
            events_summary = self._read_jsonl(events_path)
            trace.tool_result("inspect_events", events_summary, self._elapsed_ms(started))
            trace.observation("Checked timeline events for changes that align with the regression window.")

        topology_path = input_path / "topology.json"
        if topology_path.exists():
            trace.tool_call("query_topology", {"path": str(topology_path)})
            started = perf_counter()
            trace.tool_result("query_topology", self._read_json(topology_path), self._elapsed_ms(started))

        trace.final_answer(self._build_final_answer(
            slow_cards,
            [
                "card throughput below 80% of peer median",
                "card step time above 125% of peer median",
            ],
        ))
        trace.finish(self._estimate_agent_token_usage(trace.to_dict()))
        return trace.to_dict()

    def _run_ascend_profile(self, trace: TraceBuilder, input_path: Path) -> dict[str, Any]:
        trace.thought(
            "For Ascend cluster profiler data, compare whole-step Stage first, "
            "then explain whether the abnormal rank is due to compute, communication, "
            "or host/free time."
        )

        step_path = input_path / "cluster_analysis_output" / "cluster_step_trace_time.csv"
        trace.tool_call("read_cluster_step_trace", {"path": str(step_path)})
        started = perf_counter()
        step_summary = self._read_cluster_step_trace(step_path)
        trace.tool_result("read_cluster_step_trace", step_summary, self._elapsed_ms(started))

        slow_cards = self._predict_ascend_slow_cards(step_summary["ranks"])
        stage_rank = step_summary["max_stage_rank"]
        free_rank = step_summary["max_free_rank"]
        trace.observation(
            f"rank{stage_rank} has the largest Stage time, while rank{free_rank} has "
            "a large Free-time outlier. Treating Free as host/synchronization overhead "
            "points to the abnormal slow rank instead of labeling it as a fast card."
        )

        advisor_path = self._find_advisor_report(input_path)
        if advisor_path is not None:
            trace.tool_call("read_advisor_report", {"path": str(advisor_path)})
            started = perf_counter()
            advisor_summary = self._read_advisor_summary(advisor_path)
            trace.tool_result("read_advisor_report", advisor_summary, self._elapsed_ms(started))
            trace.observation(
                "Advisor report includes slow-rank analysis and compares Rank3 "
                "against peer ranks, supporting rank3 as the main abnormal card."
            )

        trace.final_answer(self._build_final_answer(
            slow_cards,
            [
                "rank3 has the highest Stage time",
                "rank3 has a large Free-time outlier consistent with host/API overhead",
                "advisor report compares Rank3 against peer ranks in slow-rank analysis",
            ],
        ))
        trace.finish(self._estimate_agent_token_usage(trace.to_dict()))
        return trace.to_dict()

    def _read_metrics(self, path: Path) -> dict[str, Any]:
        rows: list[dict[str, str]] = []
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)

        numeric_max: dict[str, float] = {}
        numeric_min: dict[str, float] = {}
        for row in rows:
            for key, value in row.items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                numeric_max[key] = max(number, numeric_max.get(key, number))
                numeric_min[key] = min(number, numeric_min.get(key, number))

        return {
            "rows": len(rows),
            "columns": list(rows[0].keys()) if rows else [],
            "numeric_max": numeric_max,
            "numeric_min": numeric_min,
            "sample_rows": rows[:2] + rows[-2:] if len(rows) > 4 else rows,
        }

    def _read_metric_rows(self, path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def _predict_slow_cards(self, path: Path) -> list[str]:
        rows = self._read_metric_rows(path)
        per_card: dict[str, dict[str, list[float]]] = {}
        for row in rows:
            card_id = row.get("card_id")
            if not card_id:
                continue
            bucket = per_card.setdefault(card_id, {"tokens_per_sec": [], "step_time_ms": []})
            for key in bucket:
                try:
                    bucket[key].append(float(row[key]))
                except (KeyError, TypeError, ValueError):
                    pass

        card_stats = {}
        for card_id, values in per_card.items():
            if values["tokens_per_sec"]:
                card_stats[card_id] = {
                    "tokens_per_sec": sum(values["tokens_per_sec"]) / len(values["tokens_per_sec"]),
                    "step_time_ms": sum(values["step_time_ms"]) / len(values["step_time_ms"]),
                }

        if not card_stats:
            return []

        throughput_median = median(item["tokens_per_sec"] for item in card_stats.values())
        step_time_median = median(item["step_time_ms"] for item in card_stats.values())

        slow_cards = []
        for card_id, stats in card_stats.items():
            low_throughput = stats["tokens_per_sec"] < throughput_median * 0.8
            high_step_time = stats["step_time_ms"] > step_time_median * 1.25
            if low_throughput and high_step_time:
                slow_cards.append(card_id)

        return sorted(slow_cards)

    def _read_cluster_step_trace(self, path: Path) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("Type") != "rank":
                    continue
                rank = int(row["Index"])
                parsed = {
                    "rank": rank,
                    "step": int(row["Step"]),
                    "computing_us": float(row["Computing"]),
                    "communication_us": float(row["Communication(Not Overlapped and Exclude Receive)"]),
                    "free_us": float(row["Free"]),
                    "stage_us": float(row["Stage"]),
                    "preparing_us": float(row["Preparing"]),
                }
                parsed["active_no_free_us"] = (
                    parsed["computing_us"] + parsed["communication_us"] + parsed["preparing_us"]
                )
                rows.append(parsed)

        if not rows:
            raise ValueError(f"No rank rows found in {path}")

        max_stage = max(rows, key=lambda item: item["stage_us"])
        max_free = max(rows, key=lambda item: item["free_us"])
        return {
            "ranks": sorted(rows, key=lambda item: item["rank"]),
            "max_stage_rank": max_stage["rank"],
            "max_stage_us": max_stage["stage_us"],
            "max_free_rank": max_free["rank"],
            "max_free_us": max_free["free_us"],
        }

    def _predict_ascend_slow_cards(self, rows: list[dict[str, Any]]) -> list[str]:
        free_values = [row["free_us"] for row in rows]
        median_free = median(free_values)
        max_stage = max(rows, key=lambda row: row["stage_us"])
        max_free = max(rows, key=lambda row: row["free_us"])

        if median_free > 0 and max_free["free_us"] > median_free * 5:
            return [f"rank{max_free['rank']}"]
        return [f"rank{max_stage['rank']}"]

    def _read_text(self, path: Path, max_chars: int = 6000) -> str:
        return path.read_text(encoding="utf-8")[:max_chars]

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def _read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _find_advisor_report(self, input_path: Path) -> Path | None:
        candidates = sorted(input_path.glob("mstt_advisor_*.html"))
        if candidates:
            return candidates[0]
        candidates = sorted((input_path / "log").glob("mstt_advisor_*.xlsx"))
        return candidates[0] if candidates else None

    def _read_advisor_summary(self, path: Path) -> dict[str, Any]:
        if path.suffix.lower() == ".html":
            text = self._read_text(path, max_chars=120000)
            return {
                "slow_rank_section": self._extract_html_section(text, "slow rank", "slow link"),
                "comparison_mentions": sorted(set(re.findall(r"Rank\d+ Step\d+", text))),
            }
        return {"path": str(path), "note": "Advisor workbook detected."}

    def _extract_html_section(self, text: str, start_marker: str, end_marker: str) -> str:
        start = text.find(start_marker)
        if start < 0:
            return ""
        end = text.find(end_marker, start + len(start_marker))
        section = text[start:end if end > start else start + 8000]
        section = re.sub(r"<[^>]+>", " ", section)
        section = html.unescape(section)
        return " ".join(section.split())[:2000]

    def _build_final_answer(self, slow_cards: list[str], evidence: list[str]) -> dict[str, Any]:
        return {
            "slow_cards": slow_cards,
            "evidence": evidence,
            "reasoning_summary": (
                "Compare each rank against peer ranks, use whole-step latency for "
                "correctness, and inspect component timing to avoid confusing "
                "short device compute with a genuinely fast card."
            ),
            "confidence": 0.78,
        }

    def _elapsed_ms(self, started: float) -> int:
        return round((perf_counter() - started) * 1000)

    def _estimate_agent_token_usage(self, trace: dict[str, Any]) -> dict[str, int]:
        prompt_chars = len(trace.get("prompt", ""))
        tool_chars = 0
        output_chars = 0
        for event in trace.get("events", []):
            if event.get("type") == "tool_result":
                tool_chars += len(json.dumps(event.get("output", ""), ensure_ascii=False))
            elif event.get("type") in {"observation", "final_answer"}:
                output_chars += len(json.dumps(event, ensure_ascii=False))
        input_tokens = max(1, round((prompt_chars + tool_chars) / 4))
        output_tokens = max(1, round(output_chars / 4))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
