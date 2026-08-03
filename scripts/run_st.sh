#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
TEST_TARGETS=(
  "${REPO_ROOT}/tests/benchmark"
  "${REPO_ROOT}/tests/e2e"
  "${REPO_ROOT}/tests/test_benchmark_runner.py"
)

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to run system tests." >&2
  exit 1
fi

cd "${REPO_ROOT}"
echo "[run_st] Running benchmark and end-to-end tests..."
uv run pytest -q "${TEST_TARGETS[@]}" "$@"
