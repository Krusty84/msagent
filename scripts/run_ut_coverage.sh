#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/ut_coverage"
TEST_TARGETS=(
  "${REPO_ROOT}/tests/ut"
  "${REPO_ROOT}/tests/skills"
)
RCFILE="${OUTPUT_DIR}/.coveragerc"
DIFF_MODE=false
TARGET_BRANCH="master"

if [[ "${1:-}" == "diff" ]]; then
  DIFF_MODE=true
  TARGET_BRANCH="${2:-master}"
  shift
  if [[ $# -gt 0 && "${1}" != -* ]]; then
    shift
  fi
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to run unit test coverage." >&2
  exit 1
fi

mkdir -p "${OUTPUT_DIR}"
export COVERAGE_FILE="${OUTPUT_DIR}/.coverage"
rm -f "${COVERAGE_FILE}" "${RCFILE}" "${OUTPUT_DIR}/coverage.xml" "${OUTPUT_DIR}/junit.xml" "${OUTPUT_DIR}/python_coverage_report.log"

cleanup() {
  rm -f "${RCFILE}"
}
trap cleanup EXIT

cat > "${RCFILE}" <<EOF
[run]
branch = True
source =
    ${REPO_ROOT}/src/msagent
    ${REPO_ROOT}/skills

[report]
include_namespace_packages = True
EOF

cd "${REPO_ROOT}"
echo "[run_ut_coverage] Running unit and Skill tests with branch coverage..."
uv run --with coverage coverage run --rcfile="${RCFILE}" -m pytest -q "${TEST_TARGETS[@]}" "$@" --junitxml="${OUTPUT_DIR}/junit.xml"
uv run --with coverage coverage xml --rcfile="${RCFILE}" -o "${OUTPUT_DIR}/coverage.xml"
uv run --with coverage coverage report --rcfile="${RCFILE}" > "${OUTPUT_DIR}/python_coverage_report.log"

if [[ "${DIFF_MODE}" == "true" ]]; then
  uv run --with diff-cover diff-cover "${OUTPUT_DIR}/coverage.xml" \
    --compare-branch="origin/${TARGET_BRANCH}" \
    --html-report="${OUTPUT_DIR}/inc_coverage_result.html" \
    --fail-under=80
fi

echo "[run_ut_coverage] Reports written to: ${OUTPUT_DIR}"
