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
    loop_type: str
    calls: list[str]
    score: int
    confidence: str
    evidence: list[dict[str, object]]


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
        collector = DirectCallCollector()
        if isinstance(node, (ast.For, ast.AsyncFor)):
            collector.visit(node.iter)
        else:
            collector.visit(node.test)
        for statement in (*node.body, *node.orelse):
            collector.visit(statement)
        calls = sorted(collector.calls)
        evidence: list[dict[str, object]] = []
        for call in calls:
            matches = [
                (suffix, weight)
                for suffix, weight in CALL_WEIGHTS.items()
                if call == suffix or call.endswith(f".{suffix}")
            ]
            if matches:
                suffix, weight = max(matches, key=lambda item: item[1])
                evidence.append({"call": call, "matched": suffix, "weight": weight})
        score = sum(int(item["weight"]) for item in evidence)
        if score:
            self.candidates.append(
                Candidate(
                    path=self.relative_path,
                    line=node.lineno,
                    scope=".".join(self.scope) or "<module>",
                    loop_type=type(node).__name__,
                    calls=calls,
                    score=score,
                    confidence="high"
                    if score >= 10
                    else "medium"
                    if score >= 6
                    else "low",
                    evidence=evidence,
                )
            )


class DirectCallCollector(ast.NodeVisitor):
    """Collect calls owned by one loop without descending into nested execution units."""

    def __init__(self) -> None:
        self.calls: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        name = call_name(node)
        if name:
            self.calls.add(name)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        return

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        return

    def visit_While(self, node: ast.While) -> None:
        return

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def discover(root: Path) -> tuple[list[Candidate], list[dict[str, str]]]:
    candidates: list[Candidate] = []
    errors: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        relative_path = path.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative_path.parts[:-1]):
            continue
        relative = str(relative_path)
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
                f"{item.score:>3} {item.confidence:<6} {item.path}:{item.line} "
                f"{item.scope} {item.loop_type} [{', '.join(item.calls)}]"
            )
        if errors:
            print(f"warning: skipped {len(errors)} unreadable or invalid Python files")
    return 0 if selected else 2


if __name__ == "__main__":
    raise SystemExit(main())
