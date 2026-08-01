#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
UT_TARGET="${REPO_ROOT}/tests/ut"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to run unit tests." >&2
  exit 1
fi

cd "${REPO_ROOT}"
echo "[run_ut] Running unit tests..."
uv run pytest -q "${UT_TARGET}" "$@"
