import json

from trajectory_extractor.cli import main
from trajectory_extractor.pipeline import ExtractionRequest, discover_threads, extract
from trajectory_extractor.sources.locate import resolve_project

THREAD = "thread-42"


def build_project(tmp_path, *, with_audit=True, with_history=False):
    """Lay out a fake MSAGENT_HOME the way msagent would have written it."""
    home = tmp_path / "msagent-home"
    working_dir = tmp_path / "workspace"
    working_dir.mkdir()

    root = resolve_project(working_dir, home=home).root
    root.mkdir(parents=True)

    if with_audit:
        audit_dir = root / "audit_log"
        audit_dir.mkdir()
        blocks = [
            {
                "agent_name": "Quantizer",
                "event": "user.turn",
                "run_id": "r1",
                "message": "квантуй /data/models/qwen3 под точность 0.9",
            },
            {
                "agent_name": "Quantizer",
                "event": "subagent.delegation",
                "subagent_type": "quant-tuning-quantizer",
                "run_id": "r1",
                "delegation_id": "d1",
                "status": "ok",
                "task_description_raw": "run w8a8 on /data/models/qwen3",
            },
        ]
        (audit_dir / f"Quantizer_{THREAD}.jsonl").write_text(
            "\n".join(json.dumps(block, ensure_ascii=False, indent=2) for block in blocks) + "\n",
            encoding="utf-8",
        )

    if with_history:
        history_dir = root / "conversation_history"
        history_dir.mkdir()
        (history_dir / f"{THREAD}.md").write_text(
            "## Offloaded at 2026-08-28T09:00:00+00:00\n\nHuman: подготовь окружение\nAI: готово\n",
            encoding="utf-8",
        )

    return home, working_dir


def write_trace(tmp_path):
    """A trace with a failure, a correction, a retry run and a repeated command."""
    events = [
        {"index": 1, "type": "session_started", "agent": "Quantizer", "thread_id": THREAD},
        {
            "index": 2,
            "type": "tool_call",
            "tool": "execute",
            "item_id": "c1",
            "input": {"command": "msmodelslim quant --model /data/models/qwen3 --device 3"},
        },
        {
            "index": 3,
            "type": "tool_result",
            "tool": "execute",
            "item_id": "c1",
            "output": {"content": "error: unsupported layer", "is_error": True},
        },
        {
            "index": 4,
            "type": "tool_call",
            "tool": "execute",
            "item_id": "c2",
            "input": {"command": "msmodelslim quant --model /data/models/qwen3 --device 3 --fallback"},
        },
        {
            "index": 5,
            "type": "tool_result",
            "tool": "execute",
            "item_id": "c2",
            "output": {"content": "written to /data/out/run01", "is_error": False},
        },
        {
            "index": 6,
            "type": "tool_call",
            "tool": "fetch",
            "item_id": "c3",
            "input": {"url": "https://eval.internal/run", "token": "sk-abcdefghijklmnopqrst"},
        },
        {
            "index": 7,
            "type": "tool_result",
            "tool": "fetch",
            "item_id": "c3",
            "output": {"content": "timeout", "is_error": True},
        },
        {
            "index": 8,
            "type": "tool_call",
            "tool": "fetch",
            "item_id": "c4",
            "input": {"url": "https://eval.internal/run", "token": "sk-abcdefghijklmnopqrst"},
        },
        {
            "index": 9,
            "type": "tool_result",
            "tool": "fetch",
            "item_id": "c4",
            "output": {"content": "timeout", "is_error": True},
        },
        {
            "index": 10,
            "type": "tool_call",
            "tool": "execute",
            "item_id": "c5",
            "input": {"command": "bash /home/kirill/scripts/report.sh"},
        },
        {
            "index": 11,
            "type": "tool_result",
            "tool": "execute",
            "item_id": "c5",
            "output": {"content": "ok", "is_error": False},
        },
        {
            "index": 12,
            "type": "tool_call",
            "tool": "execute",
            "item_id": "c6",
            "input": {"command": "bash /home/kirill/scripts/report.sh"},
        },
        {
            "index": 13,
            "type": "tool_result",
            "tool": "execute",
            "item_id": "c6",
            "output": {"content": "ok", "is_error": False},
        },
    ]
    path = tmp_path / "run.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return path


def run(tmp_path, **overrides):
    home, working_dir = build_project(tmp_path, **overrides.pop("project", {}))
    request = ExtractionRequest(
        working_dir=working_dir,
        thread_id=THREAD,
        home=home,
        trace_file=write_trace(tmp_path),
        **overrides,
    )
    return extract(request)


def test_extract_merges_audit_task_text_with_trace_steps(tmp_path):
    document = run(tmp_path)
    assert document.thread_id == THREAD
    assert document.agent == "Quantizer"
    assert any("квантуй" in message for message in document.user_messages)
    assert document.steps


def test_extract_collapses_the_identical_retry_run(tmp_path):
    document = run(tmp_path)
    fetches = [step for step in document.steps if step.tool == "fetch"]
    assert len(fetches) == 1
    assert fetches[0].repeat_count == 2
    assert fetches[0].ok is False


def test_extract_detects_the_failure_and_its_correction(tmp_path):
    document = run(tmp_path)
    assert len(document.recoveries) == 1
    recovery = document.recoveries[0]
    assert recovery.tool == "execute"
    assert recovery.changed_args == ["command"]
    assert "unsupported layer" in recovery.error


def test_extract_lifts_shared_values_into_one_placeholder(tmp_path):
    document = run(tmp_path)
    commands = [step.args.get("command", "") for step in document.steps]
    assert not any("/data/models/qwen3" in command for command in commands)
    assert any("<DEVICE_IDS>" in command for command in commands)


def test_extract_redacts_secrets_by_default(tmp_path):
    document = run(tmp_path)
    payload = json.dumps(document.to_json(), ensure_ascii=False)
    assert "sk-abcdefghijklmnopqrst" not in payload
    assert "/home/kirill" not in payload


def test_redaction_can_be_disabled(tmp_path):
    document = run(tmp_path, redact=False)
    payload = json.dumps(document.to_json(), ensure_ascii=False)
    assert "sk-abcdefghijklmnopqrst" in payload


def test_extract_flags_the_repeated_command_as_a_script_candidate(tmp_path):
    document = run(tmp_path)
    repeated = [item for item in document.script_candidates if item.reason == "repeated"]
    assert repeated
    assert repeated[0].occurrences == 2


def test_extract_carries_audit_phases_through(tmp_path):
    document = run(tmp_path)
    assert [phase.name for phase in document.phases] == ["quant-tuning-quantizer"]


def test_offloaded_history_is_recovered_when_present(tmp_path):
    document = run(tmp_path, project={"with_history": True})
    assert document.offloaded_context
    assert any("подготовь окружение" in message for message in document.user_messages)


def test_missing_audit_is_reported_rather_than_failing(tmp_path):
    document = run(tmp_path, project={"with_audit": False})
    assert any("no audit file" in warning for warning in document.warnings)
    assert document.steps


def test_document_json_is_serializable_and_carries_stats(tmp_path):
    document = run(tmp_path)
    payload = json.loads(json.dumps(document.to_json(), ensure_ascii=False))
    assert payload["schema_version"] == 1
    assert payload["stats"]["steps"] == len(document.steps)
    assert payload["stats"]["failed_steps"] >= 1


def test_discover_threads_lists_audit_only_threads(tmp_path):
    home, working_dir = build_project(tmp_path)
    rows = discover_threads(ExtractionRequest(working_dir=working_dir, home=home))
    assert rows == [
        {
            "thread_id": THREAD,
            "agent": "Quantizer",
            "namespaces": [],
            "latest_checkpoint_id": "",
            "has_audit": True,
        }
    ]


def test_cli_writes_json_and_reports_success(tmp_path, capsys):
    home, working_dir = build_project(tmp_path)
    trace = write_trace(tmp_path)
    out = tmp_path / "steps.json"

    code = main(
        [
            "--working-dir",
            str(working_dir),
            "--home",
            str(home),
            "--thread-id",
            THREAD,
            "--trace-file",
            str(trace),
            "--out",
            str(out),
        ]
    )

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["thread_id"] == THREAD
    assert payload["steps"]
    assert "steps       :" in capsys.readouterr().err


def test_cli_rejects_an_unknown_source(tmp_path, capsys):
    assert main(["--source", "nope", "--working-dir", str(tmp_path)]) == 2
    assert "unknown source" in capsys.readouterr().err


def test_cli_lists_threads(tmp_path, capsys):
    home, working_dir = build_project(tmp_path)
    code = main(["--list-threads", "--working-dir", str(working_dir), "--home", str(home)])
    assert code == 0
    assert THREAD in capsys.readouterr().out


def test_cli_returns_one_when_nothing_was_extracted(tmp_path, capsys):
    home, working_dir = build_project(tmp_path, with_audit=False)
    code = main(["--working-dir", str(working_dir), "--home", str(home), "--quiet"])
    assert code == 1
    capsys.readouterr()
