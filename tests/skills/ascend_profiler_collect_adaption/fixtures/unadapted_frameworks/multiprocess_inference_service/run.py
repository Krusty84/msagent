from __future__ import annotations

import json
import multiprocessing as mp


def worker_main(commands, results) -> None:
    import torch
    import torch_npu  # noqa: F401

    torch.manual_seed(2026)
    device = torch.device("npu:0")
    model = torch.nn.Linear(4, 4).to(device).eval()
    while True:
        command, payload = commands.get()
        if command == "shutdown":
            return
        if command != "infer":
            raise ValueError(f"unsupported command: {command}")
        inputs = torch.tensor(payload, dtype=torch.float32, device=device)
        with torch.no_grad():
            outputs = model(inputs)
        results.put(outputs.cpu().tolist())


def main() -> None:
    context = mp.get_context("spawn")
    commands = context.Queue()
    results = context.Queue()
    worker = context.Process(target=worker_main, args=(commands, results))
    worker.start()
    requests = [
        [[1.0, 2.0, 3.0, 4.0]],
        [[-1.0, 0.5, 2.0, -0.5]],
    ]
    for request in requests:
        commands.put(("infer", request))
    outputs = [results.get(timeout=60) for _ in requests]
    commands.put(("shutdown", None))
    worker.join(timeout=60)
    if worker.exitcode != 0:
        raise RuntimeError(f"worker exited with code {worker.exitcode}")
    checksum = sum(value for output in outputs for row in output for value in row)
    print(json.dumps({"outputs": outputs, "checksum": checksum}, sort_keys=True))


if __name__ == "__main__":
    main()
