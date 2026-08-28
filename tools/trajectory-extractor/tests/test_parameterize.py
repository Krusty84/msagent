from trajectory_extractor.models import Step
from trajectory_extractor.parameterize import parameterize


def step(index, tool, args, result_preview=""):
    return Step(index=index, tool=tool, args_raw=dict(args), args=dict(args), result_preview=result_preview)


def placeholders(parameters):
    return {parameter.placeholder for parameter in parameters}


def test_a_whole_value_takes_its_placeholder_name_from_the_argument_key():
    steps = [step(1, "quantize", {"model_path": "/data/models/qwen3"})]
    parameters = parameterize(steps)

    assert steps[0].args["model_path"] == "<MODEL_PATH>"
    assert "<MODEL_PATH>" in placeholders(parameters)


def test_the_same_value_maps_to_the_same_placeholder_across_steps():
    steps = [
        step(1, "quantize", {"model_path": "/data/models/qwen3"}),
        step(2, "execute", {"command": "ls /data/models/qwen3/config.json"}),
    ]
    parameterize(steps)
    assert "<MODEL_PATH>" in steps[1].args["command"]
    assert "/data/models/qwen3" not in steps[1].args["command"]


def test_generic_argument_keys_fall_back_to_a_kind_prefixed_name():
    steps = [step(1, "execute", {"command": "cat /var/log/run/output.log"})]
    parameters = parameterize(steps)
    assert "<PATH_1>" in steps[0].args["command"]
    assert any(parameter.kind == "path" for parameter in parameters)


def test_longer_values_are_substituted_before_the_prefixes_they_contain():
    steps = [
        step(1, "a", {"root": "/data/run"}),
        step(2, "b", {"nested": "/data/run/logs/trace.txt"}),
    ]
    parameterize(steps)
    assert steps[1].args["nested"] == "<NESTED>"
    assert steps[0].args["root"] == "<ROOT>"


def test_paths_win_over_versions_nested_inside_them():
    steps = [step(1, "execute", {"command": "source /opt/cann/8.0.RC3/set_env.sh"})]
    parameters = parameterize(steps)
    kinds = {parameter.kind for parameter in parameters}
    assert "path" in kinds
    assert "version" not in kinds


def test_urls_and_addresses_are_lifted():
    steps = [
        step(1, "fetch", {"endpoint": "https://api.example.org/v1/models"}),
        step(2, "ping", {"target": "10.0.0.14:8080"}),
    ]
    parameters = parameterize(steps)
    kinds = {parameter.kind for parameter in parameters}
    assert {"url", "ip"} <= kinds


def test_device_ordinals_are_replaced_in_context_not_by_bare_value():
    steps = [
        step(1, "execute", {"command": "python run.py --device 3 --batch 3"}),
    ]
    parameters = parameterize(steps)
    command = steps[0].args["command"]

    assert "--device <DEVICE_IDS>" in command
    assert "--batch 3" in command
    device = next(parameter for parameter in parameters if parameter.placeholder == "<DEVICE_IDS>")
    assert device.values == ["3"]


def test_visible_devices_environment_variable_is_covered():
    steps = [step(1, "execute", {"command": "ASCEND_RT_VISIBLE_DEVICES=0,1,2,3 bash run.sh"})]
    parameterize(steps)
    assert "ASCEND_RT_VISIBLE_DEVICES=<DEVICE_IDS>" in steps[0].args["command"]


def test_result_previews_are_parameterized_so_data_flow_stays_visible():
    steps = [
        step(1, "locate", {"where": "/srv/output/run01"}, result_preview="written to /srv/output/run01"),
    ]
    parameterize(steps)
    assert "<WHERE>" in steps[0].result_preview


def test_result_parameterization_can_be_disabled():
    steps = [step(1, "locate", {"where": "/srv/output/run01"}, result_preview="written to /srv/output/run01")]
    parameterize(steps, include_results=False)
    assert steps[0].result_preview == "written to /srv/output/run01"


def test_raw_arguments_are_preserved_alongside_the_parameterized_form():
    steps = [step(1, "quantize", {"model_path": "/data/models/qwen3"})]
    parameterize(steps)
    assert steps[0].args_raw["model_path"] == "/data/models/qwen3"


def test_placeholder_names_stay_unique_for_distinct_values():
    steps = [
        step(1, "a", {"path": "/data/first/run"}),
        step(2, "b", {"path": "/data/second/run"}),
    ]
    parameters = parameterize(steps)
    names = [parameter.placeholder for parameter in parameters]
    assert len(names) == len(set(names))


def test_short_values_are_left_alone():
    steps = [step(1, "execute", {"command": "cd /a/b"})]
    parameters = parameterize(steps)
    assert parameters == []
    assert steps[0].args["command"] == "cd /a/b"


def test_nested_structures_are_rewritten():
    steps = [step(1, "batch", {"jobs": [{"artifact_dir": "/data/models/qwen3"}]})]
    parameterize(steps)
    assert steps[0].args["jobs"][0]["artifact_dir"] == "<ARTIFACT_DIR>"


def test_a_child_path_reuses_its_parent_placeholder_instead_of_a_new_one():
    steps = [
        step(1, "quantize", {"model_path": "/data/models/qwen3"}),
        step(2, "execute", {"command": "ls /data/models/qwen3/config.json"}),
    ]
    parameters = parameterize(steps)

    assert steps[1].args["command"] == "ls <MODEL_PATH>/config.json"
    assert [parameter.placeholder for parameter in parameters] == ["<MODEL_PATH>"]


def test_a_redacted_home_path_stays_one_placeholder():
    steps = [step(1, "execute", {"command": "bash /home/<USER>/bench/run_eval.sh"})]
    parameters = parameterize(steps)

    assert steps[0].args["command"] == "bash <PATH_1>"
    assert parameters[0].values == ["/home/<USER>/bench/run_eval.sh"]
