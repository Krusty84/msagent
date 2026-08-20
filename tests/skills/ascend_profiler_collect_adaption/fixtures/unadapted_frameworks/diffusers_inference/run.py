from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from diffusers import DDPMScheduler, UNet2DModel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()

    torch.manual_seed(13)
    model = UNet2DModel(
        sample_size=8,
        in_channels=3,
        out_channels=3,
        layers_per_block=1,
        block_out_channels=(16,),
        down_block_types=("DownBlock2D",),
        up_block_types=("UpBlock2D",),
        norm_num_groups=4,
    ).eval().npu()
    scheduler = DDPMScheduler(num_train_timesteps=8)
    scheduler.set_timesteps(2)
    sample = torch.ones((1, 3, 8, 8), device="npu")

    with torch.no_grad():
        for timestep in scheduler.timesteps:
            predicted_noise = model(sample, timestep).sample
            sample = scheduler.step(predicted_noise, timestep, sample).prev_sample

    payload = {
        "framework": "diffusers",
        "steps": len(scheduler.timesteps),
        "checksum": float(sample.float().cpu().sum()),
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
