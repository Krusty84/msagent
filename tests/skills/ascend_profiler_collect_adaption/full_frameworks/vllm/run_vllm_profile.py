from __future__ import annotations

import argparse
import json
from pathlib import Path

from vllm import LLM, SamplingParams

PROMPTS = [
    "The capital of France is",
    "One plus one equals",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path)
    args = parser.parse_args()

    profiler_config = None
    if args.profile_dir is not None:
        profiler_config = {
            "profiler": "torch",
            "torch_profiler_dir": str(args.profile_dir),
            "torch_profiler_with_stack": False,
            "torch_profiler_with_memory": False,
        }

    llm = LLM(
        model=str(args.model),
        dtype="bfloat16",
        max_model_len=256,
        gpu_memory_utilization=0.2,
        enforce_eager=True,
        disable_custom_all_reduce=True,
        seed=123,
        profiler_config=profiler_config,
    )
    sampling = SamplingParams(temperature=0.0, max_tokens=16, seed=123)

    # Exercise the complete engine before collection so model loading and first-use
    # initialization do not replace the actual inference interval.
    llm.generate(["warmup"], sampling, use_tqdm=False)

    if args.profile_dir is not None:
        llm.start_profile(profile_prefix="vllm_complete")
    try:
        outputs = llm.generate(PROMPTS, sampling, use_tqdm=False)
    finally:
        if args.profile_dir is not None:
            llm.stop_profile()

    payload = {
        "prompts": PROMPTS,
        "outputs": [
            {
                "text": request.outputs[0].text,
                "token_ids": list(request.outputs[0].token_ids),
                "finish_reason": request.outputs[0].finish_reason,
            }
            for request in outputs
        ],
    }
    args.result.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
