from __future__ import annotations

import json
from time import perf_counter
from typing import Any

from .schema import BenchmarkCase


class MockLLMJudge:
    """Structured judge stub.

    This has the same input/output contract a real LLM judge should use, but it
    stays deterministic so benchmark development is repeatable.
    """

    judge_info = {
        "name": "mock-llm-judge",
        "model": "heuristic-judge-v0",
        "token_usage_mode": "estimated",
    }

    def judge(
        self,
        case: BenchmarkCase,
        trace: dict[str, Any],
        correctness: dict[str, Any],
    ) -> dict[str, Any]:
        started = perf_counter()
        answer = self._final_answer(trace)
        observations = [
            str(event.get("content", ""))
            for event in trace.get("events", [])
            if event.get("type") == "observation"
        ]
        tools = [
            str(event.get("tool"))
            for event in trace.get("events", [])
            if event.get("type") == "tool_call"
        ]

        reasoning_score = self._score_reasoning(observations)
        evidence_score = self._score_evidence(answer, observations)
        tool_use_score = self._score_tool_use(tools)
        result_score = round(float(correctness.get("f1", 0.0)) * 5, 2)
        format_score = 5.0 if isinstance(answer.get("slow_cards"), list) else 1.0

        overall = round(
            (
                reasoning_score
                + evidence_score
                + tool_use_score
                + result_score
                + format_score
            )
            / 5,
            2,
        )

        result = {
            "case_id": case.id,
            "judge": self.judge_info,
            "reasoning_score": reasoning_score,
            "evidence_score": evidence_score,
            "tool_use_score": tool_use_score,
            "result_score": result_score,
            "format_score": format_score,
            "overall_score": overall,
            "strengths": self._strengths(correctness, observations),
            "weaknesses": self._weaknesses(correctness, observations),
        }
        result["duration_ms"] = round((perf_counter() - started) * 1000)
        result["token_usage"] = self._estimate_token_usage(case, trace, result)
        return result

    def _score_reasoning(self, observations: list[str]) -> float:
        text = " ".join(observations).lower()
        score = 2.0
        if "peer" in text or "median" in text:
            score += 1.0
        if "stage" in text or "throughput" in text:
            score += 1.0
        if "free" in text or "host" in text or "synchronization" in text:
            score += 1.0
        return min(5.0, score)

    def _score_evidence(self, answer: dict[str, Any], observations: list[str]) -> float:
        evidence = answer.get("evidence", [])
        evidence_count = len(evidence) if isinstance(evidence, list) else 0
        observation_count = len([item for item in observations if item.strip()])
        return min(5.0, 1.5 + evidence_count + observation_count * 0.5)

    def _score_tool_use(self, tools: list[str]) -> float:
        if not tools:
            return 0.0
        distinct = set(tools)
        if "read_cluster_step_trace" in distinct or "read_metrics" in distinct:
            return min(5.0, 2.0 + len(distinct))
        return min(4.0, 1.0 + len(distinct))

    def _strengths(self, correctness: dict[str, Any], observations: list[str]) -> list[str]:
        strengths = []
        if correctness.get("exact_match"):
            strengths.append("Correctly identified the slow-card set.")
        text = " ".join(observations).lower()
        if "free" in text or "host" in text:
            strengths.append("Considered Free/host-side overhead instead of only device compute.")
        if "peer" in text or "median" in text:
            strengths.append("Compared the candidate card against peer ranks/cards.")
        return strengths or ["Produced a structured slow_cards answer."]

    def _weaknesses(self, correctness: dict[str, Any], observations: list[str]) -> list[str]:
        weaknesses = []
        if not correctness.get("exact_match"):
            weaknesses.append("Predicted slow_cards did not exactly match ground truth.")
        if len(observations) < 2:
            weaknesses.append("Trace has limited intermediate observations for judging the process.")
        return weaknesses

    def _final_answer(self, trace: dict[str, Any]) -> dict[str, Any]:
        finals = [
            event
            for event in trace.get("events", [])
            if event.get("type") == "final_answer"
        ]
        if not finals:
            return {}
        answer = finals[-1].get("answer", {})
        return answer if isinstance(answer, dict) else {}

    def _estimate_token_usage(
        self,
        case: BenchmarkCase,
        trace: dict[str, Any],
        judge_result: dict[str, Any],
    ) -> dict[str, int]:
        judge_input = {
            "case": {
                "id": case.id,
                "prompt": case.prompt,
                "ground_truth": case.ground_truth,
            },
            "trace": trace,
        }
        input_tokens = max(1, round(len(json.dumps(judge_input, ensure_ascii=False)) / 4))
        output_tokens = max(1, round(len(json.dumps(judge_result, ensure_ascii=False)) / 4))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }


def normalized_judge_score(judge_result: dict[str, Any]) -> float:
    return max(0.0, min(1.0, float(judge_result.get("overall_score", 0.0)) / 5.0))

