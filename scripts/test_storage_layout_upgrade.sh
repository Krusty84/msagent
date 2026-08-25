#!/usr/bin/env bash

# Black-box upgrade checks for the global msAgent storage layout.
#
# Usage:
#   scripts/test_storage_layout_upgrade.sh <old-conda-env> <new-conda-env>
#
# The old environment must contain the last release that still used
# <workspace>/.msagent. The new environment must contain the global-layout
# release. All application invocations go through `conda run ... msagent`.

set -euo pipefail

OLD_ENV="${OLD_CONDA_ENV:-${1:-}}"
NEW_ENV="${NEW_CONDA_ENV:-${2:-}}"
CONDA_BIN="${CONDA_BIN:-conda}"
KEEP_ARTIFACTS="${KEEP_ARTIFACTS:-0}"

fail() {
    printf '[storage-layout] FAIL: %s\n' "$*" >&2
    if [[ -n "${LAST_OUTPUT:-}" ]]; then
        printf '%s\n' '--- last command output ---' >&2
        printf '%s\n' "$LAST_OUTPUT" >&2
        printf '%s\n' '--- end command output ---' >&2
    fi
    exit 1
}

usage() {
    printf 'Usage: %s <old-conda-env> <new-conda-env>\n' "$0" >&2
    printf '\nEnvironment variables:\n' >&2
    printf '  OLD_CONDA_ENV  Old environment name (alternative to argument 1)\n' >&2
    printf '  NEW_CONDA_ENV  New environment name (alternative to argument 2)\n' >&2
    printf '  CONDA_BIN      Conda executable, default: conda\n' >&2
    printf '  KEEP_ARTIFACTS=1  Keep temporary test data after completion\n' >&2
    exit 2
}

[[ -n "$OLD_ENV" && -n "$NEW_ENV" ]] || usage
command -v "$CONDA_BIN" >/dev/null 2>&1 || fail "Conda executable not found: $CONDA_BIN"

ROOT_CASE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/msagent-layout-tests.XXXXXX")"
trap '[[ "$KEEP_ARTIFACTS" == "1" ]] || rm -rf "$ROOT_CASE_DIR"' EXIT

LAST_OUTPUT=""
LAST_STATUS=0
CASE_HOME=""

log() {
    printf '[storage-layout] %s\n' "$*"
}

run_capture() {
    local env_name="$1"
    local home_dir="$2"
    local configured_home="$3"
    shift 3

    set +e
    if [[ -n "$configured_home" ]]; then
        LAST_OUTPUT="$(HOME="$home_dir" MSAGENT_HOME="$configured_home" "$CONDA_BIN" run --no-capture-output -n "$env_name" msagent "$@" 2>&1)"
    else
        LAST_OUTPUT="$(env -u MSAGENT_HOME HOME="$home_dir" "$CONDA_BIN" run --no-capture-output -n "$env_name" msagent "$@" 2>&1)"
    fi
    LAST_STATUS=$?
    set -e
}

run_old() {
    run_capture "$OLD_ENV" "$@"
}

run_new() {
    run_capture "$NEW_ENV" "$@"
}

assert_status() {
    local expected="$1"
    [[ "$LAST_STATUS" == "$expected" ]] || fail "Expected exit status $expected, got $LAST_STATUS"
}

assert_file() {
    [[ -f "$1" ]] || fail "Expected regular file: $1"
}

assert_dir() {
    [[ -d "$1" ]] || fail "Expected directory: $1"
}

assert_absent() {
    [[ ! -e "$1" ]] || fail "Expected path to be absent: $1"
}

assert_contains() {
    local expected="$1"
    grep -Fq -- "$expected" <<<"$LAST_OUTPUT" || fail "Output does not contain: $expected"
}

assert_not_contains() {
    local unexpected="$1"
    if grep -Fq -- "$unexpected" <<<"$LAST_OUTPUT"; then
        fail "Output unexpectedly contains: $unexpected"
    fi
}

assert_metadata_v2() {
    local metadata_file="$1"
    assert_file "$metadata_file"
    grep -Fq '"storage_layout_version": 2' "$metadata_file" \
        || fail "Metadata is not layout version 2: $metadata_file"
}

new_home_case() {
    local name="$1"
    CASE_HOME="$ROOT_CASE_DIR/$name/home"
    mkdir -p "$CASE_HOME"
    printf '%s\n' "$CASE_HOME"
}

log "Checking old environment: $OLD_ENV"
run_old "$ROOT_CASE_DIR/probe-home" "" --version
assert_status 0
log "Old environment version command succeeded"

log "Checking new environment: $NEW_ENV"
run_new "$ROOT_CASE_DIR/probe-home" "" --version
assert_status 0
log "New environment version command succeeded"

# TC-001: An old release creates the legacy global-looking directory when the
# user's home is also used as the old workspace. The new release must stop
# before creating logs, metadata, or new config directories.
log "TC-001 legacy home is blocked before any new write"
new_home_case tc001 >/dev/null
legacy_workspace="$CASE_HOME"
mkdir -p "$legacy_workspace/workspace"
run_old "$CASE_HOME" "" config --show -w "$legacy_workspace"
assert_status 0
legacy_home="$CASE_HOME/.msagent"
assert_file "$legacy_home/config.llms.yml"
legacy_logs_before="absent"
if [[ -e "$legacy_home/logs" || -L "$legacy_home/logs" ]]; then
    legacy_logs_before="present"
fi

run_new "$CASE_HOME" "" config --show -w "$CASE_HOME/workspace"
assert_status 1
assert_contains "Legacy msAgent storage layout detected"
assert_contains "$legacy_home"
assert_contains "config.llms.yml"
assert_absent "$legacy_home/metadata.json"
legacy_logs_after="absent"
if [[ -e "$legacy_home/logs" || -L "$legacy_home/logs" ]]; then
    legacy_logs_after="present"
fi
[[ "$legacy_logs_before" == "$legacy_logs_after" ]] \
    || fail "Legacy logs path changed while the new layout was rejected"
assert_absent "$legacy_home/config"
assert_absent "$legacy_home/state"

run_new "$CASE_HOME" "" --help
assert_status 0
assert_not_contains "Legacy msAgent storage layout detected"
run_new "$CASE_HOME" "" config --help
assert_status 0
run_new "$CASE_HOME" "" --version
assert_status 0

# TC-002: A legacy .msagent left in an ordinary workspace is out of scope. A
# separate global home should initialize successfully and the residue remains.
log "TC-002 workspace residue does not block new global home"
new_home_case tc002 >/dev/null
workspace="$CASE_HOME/workspace"
mkdir -p "$workspace"
run_old "$CASE_HOME" "" config --show -w "$workspace"
assert_status 0
assert_file "$workspace/.msagent/config.llms.yml"

global_home="$CASE_HOME/global-home"
run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
assert_status 0
assert_metadata_v2 "$global_home/metadata.json"
assert_file "$workspace/.msagent/config.llms.yml"

# TC-003: Empty/new home initialization and idempotent second startup.
log "TC-003 empty global home initializes and is idempotent"
new_home_case tc003 >/dev/null
global_home="$CASE_HOME/global-home"
workspace="$CASE_HOME/workspace"
mkdir -p "$workspace"
run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
assert_status 0
assert_metadata_v2 "$global_home/metadata.json"
assert_dir "$global_home/config"
assert_dir "$global_home/state/projects"
assert_absent "$workspace/.msagent"

metadata_before="$(sha256sum "$global_home/metadata.json")"
run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
assert_status 0
metadata_after="$(sha256sum "$global_home/metadata.json")"
[[ "$metadata_before" == "$metadata_after" ]] || fail "Second startup changed metadata"

# TC-004: A new directory containing config/state but no metadata is backfilled.
log "TC-004 existing new directories are recognized and backfilled"
new_home_case tc004 >/dev/null
global_home="$CASE_HOME/global-home"
workspace="$CASE_HOME/workspace"
mkdir -p "$global_home/config" "$global_home/state/projects" "$workspace"
printf 'user override\n' > "$global_home/config/custom.yml"
run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
assert_status 0
assert_metadata_v2 "$global_home/metadata.json"
assert_file "$global_home/config/custom.yml"

# TC-005: Only a shared user directory such as skills/ must not be treated as
# an old layout, because it is valid in the new layout.
log "TC-005 skills-only home is not falsely rejected"
new_home_case tc005 >/dev/null
global_home="$CASE_HOME/global-home"
workspace="$CASE_HOME/workspace"
mkdir -p "$global_home/skills" "$workspace"
printf 'user skill\n' > "$global_home/skills/user-skill.md"
run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
assert_status 0
assert_metadata_v2 "$global_home/metadata.json"
assert_file "$global_home/skills/user-skill.md"

# TC-006: New and old markers mixed together must produce a distinct warning
# and must not advise deleting the whole directory silently.
log "TC-006 mixed old and new layout is blocked"
new_home_case tc006 >/dev/null
global_home="$CASE_HOME/global-home"
workspace="$CASE_HOME/workspace"
mkdir -p "$global_home/config" "$global_home/state/projects" "$workspace"
run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
assert_status 0
printf 'legacy override\n' > "$global_home/config.llms.yml"
run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
assert_status 1
assert_contains "Mixed old and new msAgent storage layouts detected"
assert_contains "config/"
assert_contains "config.llms.yml"
assert_file "$global_home/metadata.json"

# TC-007: Broken, lower, and higher metadata versions are all rejected without
# overwriting the user's metadata.
for version_case in invalid-json lower-version higher-version; do
    log "TC-007 $version_case metadata is rejected"
    new_home_case "tc007-$version_case" >/dev/null
    global_home="$CASE_HOME/global-home"
    workspace="$CASE_HOME/workspace"
    mkdir -p "$global_home" "$workspace"
    case "$version_case" in
        invalid-json) printf '{broken\n' > "$global_home/metadata.json" ;;
        lower-version) printf '{"storage_layout_version": 1}\n' > "$global_home/metadata.json" ;;
        higher-version) printf '{"storage_layout_version": 99}\n' > "$global_home/metadata.json" ;;
    esac
    before="$(sha256sum "$global_home/metadata.json")"
    run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
    assert_status 1
    after="$(sha256sum "$global_home/metadata.json")"
    [[ "$before" == "$after" ]] || fail "Metadata was overwritten for $version_case"
    assert_absent "$global_home/logs"
done

# TC-008: MSAGENT_HOME must override the default home used by the new release.
log "TC-008 custom MSAGENT_HOME is the inspected path"
new_home_case tc008 >/dev/null
global_home="$CASE_HOME/custom-msagent-home"
workspace="$CASE_HOME/workspace"
mkdir -p "$global_home" "$workspace"
printf 'legacy override\n' > "$global_home/config.llms.yml"
run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
assert_status 1
assert_contains "$global_home"
assert_contains "config.llms.yml"

# TC-009: Every strong legacy marker is independently recognized. These
# fixtures are intentionally created directly because an old release normally
# creates all of them together, while the detector must also handle one left
# behind by a partial cleanup.
log "TC-009 every legacy marker is independently blocked"
legacy_markers=(
    "config.llms.yml"
    "config.mcp.json"
    "config.approval.json"
    "config.agents.yml"
    "config.subagents.yml"
    "config.checkpointers.yml"
    "config.checkpoints.db"
    "langgraph.json"
    ".history"
    "memory.md"
    "agents/"
    "subagents/"
    "llms/"
    "checkpointers/"
    "sandboxes/"
)
for marker in "${legacy_markers[@]}"; do
    marker_id="${marker//\//_}"
    new_home_case "tc009-$marker_id" >/dev/null
    global_home="$CASE_HOME/global-home"
    workspace="$CASE_HOME/workspace"
    mkdir -p "$global_home" "$workspace"
    if [[ "$marker" == */ ]]; then
        mkdir -p "$global_home/${marker%/}"
    else
        printf 'legacy marker\n' > "$global_home/$marker"
    fi
    run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
    assert_status 1
    assert_contains "Legacy msAgent storage layout detected"
    assert_contains "$marker"
    assert_absent "$global_home/metadata.json"
    assert_absent "$global_home/logs"
done

# TC-010: The directories shared by old and new versions are not sufficient
# evidence of a legacy layout on their own.
log "TC-010 shared directories alone are accepted"
shared_dirs=("skills" "prompts" "cache" "oauth" "logs")
for shared_dir in "${shared_dirs[@]}"; do
    new_home_case "tc010-$shared_dir" >/dev/null
    global_home="$CASE_HOME/global-home"
    workspace="$CASE_HOME/workspace"
    mkdir -p "$global_home/$shared_dir" "$workspace"
    run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
    assert_status 0
    assert_metadata_v2 "$global_home/metadata.json"
done

# TC-011: Metadata shape and value validation must reject every invalid form
# without replacing the original file.
log "TC-011 every invalid metadata form is rejected"
metadata_cases=(
    "root-array|[]"
    "missing-field|{\"other\":\"value\"}"
    "boolean|{\"storage_layout_version\":true}"
    "string|{\"storage_layout_version\":\"2\"}"
    "zero|{\"storage_layout_version\":0}"
    "negative|{\"storage_layout_version\":-1}"
)
for metadata_case in "${metadata_cases[@]}"; do
    case_name="${metadata_case%%|*}"
    payload="${metadata_case#*|}"
    new_home_case "tc011-$case_name" >/dev/null
    global_home="$CASE_HOME/global-home"
    workspace="$CASE_HOME/workspace"
    mkdir -p "$global_home" "$workspace"
    printf '%s\n' "$payload" > "$global_home/metadata.json"
    before="$(sha256sum "$global_home/metadata.json")"
    run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
    assert_status 1
    after="$(sha256sum "$global_home/metadata.json")"
    [[ "$before" == "$after" ]] || fail "Metadata was overwritten for $case_name"
    assert_absent "$global_home/logs"
done

# TC-012: Home and metadata paths with the wrong filesystem type are rejected.
log "TC-012 invalid filesystem types are rejected"
new_home_case tc012-home-file >/dev/null
home_file="$CASE_HOME/global-home"
workspace="$CASE_HOME/workspace"
rm -rf "$home_file"
printf 'not a directory\n' > "$home_file"
mkdir -p "$workspace"
run_new "$CASE_HOME" "$home_file" config --show -w "$workspace"
assert_status 1
assert_contains "home must be a directory"

new_home_case tc012-metadata-directory >/dev/null
global_home="$CASE_HOME/global-home"
workspace="$CASE_HOME/workspace"
mkdir -p "$global_home/metadata.json" "$workspace"
run_new "$CASE_HOME" "$global_home" config --show -w "$workspace"
assert_status 1
assert_contains "metadata must be a regular file"

log "PASS: all storage layout upgrade checks passed"
log "Artifacts: $ROOT_CASE_DIR"
