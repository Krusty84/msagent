from __future__ import annotations

from typing import Any

from .schema import BenchmarkCase


class RuleBasedTraceScorer:
    def score(self, case: BenchmarkCase, trace: dict[str, Any]) -> dict[str, Any]:
        predicted = set(self._predicted_slow_cards(trace))
        expected = set(case.ground_truth)

        true_positives = sorted(predicted & expected)
        false_positives = sorted(predicted - expected)
        false_negatives = sorted(expected - predicted)

        precision = self._safe_divide(len(true_positives), len(predicted))
        recall = self._safe_divide(len(true_positives), len(expected))
        f1 = self._f1(precision, recall)
        exact_match = predicted == expected

        return {
            "case_id": case.id,
            "score": round(f1, 4),
            "exact_match": exact_match,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "predicted_slow_cards": sorted(predicted),
            "ground_truth": sorted(expected),
            "true_positives": true_positives,
            "false_positives": false_positives,
            "false_negatives": false_negatives,
        }

    def _predicted_slow_cards(self, trace: dict[str, Any]) -> list[str]:
        answer = self._final_answer(trace)
        if not answer:
            return []
        raw_cards = answer.get("slow_cards", [])
        if isinstance(raw_cards, str):
            raw_cards = [item.strip() for item in raw_cards.split(",")]
        if not isinstance(raw_cards, list):
            return []
        return sorted(str(card).strip() for card in raw_cards if str(card).strip())

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

    def _safe_divide(self, numerator: int, denominator: int) -> float:
        if denominator == 0:
            return 1.0 if numerator == 0 else 0.0
        return numerator / denominator

    def _f1(self, precision: float, recall: float) -> float:
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)
