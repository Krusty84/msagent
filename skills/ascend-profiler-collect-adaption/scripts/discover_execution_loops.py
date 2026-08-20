#!/usr/bin/env python3
"""Rank Python loops that are likely to execute model work."""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path

CALL_WEIGHTS = {
    "backward": 6,
    "optimizer.step": 6,
    "execute_model": 6,
    "generate": 5,
    "training_step": 5,
    "forward": 4,
    "model": 3,
    "step": 2,
}
IGNORED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "site-packages",
    "__pycache__",
}


@dataclass
class Candidate:
    path: str
    line: int
    scope: str
    calls: list[str]
    score: int


def call_name(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.AST = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


class LoopVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scope: list[str] = []
        self.candidates: list[Candidate] = []

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_For(self, node: ast.For) -> None:
        self._record_loop(node)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._record_loop(node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._record_loop(node)
        self.generic_visit(node)

    def _record_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        calls = sorted(
            {call_name(item) for item in ast.walk(node) if isinstance(item, ast.Call)}
        )
        score = 0
        for call in calls:
            for suffix, weight in CALL_WEIGHTS.items():
                if call == suffix or call.endswith(f".{suffix}"):
                    score += weight
        if score:
            self.candidates.append(
                Candidate(
                    path=self.relative_path,
                    line=node.lineno,
                    scope=".".join(self.scope) or "<module>",
                    calls=calls,
                    score=score,
                )
            )


def discover(root: Path) -> tuple[list[Candidate], list[dict[str, str]]]:
    candidates: list[Candidate] = []
    errors: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        relative = str(path.relative_to(root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            errors.append({"path": relative, "error": str(exc)})
            continue
        visitor = LoopVisitor(relative)
        visitor.visit(tree)
        candidates.extend(visitor.candidates)
    candidates.sort(key=lambda item: (-item.score, item.path, item.line))
    return candidates, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.repository.is_dir():
        parser.error(f"repository does not exist: {args.repository}")
    candidates, errors = discover(args.repository.resolve())
    selected = candidates[: max(args.limit, 0)]
    if args.json:
        print(
            json.dumps(
                {"candidates": [asdict(item) for item in selected], "errors": errors},
                indent=2,
            )
        )
    else:
        for item in selected:
            print(
                f"{item.score:>3} {item.path}:{item.line} {item.scope} [{', '.join(item.calls)}]"
            )
        if errors:
            print(f"warning: skipped {len(errors)} unreadable or invalid Python files")
    return 0 if selected else 2


if __name__ == "__main__":
    raise SystemExit(main())
