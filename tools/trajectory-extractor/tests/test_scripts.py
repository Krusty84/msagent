from trajectory_extractor.models import Step
from trajectory_extractor.scripts import command_text, find_script_candidates, is_shell_step


def shell(index, command, repeat=1, tool="execute"):
    return Step(
        index=index,
        tool=tool,
        args={"command": command},
        args_raw={"command": command},
        repeat_count=repeat,
    )


def test_shell_steps_are_recognized_by_tool_name():
    assert is_shell_step(Step(index=1, tool="execute"))
    assert is_shell_step(Step(index=1, tool="msprof-mcp__run_command"))
    assert not is_shell_step(Step(index=1, tool="read_file"))


def test_shell_steps_are_recognized_by_a_command_argument():
    assert is_shell_step(Step(index=1, tool="custom", args={"command": "ls"}))


def test_command_text_prefers_the_first_populated_key():
    assert command_text(Step(index=1, tool="execute", args={"cmd": "ls -la"})) == "ls -la"
    assert command_text(Step(index=1, tool="execute", args={})) == ""


def test_a_repeated_command_becomes_a_candidate():
    steps = [shell(1, "bash run_eval.sh"), shell(2, "other"), shell(3, "bash run_eval.sh")]
    candidates = find_script_candidates(steps)
    repeated = [item for item in candidates if item.reason == "repeated"]

    assert len(repeated) == 1
    assert repeated[0].template == "bash run_eval.sh"
    assert repeated[0].occurrences == 2
    assert repeated[0].step_indices == [1, 3]


def test_collapsed_retries_count_towards_occurrences():
    candidates = find_script_candidates([shell(1, "bash run_eval.sh", repeat=3)])
    assert [item.occurrences for item in candidates if item.reason == "repeated"] == [3]


def test_a_multiline_command_is_a_candidate_on_its_own():
    steps = [shell(1, "python - <<'EOF'\nprint(1)\nEOF")]
    candidates = find_script_candidates(steps)
    assert any(item.reason == "multiline" and item.multiline for item in candidates)


def test_a_long_command_is_a_candidate_on_its_own():
    steps = [shell(1, "python train.py " + "--flag value " * 20)]
    assert any(item.reason == "long" for item in find_script_candidates(steps))


def test_a_run_of_shell_steps_is_reported_as_one_sequence():
    steps = [shell(1, "step one"), shell(2, "step two"), shell(3, "step three")]
    sequences = [item for item in find_script_candidates(steps) if item.reason == "sequence"]

    assert len(sequences) == 1
    assert sequences[0].step_indices == [1, 2, 3]
    assert sequences[0].template.splitlines() == ["step one", "step two", "step three"]


def test_a_short_run_is_not_a_sequence():
    steps = [shell(1, "step one"), shell(2, "step two")]
    assert not [item for item in find_script_candidates(steps) if item.reason == "sequence"]


def test_a_non_shell_step_breaks_a_sequence():
    steps = [
        shell(1, "one"),
        shell(2, "two"),
        Step(index=3, tool="read_file", args={"path": "/a/b"}),
        shell(4, "three"),
    ]
    assert not [item for item in find_script_candidates(steps) if item.reason == "sequence"]


def test_a_unique_short_command_is_not_a_candidate():
    assert find_script_candidates([shell(1, "ls")]) == []


def test_the_repeat_threshold_is_configurable():
    steps = [shell(1, "ls"), shell(2, "ls")]
    assert find_script_candidates(steps, min_occurrences=3) == []
    assert find_script_candidates(steps, min_occurrences=2)
