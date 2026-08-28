import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from trajectory_extractor.sources.audit import find_audit_file, list_audit_threads, read_audit
from trajectory_extractor.sources.checkpoint import list_threads, messages_to_events
from trajectory_extractor.sources.history import read_history
from trajectory_extractor.sources.locate import _fallback_project_id, resolve_project
from trajectory_extractor.sources.trace import read_trace

# --- trace ------------------------------------------------------------------


def write_trace(path, events):
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def test_read_trace_extracts_session_metadata_and_events(tmp_path):
    path = tmp_path / "run.jsonl"
    write_trace(
        path,
        [
            {"index": 1, "type": "session_started", "agent": "Profiler", "thread_id": "t-1"},
            {"index": 2, "type": "assistant_message", "content": "planning", "tool_call_count": 1},
            {"index": 3, "type": "tool_call", "tool": "execute", "item_id": "c1", "input": {"command": "ls"}},
            {
                "index": 4,
                "type": "tool_result",
                "tool": "execute",
                "item_id": "c1",
                "output": {"content": "a.txt", "is_error": False},
                "duration_ms": 12,
            },
        ],
    )

    result = read_trace(path)
    assert result.thread_id == "t-1"
    assert result.agent == "Profiler"
    kinds = [event.kind for event in result.events]
    assert kinds == ["assistant", "tool_call", "tool_result"]
    assert result.events[1].args == {"command": "ls"}
    assert result.events[2].duration_ms == 12


def test_read_trace_marks_subagent_origin(tmp_path):
    path = tmp_path / "run.jsonl"
    write_trace(
        path,
        [{"index": 1, "type": "tool_call", "tool": "grep", "item_id": "c", "input": {}, "origin": "subagent"}],
    )
    assert read_trace(path).events[0].origin == "subagent"


def test_read_trace_warns_about_truncation(tmp_path):
    path = tmp_path / "run.jsonl"
    write_trace(
        path,
        [
            {
                "index": 1,
                "type": "tool_result",
                "tool": "execute",
                "item_id": "c",
                "output": {"content": "x", "content_truncated": True},
            }
        ],
    )
    assert any("truncated" in warning for warning in read_trace(path).warnings)


def test_read_trace_always_warns_that_user_messages_are_absent(tmp_path):
    path = tmp_path / "run.jsonl"
    write_trace(path, [{"index": 1, "type": "session_started", "thread_id": "t"}])
    assert any("no user messages" in warning for warning in read_trace(path).warnings)


def test_read_trace_skips_malformed_lines(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text('{"index":1,"type":"session_started"}\nnot json\n', encoding="utf-8")
    result = read_trace(path)
    assert any("malformed JSON" in warning for warning in result.warnings)


def test_read_trace_reports_a_missing_file(tmp_path):
    result = read_trace(tmp_path / "absent.jsonl")
    assert result.is_empty
    assert any("not found" in warning for warning in result.warnings)


# --- audit ------------------------------------------------------------------


def write_audit(path, payloads):
    blocks = [json.dumps(payload, ensure_ascii=False, indent=2) for payload in payloads]
    path.write_text("\n".join(blocks) + "\n", encoding="utf-8")


def test_read_audit_parses_pretty_printed_blocks(tmp_path):
    path = tmp_path / "Quantizer_thread-9.jsonl"
    write_audit(
        path,
        [
            {"agent_name": "Quantizer", "event": "user.turn", "run_id": "r1", "message": "квантуй модель"},
            {
                "agent_name": "Quantizer",
                "event": "subagent.delegation",
                "subagent_type": "quant-tuning-quantizer",
                "run_id": "r1",
                "delegation_id": "d1",
                "status": "ok",
                "duration_ms": 4200,
                "task_description_raw": "quantize with w8a8",
                "input": {"model": "qwen3"},
                "output": {"accuracy": 0.91},
            },
        ],
    )

    result = read_audit(path)
    assert result.thread_id == "thread-9"
    assert result.agent == "Quantizer"
    assert result.user_messages == ["квантуй модель"]
    assert len(result.phases) == 1
    assert result.phases[0].name == "quant-tuning-quantizer"
    assert result.phases[0].output == {"accuracy": 0.91}


def test_read_audit_records_human_decisions(tmp_path):
    path = tmp_path / "Profiler_t.jsonl"
    write_audit(
        path,
        [
            {
                "agent_name": "Profiler",
                "event": "user.response",
                "run_id": "r",
                "start_time": "2026-08-28 10:00:00",
                "kind": "approval",
                "prompt": "run rm -rf build?",
                "response": "reject",
            }
        ],
    )
    phases = read_audit(path).phases
    assert phases[0].name == "human-decision"
    assert phases[0].output["response"] == "reject"


def test_read_audit_warns_when_no_delegations_are_present(tmp_path):
    path = tmp_path / "Profiler_t.jsonl"
    write_audit(path, [{"event": "user.turn", "message": "hi", "agent_name": "Profiler", "run_id": "r"}])
    assert any("audit_log" in warning for warning in read_audit(path).warnings)


def test_find_audit_file_matches_the_agent_prefix(tmp_path):
    (tmp_path / "Quantizer_abc.jsonl").write_text("{}", encoding="utf-8")
    assert find_audit_file(tmp_path, "abc").name == "Quantizer_abc.jsonl"
    assert find_audit_file(tmp_path, "missing") is None


def test_find_audit_file_supports_the_legacy_name(tmp_path):
    (tmp_path / "abc.jsonl").write_text("{}", encoding="utf-8")
    assert find_audit_file(tmp_path, "abc").name == "abc.jsonl"


def test_list_audit_threads_splits_agent_and_thread(tmp_path):
    (tmp_path / "Quantizer_t1.jsonl").write_text("{}", encoding="utf-8")
    (tmp_path / "t2.jsonl").write_text("{}", encoding="utf-8")
    entries = {thread: agent for thread, agent, _path in list_audit_threads(tmp_path)}
    assert entries == {"t1": "Quantizer", "t2": ""}


# --- history ----------------------------------------------------------------


def test_read_history_splits_offloaded_sections(tmp_path):
    path = tmp_path / "t.md"
    path.write_text(
        "## Offloaded at 2026-08-28T10:00:00+00:00\n\n"
        "Human: analyse the profile\nAI: starting\n\n"
        "## Offloaded at 2026-08-28T11:00:00+00:00\n\n"
        "Human: now compare runs\nTool: done\n",
        encoding="utf-8",
    )

    result = read_history(path)
    assert len(result.offloaded_context) == 2
    assert result.user_messages == ["analyse the profile", "now compare runs"]
    assert any("compacted" in warning for warning in result.warnings)


def test_read_history_keeps_message_bodies_that_contain_colons(tmp_path):
    path = tmp_path / "t.md"
    path.write_text(
        "## Offloaded at x\n\nHuman: check this\nNote: keep it\nAI: fine\n",
        encoding="utf-8",
    )
    assert read_history(path).user_messages == ["check this\nNote: keep it"]


def test_read_history_on_a_missing_file_is_empty(tmp_path):
    assert read_history(tmp_path / "absent.md").is_empty


# --- locate -----------------------------------------------------------------


def test_project_id_matches_the_msagent_layout(tmp_path):
    home = tmp_path / "home"
    working_dir = tmp_path / "workspace"
    working_dir.mkdir()

    location = resolve_project(working_dir, home=home)
    expected = home.resolve() / "state" / "projects" / _fallback_project_id(working_dir)
    assert location.root == expected


def test_location_exposes_the_expected_files(tmp_path):
    location = resolve_project(tmp_path, home=tmp_path / "home")
    assert location.checkpoints_db.name == "checkpoints.sqlite"
    assert location.audit_dir.name == "audit_log"
    assert location.conversation_history_dir.name == "conversation_history"


# --- checkpoint -------------------------------------------------------------


@dataclass
class StubMessage:
    type: str
    content: Any = ""
    tool_calls: list = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    status: str | None = None


def test_messages_to_events_converts_a_full_exchange():
    messages = [
        StubMessage(type="system", content="you are an agent"),
        StubMessage(type="human", content="analyse /data/prof"),
        StubMessage(
            type="ai",
            content="looking",
            tool_calls=[{"name": "execute", "id": "c1", "args": {"command": "ls"}}],
        ),
        StubMessage(type="tool", content="a.txt", tool_call_id="c1", name="execute"),
    ]

    events, user_messages = messages_to_events(messages)
    assert [event.kind for event in events] == ["user", "assistant", "tool_call", "tool_result"]
    assert user_messages == ["analyse /data/prof"]
    assert events[2].args == {"command": "ls"}
    assert events[3].ok is True


def test_messages_to_events_marks_tool_errors():
    messages = [StubMessage(type="tool", content="boom", tool_call_id="c", status="error")]
    events, _ = messages_to_events(messages)
    assert events[0].ok is False


def test_messages_to_events_flattens_block_content():
    messages = [StubMessage(type="ai", content=[{"type": "text", "text": "first"}, {"type": "text", "text": "second"}])]
    events, _ = messages_to_events(messages)
    assert events[0].text == "first\nsecond"


def test_messages_to_events_accepts_plain_dictionaries():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "", "tool_calls": [{"name": "grep", "id": "c", "args": {"q": "x"}}]},
    ]
    events, user_messages = messages_to_events(messages)
    assert user_messages == ["hello"]
    assert events[1].tool == "grep"


def test_messages_to_events_infers_tool_role_from_a_call_id():
    messages = [{"content": "output", "tool_call_id": "c1"}]
    events, _ = messages_to_events(messages)
    assert events[0].kind == "tool_result"


def test_messages_to_events_tags_origin_and_namespace():
    messages = [StubMessage(type="ai", content="x")]
    events, _ = messages_to_events(messages, origin="subagent", namespace="task:1")
    assert events[0].origin == "subagent"
    assert events[0].namespace == "task:1"


def make_checkpoint_db(path, rows):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE checkpoints ("
        "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
        "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB)"
    )
    connection.executemany("INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id) VALUES (?, ?, ?)", rows)
    connection.commit()
    connection.close()


def test_list_threads_groups_namespaces_newest_first(tmp_path):
    database = tmp_path / "checkpoints.sqlite"
    make_checkpoint_db(
        database,
        [
            ("t-old", "", "0001"),
            ("t-new", "", "0009"),
            ("t-new", "task:abc", "0008"),
        ],
    )

    threads = list_threads(database)
    assert [thread.thread_id for thread in threads] == ["t-new", "t-old"]
    assert threads[0].namespaces == ("", "task:abc")
    assert threads[0].latest_checkpoint_id == "0009"


def test_list_threads_on_a_missing_or_foreign_database(tmp_path):
    assert list_threads(tmp_path / "absent.sqlite") == []
    stray = tmp_path / "stray.sqlite"
    stray.write_text("not a database", encoding="utf-8")
    assert list_threads(stray) == []


def test_read_audit_omits_options_when_none_were_offered(tmp_path):
    path = tmp_path / "Profiler_t.jsonl"
    write_audit(
        path,
        [
            {
                "agent_name": "Profiler",
                "event": "user.response",
                "run_id": "r",
                "kind": "approval",
                "prompt": "run it?",
                "response": "approve",
            }
        ],
    )
    assert read_audit(path).phases[0].output == {"response": "approve"}
