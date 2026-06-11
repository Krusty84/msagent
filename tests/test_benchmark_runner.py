from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_SRC = PROJECT_ROOT / "tests" / "benchmark" / "src"


def _benchmark_module(name: str):
    src = str(BENCHMARK_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    return importlib.import_module(name)


def test_benchmark_smoke_case_runs_with_tracked_fixture(tmp_path: Path) -> None:
    run = _benchmark_module("run_benchmark").run
    config = PROJECT_ROOT / "tests" / "benchmark" / "benchmarks" / "mock_agent_smoke.yaml"
    out_dir = tmp_path / "out"

    report = run(
        config,
        out_dir,
        agent_kind="heuristic",
        judge_kind="heuristic",
        timeout_seconds=5,
    )

    assert report["case_count"] == 1
    assert report["failed_count"] == 0
    assert report["scores"][0]["case_id"] == "mock_agent_smoke"
    assert report["scores"][0]["score"] > 0
    assert (out_dir / "scores.json").exists()
    assert (out_dir / "traces" / "mock_agent_smoke.trace.json").exists()


def test_benchmark_records_failed_case_and_continues(tmp_path: Path) -> None:
    run = _benchmark_module("run_benchmark").run
    case_path = tmp_path / "cases" / "missing_input.yaml"
    case_path.parent.mkdir()
    case_path.write_text(
        """
id: missing_input
input_data_path: ./missing-data
prompt: Read the input data.
must_include:
  - expected answer
scoring_prompt: Score the answer.
""".strip(),
        encoding="utf-8",
    )

    out_dir = tmp_path / "out"
    report = run(
        case_path,
        out_dir,
        agent_kind="heuristic",
        judge_kind="heuristic",
        timeout_seconds=5,
    )

    assert report["case_count"] == 1
    assert report["failed_count"] == 1
    assert report["average_score"] == 0.0
    assert report["scores"][0]["status"] == "failed"
    failure_path = Path(report["failed_cases"][0]["failure_path"])
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    assert failure["case_id"] == "missing_input"
    assert failure["error"]["type"] == "FileNotFoundError"
    assert (out_dir / "scores.json").exists()


def test_must_include_results_only_match_explicit_item_keys(tmp_path: Path) -> None:
    run_benchmark = _benchmark_module("run_benchmark")
    schema = _benchmark_module("schema")
    case = schema.BenchmarkCase(
        id="case",
        input_data_path="input",
        prompt="prompt",
        must_include=["expected item"],
        must_include_regex=[],
        must_tool_use=[],
        scoring_prompt="score",
        source_path=tmp_path / "case.yaml",
    )

    normalized = run_benchmark._normalize_must_include_results(
        case,
        {
            "must_include_results": [
                {
                    "item": "different item",
                    "covered": True,
                    "reason": "This must not be reused by position.",
                }
            ]
        },
    )

    assert normalized == [
        {
            "item": "expected item",
            "covered": False,
            "reason": "Judge did not return a result for this required item.",
        }
    ]


def test_load_suite_finds_nested_case_files(tmp_path: Path) -> None:
    schema = _benchmark_module("schema")
    case_path = tmp_path / "suite" / "domain" / "nested.yaml"
    case_path.parent.mkdir(parents=True)
    case_path.write_text(
        """
id: nested
input_data_path: ./input
prompt: Read the input.
must_include:
  - nested answer
scoring_prompt: Score the answer.
""".strip(),
        encoding="utf-8",
    )

    suite = schema.load_suite(tmp_path / "suite")

    assert [case.id for case in suite.cases] == ["nested"]


def test_string_list_fields_are_single_items_not_comma_split(tmp_path: Path) -> None:
    schema = _benchmark_module("schema")

    case = schema.BenchmarkCase.from_dict(
        {
            "id": "case",
            "input_data_path": "./input",
            "prompt": "prompt",
            "must_include": "alpha, beta",
            "scoring_prompt": "score",
        },
        tmp_path / "case.yaml",
    )

    assert case.must_include == ["alpha, beta"]
