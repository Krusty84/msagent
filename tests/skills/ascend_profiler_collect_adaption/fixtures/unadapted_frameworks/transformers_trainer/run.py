from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.data import Dataset
from transformers import Trainer, TrainingArguments


class TinyDataset(Dataset):
    def __init__(self) -> None:
        generator = torch.Generator().manual_seed(20260820)
        self.input_ids = torch.randint(0, 16, (4, 4), generator=generator)
        self.labels = torch.randn(4, 2, generator=generator)

    def __len__(self) -> int:
        return len(self.input_ids)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        return {"input_ids": self.input_ids[index], "labels": self.labels[index]}


class TinyModel(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.embedding = nn.Embedding(16, 4)
        self.projection = nn.Linear(4, 2)

    def forward(
        self, input_ids: torch.Tensor, labels: torch.Tensor | None = None
    ) -> dict[str, torch.Tensor]:
        logits = self.projection(self.embedding(input_ids).mean(dim=1))
        loss = F.mse_loss(logits, labels) if labels is not None else logits.sum() * 0
        return {"loss": loss, "logits": logits}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()

    torch.manual_seed(7)
    model = TinyModel()
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(args.result.parent / "trainer-output"),
            max_steps=2,
            per_device_train_batch_size=1,
            learning_rate=1e-3,
            report_to="none",
            save_strategy="no",
            logging_strategy="no",
            remove_unused_columns=False,
            seed=7,
        ),
        train_dataset=TinyDataset(),
    )
    train_output = trainer.train()
    checksum = sum(float(parameter.detach().float().cpu().sum()) for parameter in model.parameters())
    payload = {
        "framework": "transformers",
        "steps": int(train_output.global_step),
        "training_loss": float(train_output.training_loss),
        "checksum": checksum,
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
