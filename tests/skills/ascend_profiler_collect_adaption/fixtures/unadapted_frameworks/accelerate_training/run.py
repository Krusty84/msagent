from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
import torch.nn.functional as F
from accelerate import Accelerator
from torch import nn
from torch.utils.data import DataLoader, TensorDataset


class TinyModel(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.layers = nn.Sequential(nn.Linear(4, 8), nn.ReLU(), nn.Linear(8, 2))

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.layers(inputs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()

    torch.manual_seed(11)
    accelerator = Accelerator()
    model = TinyModel()
    optimizer = torch.optim.SGD(model.parameters(), lr=1e-2)
    inputs = torch.arange(8, dtype=torch.float32).reshape(2, 4) / 8
    labels = torch.tensor([[0.25, -0.25], [0.5, -0.5]], dtype=torch.float32)
    loader = DataLoader(TensorDataset(inputs, labels), batch_size=1, shuffle=False)
    model, optimizer, loader = accelerator.prepare(model, optimizer, loader)

    losses: list[float] = []
    for batch_inputs, batch_labels in loader:
        predictions = model(batch_inputs)
        loss = F.mse_loss(predictions, batch_labels)
        accelerator.backward(loss)
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        losses.append(float(loss.detach().float().cpu()))

    unwrapped = accelerator.unwrap_model(model)
    checksum = sum(float(parameter.detach().float().cpu().sum()) for parameter in unwrapped.parameters())
    payload = {
        "framework": "accelerate",
        "steps": len(losses),
        "losses": losses,
        "checksum": checksum,
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
