from trajectory_extractor.models import RawEvent
from trajectory_extractor.normalize import build_steps, collapse_retries, detect_recoveries


def call(index, tool, args, call_id="", origin="main"):
    return RawEvent(kind="tool_call", index=index, tool=tool, args=args, call_id=call_id, origin=origin)


def result(index, text, call_id="", ok=True, tool=""):
    return RawEvent(kind="tool_result", index=index, text=text, call_id=call_id, ok=ok, tool=tool)


def test_build_steps_pairs_calls_and_results_by_id():
    events = [
        call(1, "execute", {"command": "ls"}, call_id="a"),
        result(2, "file.txt", call_id="a"),
    ]
    steps = build_steps(events)
    assert len(steps) == 1
    assert steps[0].tool == "execute"
    assert steps[0].result_preview == "file.txt"
    assert steps[0].ok is True


def test_build_steps_falls_back_to_tool_name_when_ids_are_absent():
    events = [
        call(1, "read_file", {"path": "/a/b/c"}),
        result(2, "content", tool="read_file"),
    ]
    steps = build_steps(events)
    assert steps[0].result_preview == "content"


def test_build_steps_marks_failures_and_captures_the_error():
    events = [
        call(1, "execute", {"command": "boom"}, call_id="x"),
        result(2, "command not found", call_id="x", ok=False),
    ]
    steps = build_steps(events)
    assert steps[0].ok is False
    assert steps[0].error == "command not found"


def test_call_without_a_result_still_becomes_a_step():
    steps = build_steps([call(1, "execute", {"command": "ls"}, call_id="a")])
    assert len(steps) == 1
    assert steps[0].result_preview == ""


def test_collapse_retries_merges_consecutive_identical_calls():
    events = []
    for position in range(3):
        identifier = f"c{position}"
        events.append(call(position * 2 + 1, "fetch", {"url": "https://x.test/a"}, call_id=identifier))
        events.append(result(position * 2 + 2, "timeout", call_id=identifier, ok=False))

    collapsed = collapse_retries(build_steps(events))
    assert len(collapsed) == 1
    assert collapsed[0].repeat_count == 3
    assert collapsed[0].ok is False


def test_collapse_retries_reports_a_run_that_eventually_succeeded():
    events = [
        call(1, "fetch", {"url": "https://x.test/a"}, call_id="a"),
        result(2, "timeout", call_id="a", ok=False),
        call(3, "fetch", {"url": "https://x.test/a"}, call_id="b"),
        result(4, "payload", call_id="b"),
    ]
    collapsed = collapse_retries(build_steps(events))
    assert len(collapsed) == 1
    assert collapsed[0].repeat_count == 2
    assert collapsed[0].ok is True
    assert collapsed[0].result_preview == "payload"
    assert collapsed[0].error is None


def test_collapse_retries_keeps_different_arguments_apart_and_renumbers():
    events = [
        call(1, "fetch", {"url": "https://x.test/a"}, call_id="a"),
        result(2, "ok", call_id="a"),
        call(3, "fetch", {"url": "https://x.test/b"}, call_id="b"),
        result(4, "ok", call_id="b"),
    ]
    collapsed = collapse_retries(build_steps(events))
    assert [step.index for step in collapsed] == [1, 2]


def test_detect_recoveries_finds_the_corrected_call():
    events = [
        call(1, "msopprof", {"kernel_name": "add_kernel"}, call_id="a"),
        result(2, "empty result directory", call_id="a", ok=False),
        call(3, "msopprof", {"kernel_name": "_ZN12_GLOBAL__N_1"}, call_id="b"),
        result(4, "OpBasicInfo.csv written", call_id="b"),
    ]
    steps = collapse_retries(build_steps(events))
    recoveries = detect_recoveries(steps)

    assert len(recoveries) == 1
    recovery = recoveries[0]
    assert recovery.tool == "msopprof"
    assert recovery.changed_args == ["kernel_name"]
    assert recovery.before == {"kernel_name": "add_kernel"}
    assert recovery.after == {"kernel_name": "_ZN12_GLOBAL__N_1"}
    assert "empty result directory" in recovery.error


def test_detect_recoveries_ignores_an_unrelated_later_success():
    events = [
        call(1, "alpha", {"x": 1}, call_id="a"),
        result(2, "boom", call_id="a", ok=False),
        call(3, "beta", {"x": 2}, call_id="b"),
        result(4, "fine", call_id="b"),
    ]
    steps = collapse_retries(build_steps(events))
    assert detect_recoveries(steps) == []


def test_detect_recoveries_respects_the_lookahead_window():
    events = [
        call(1, "alpha", {"x": 1}, call_id="a"),
        result(2, "boom", call_id="a", ok=False),
    ]
    for position in range(5):
        identifier = f"f{position}"
        events.append(call(10 + position * 2, "beta", {"n": position}, call_id=identifier))
        events.append(result(11 + position * 2, "fine", call_id=identifier))
    events.append(call(40, "alpha", {"x": 2}, call_id="z"))
    events.append(result(41, "fine", call_id="z"))

    steps = collapse_retries(build_steps(events))
    assert detect_recoveries(steps, window=2) == []
    assert len(detect_recoveries(steps, window=10)) == 1
