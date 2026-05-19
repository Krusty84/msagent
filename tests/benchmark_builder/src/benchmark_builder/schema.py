from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _load_yaml_or_json(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return _load_simple_case_yaml(text, path)

    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping at top level: {path}")
    return loaded


def _load_simple_case_yaml(text: str, path: Path) -> dict[str, Any]:
    """Tiny fallback parser for the simple case YAML used by this project."""
    result: dict[str, Any] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if line.startswith((" ", "\t")) or ":" not in line:
            raise ValueError(f"Unsupported YAML shape in {path}: {line!r}")

        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value in {">", "|"}:
            block_lines: list[str] = []
            index += 1
            while index < len(lines) and (
                lines[index].startswith((" ", "\t")) or not lines[index].strip()
            ):
                block_lines.append(lines[index].strip())
                index += 1
            result[key] = "\n".join(block_lines) if value == "|" else " ".join(block_lines)
            continue

        if value == "":
            list_items: list[str] = []
            index += 1
            while index < len(lines) and (
                lines[index].startswith((" ", "\t")) or not lines[index].strip()
            ):
                item = lines[index].strip()
                if item.startswith("- "):
                    list_items.append(item[2:].strip().strip("\"'"))
                elif item:
                    raise ValueError(f"Unsupported YAML list item in {path}: {lines[index]!r}")
                index += 1
            result[key] = list_items
            continue

        if value == "[]":
            result[key] = []
            index += 1
            continue

        result[key] = value.strip("\"'")
        index += 1

    return result


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    input_data_path: str
    prompt: str
    ground_truth: list[str]
    source_path: Path = field(repr=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source_path: Path) -> "BenchmarkCase":
        missing = [
            key
            for key in ("id", "input_data_path", "prompt", "ground_truth")
            if key not in raw
        ]
        if missing:
            raise ValueError(f"{source_path} is missing required field(s): {', '.join(missing)}")

        return cls(
            id=str(raw["id"]),
            input_data_path=str(raw["input_data_path"]),
            prompt=str(raw["prompt"]),
            ground_truth=_normalize_ground_truth(raw["ground_truth"], source_path),
            source_path=source_path,
        )

    def resolve_input_path(self) -> Path:
        path = Path(self.input_data_path)
        if path.is_absolute():
            return path
        return (self.source_path.parent / path).resolve()


@dataclass(frozen=True)
class BenchmarkSuite:
    name: str
    config_path: Path
    cases: list[BenchmarkCase]


def load_suite(config_path: str | Path) -> BenchmarkSuite:
    path = Path(config_path).resolve()
    if path.is_dir():
        case_files = sorted([*path.glob("*.yaml"), *path.glob("*.yml")])
        cases = [_load_case_file(case_file) for case_file in case_files]
        name = path.name
    else:
        cases = [_load_case_file(path)]
        name = path.stem

    if not cases:
        raise ValueError(f"No benchmark case YAML files found in {path}")

    return BenchmarkSuite(
        name=name,
        config_path=path,
        cases=cases,
    )


def _load_case_file(path: Path) -> BenchmarkCase:
    raw = _load_yaml_or_json(path)
    if "cases" in raw:
        raise ValueError(f"{path} contains a suite. Expected one case per YAML file.")
    return BenchmarkCase.from_dict(raw, path)


def _normalize_ground_truth(value: Any, source_path: Path) -> list[str]:
    if isinstance(value, str):
        if not value.strip():
            return []
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError(f"{source_path} ground_truth must be a list of card ids.")
