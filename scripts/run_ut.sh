#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
TEST_TARGETS=(
  "${REPO_ROOT}/tests/ut"
  "${REPO_ROOT}/tests/skills"
)

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to run unit tests." >&2
  exit 1
fi

cd "${REPO_ROOT}"
echo "[run_ut] Running unit and Skill tests with a 1-second per-test timeout..."
uv run --with pytest-timeout pytest -q "${TEST_TARGETS[@]}" "$@" --timeout=1
