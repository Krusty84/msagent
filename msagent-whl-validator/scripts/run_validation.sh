#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

wheel_path=""
python_version="3.11"
conda_command="${CONDA_EXE:-conda}"
run_dir=""

usage() {
    cat <<'EOF'
Usage:
  ./scripts/run_validation.sh --wheel <path-to-whl> [options]

Required:
  --wheel PATH             mindstudio-agent whl to validate

Options:
  --python-version VERSION Python version for the clean Conda environment
                           (default: 3.11)
  --conda PATH             Conda executable (default: $CONDA_EXE or conda)
  --run-dir PATH           Empty directory for the environment and logs
  -h, --help               Show this help

This initial implementation performs three gated stages only:
  1. Create a clean Conda environment.
  2. Install the specified whl and its dependencies.
  3. Run pip check.
EOF
}

while (($# > 0)); do
    case "$1" in
        --wheel)
            [[ $# -ge 2 ]] || { echo "--wheel requires a value" >&2; exit 2; }
            wheel_path="$2"
            shift 2
            ;;
        --python-version)
            [[ $# -ge 2 ]] || { echo "--python-version requires a value" >&2; exit 2; }
            python_version="$2"
            shift 2
            ;;
        --conda)
            [[ $# -ge 2 ]] || { echo "--conda requires a value" >&2; exit 2; }
            conda_command="$2"
            shift 2
            ;;
        --run-dir)
            [[ $# -ge 2 ]] || { echo "--run-dir requires a value" >&2; exit 2; }
            run_dir="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$wheel_path" ]]; then
    echo "--wheel is required" >&2
    usage >&2
    exit 2
fi
if [[ ! -f "$wheel_path" ]]; then
    echo "Wheel does not exist: $wheel_path" >&2
    exit 2
fi
if [[ "$wheel_path" != *.whl ]]; then
    echo "Input is not a .whl file: $wheel_path" >&2
    exit 2
fi
if ! command -v "$conda_command" >/dev/null 2>&1; then
    echo "Conda executable was not found: $conda_command" >&2
    exit 2
fi

wheel_path="$(cd -- "$(dirname -- "$wheel_path")" && pwd)/$(basename -- "$wheel_path")"
if [[ -z "$run_dir" ]]; then
    run_id="install-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    run_dir="${PROJECT_ROOT}/artifacts/${run_id}"
else
    mkdir -p -- "$(dirname -- "$run_dir")"
    run_dir="$(cd -- "$(dirname -- "$run_dir")" && pwd)/$(basename -- "$run_dir")"
fi

if [[ -e "$run_dir" ]] && [[ -n "$(find "$run_dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    echo "Run directory must be empty: $run_dir" >&2
    exit 2
fi
mkdir -p -- "$run_dir"

env_dir="${run_dir}/conda-env"
conda_log="${run_dir}/conda-create.log"
install_log="${run_dir}/pip-install.log"
check_log="${run_dir}/pip-check.log"

echo "[1/3] Creating clean Conda environment: $env_dir"
"$conda_command" create \
    --yes \
    --prefix "$env_dir" \
    "python=${python_version}" \
    pip \
    2>&1 | tee "$conda_log"

env_python="${env_dir}/bin/python"
if [[ ! -x "$env_python" ]]; then
    echo "Conda environment did not create an executable Python: $env_python" >&2
    exit 1
fi

echo "[2/3] Installing wheel: $wheel_path"
"$env_python" -m pip install \
    --disable-pip-version-check \
    --no-input \
    "$wheel_path" \
    2>&1 | tee "$install_log"

echo "[3/3] Checking installed dependency consistency"
"$env_python" -m pip check 2>&1 | tee "$check_log"

cat <<EOF

Installation validation passed.
Conda environment: $env_dir
Validated wheel:   $wheel_path
Logs:              $run_dir
EOF
