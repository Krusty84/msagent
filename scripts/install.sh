#!/usr/bin/env bash
# =============================================================================
# msAgent installer for Linux / macOS / WSL
#
# Usage:
#   curl -LsSf https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.sh | bash
#
# What it does:
#   1. Preflight checks (network, existing pip install, downloader availability)
#   2. Bootstraps uv (official installer first, pip + domestic mirror as fallback)
#   3. Installs mindstudio-agent (latest from PyPI) into an isolated uv tool env:
#        uv tool install -U --python 3.11 mindstudio-agent --with-executables-from msprof-mcp
#   4. Adds the tool bin directory to PATH (shell profiles, idempotent)
#   5. Verifies: msagent --version
#
# The installer never pins a version: it always installs the latest
# mindstudio-agent from PyPI, so releases do not require touching this script.
# The uv tool environment is fully isolated, so it will not conflict with any
# existing Python environment (system Python, torch, mindstudio_monitor, ...).
#
# Environment variables:
#   MSAGENT_PYTHON            Python request for the isolated tool env (default: >=3.11, reuses local Python)
#   MSAGENT_VERSION           Exact version to install, e.g. "26.1.0" (default: latest)
#   MSAGENT_INDEX             PyPI index URL override (default: official PyPI, domestic mirrors as fallback)
#   MSAGENT_NO_MODIFY_PATH    set to 1 to skip all PATH modification
#   MSAGENT_YES               set to 1 to accept prompts without asking (CI/cron)
#   MSAGENT_NO_FALLBACK       set to 1 to disable the venv fallback
#   MSAGENT_FALLBACK_VENV     venv path used by the fallback install (default: ~/.msagent-venv)
#   MSAGENT_WITH_EXECUTABLES_FROM  package whose executables are exposed too (default: msprof-mcp)
#   UV_DEFAULT_INDEX / UV_INDEX_URL / PIP_INDEX_URL  used as a last-resort fallback
#   UV_PYTHON_INSTALL_MIRROR  mirror for managed CPython downloads (domestic default, used when certificate trusted)
#
# Uninstall:
#   uv tool uninstall mindstudio-agent
#   Optionally remove the PATH entries this installer added to your profiles.
#
# Upgrade:
#   Just re-run the installer; it always upgrades to the latest release.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
if [ -t 1 ] || [ "${FORCE_COLOR:-}" = "1" ]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; NC=''
fi

log_info()    { printf "${CYAN}▸${NC} %s\n" "$*"; }
log_success() { printf "${GREEN}✔${NC} %s\n" "$*"; }
log_warn()    { printf "${YELLOW}⚠${NC} %s\n" "$*" >&2; }
log_error()   { printf "${RED}✖${NC} %s\n" "$*" >&2; }

# Works both interactively and under `curl ... | bash` (reads from /dev/tty).
prompt_yn() {
  [ "${MSAGENT_YES:-0}" = "1" ] && return 0
  local answer
  printf "${YELLOW}?${NC} %s [y/N] " "$*" >&2
  if [ -t 0 ]; then
    read -r answer || return 1
  elif [ -r /dev/tty ]; then
    read -r answer < /dev/tty || return 1
  else
    log_warn "Non-interactive shell; assuming 'no'."
    return 1
  fi
  case "${answer}" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# OS / environment detection
# ---------------------------------------------------------------------------
detect_os() {
  case "$(uname -s)" in
    Darwin) OS="macos" ;;
    Linux)  OS="linux" ;;
    MINGW*|MSYS*|CYGWIN*) OS="windows" ;;
    *) OS="unknown" ;;
  esac
}
detect_os

case "${OS}" in
  linux|macos) ;;
  windows)
    log_error "This installer targets Linux, macOS, and WSL shells."
    log_error "On native Windows (PowerShell), use:"
    log_error "  irm https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.ps1 | iex"
    exit 1
    ;;
  *)
    log_error "Unsupported operating system. Please install msagent manually:"
    log_error "  python3 -m venv ~/.msagent-venv && ~/.msagent-venv/bin/pip install -U mindstudio-agent"
    exit 1
    ;;
esac

# macOS MDM/root installs may run without a usable HOME. Re-home to the real
# console user so uv installs into the expected profile directory.
if [ "${OS}" = "macos" ] && { [ -z "${HOME:-}" ] || [ "$(id -u)" -eq 0 ]; }; then
  CONSOLE_USER="$(stat -f '%Su' /dev/console 2>/dev/null || true)"
  if [ -n "${CONSOLE_USER}" ] && [ "${CONSOLE_USER}" != "root" ] && [ -d "/Users/${CONSOLE_USER}" ]; then
    HOME="/Users/${CONSOLE_USER}"
    export HOME
  fi
fi

# root runs: install into the invoking user's profile, then fix ownership.
if [ "$(id -u)" -eq 0 ]; then
  TARGET_USER="${SUDO_USER:-${CONSOLE_USER:-$(basename "${HOME:-root}")}}"
  if [ -z "${TARGET_USER}" ] || [ "${TARGET_USER}" = "root" ]; then
    fix_owner() { :; }
  else
    fix_owner() {
      chown -R "${TARGET_USER}" "$@" 2>/dev/null || \
        log_warn "Could not fix ownership for $*"
    }
  fi
else
  fix_owner() { :; }
fi

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  log_error "curl or wget is required to download the installer assets."
  exit 1
fi

probe_url() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null --connect-timeout 6 --max-time 12 -I "${url}" 2>/dev/null && return 0
    curl -fsS -o /dev/null --connect-timeout 6 --max-time 12 "${url}" 2>/dev/null && return 0
  elif command -v wget >/dev/null 2>&1; then
    wget -q --spider --timeout=6 --tries=1 "${url}" 2>/dev/null && return 0
  fi
  return 1
}

# Warn about an existing pip-installed msagent that may shadow the uv tool.
if command -v pip >/dev/null 2>&1 && pip show mindstudio-agent >/dev/null 2>&1; then
  log_warn "mindstudio-agent is already installed via pip in this environment."
  log_warn "The uv tool install below is isolated and will not modify it, but the"
  log_warn "two executables may shadow each other on PATH. Consider:"
  log_warn "  pip uninstall mindstudio-agent"
fi

# ---------------------------------------------------------------------------
# Index selection: domestic mirrors first (fast in China), official PyPI as
# fallback; generic env vars last resort. Mirror sync may lag weekly
# releases, so the install is retried with official PyPI on failure (see the
# install section), and the latest version is pinned from PyPI beforehand.
# ---------------------------------------------------------------------------
select_index() {
  local candidate
  if [ -n "${MSAGENT_INDEX:-}" ]; then
    INDEX="${MSAGENT_INDEX}"
    log_info "Using index from MSAGENT_INDEX: ${INDEX}"
    return 0
  fi
  for candidate in \
    "https://mirrors.huaweicloud.com/repository/pypi/simple" \
    "https://mirrors.aliyun.com/pypi/simple" \
    "https://pypi.tuna.tsinghua.edu.cn/simple" \
    "https://pypi.org/simple"; do
    if probe_url "${candidate}/pip/"; then
      INDEX="${candidate}"
      log_info "Selected PyPI index: ${candidate}"
      return 0
    fi
    log_warn "Index unreachable: ${candidate} (trying the next candidate)"
  done
  # All candidates are unreachable; fall back to a user-configured
  # generic index if one exists, otherwise use official PyPI.
  if [ -n "${UV_DEFAULT_INDEX:-}" ]; then
    INDEX="${UV_DEFAULT_INDEX}"
    log_info "Fallback to UV_DEFAULT_INDEX: ${INDEX}"
    return 0
  fi
  if [ -n "${UV_INDEX_URL:-}" ]; then
    INDEX="${UV_INDEX_URL}"
    log_info "Fallback to UV_INDEX_URL: ${INDEX}"
    return 0
  fi
  if [ -n "${PIP_INDEX_URL:-}" ]; then
    INDEX="${PIP_INDEX_URL}"
    log_info "Fallback to PIP_INDEX_URL: ${INDEX}"
    return 0
  fi
  INDEX="https://pypi.org/simple"
  log_warn "No reachable index found; defaulting to ${INDEX}."
}
select_index

# ---------------------------------------------------------------------------
# Resolve and announce the msagent version to install. Mirror sync may lag
# weekly releases, so the latest version is pinned straight from PyPI and
# shown up front; the install is retried with official PyPI if the selected
# mirror does not have that version yet.
# ---------------------------------------------------------------------------
MSAGENT_PYTHON="${MSAGENT_PYTHON:->=3.11}"
INSTALL_SPEC="mindstudio-agent"
VERSION_SOURCE="latest from the selected index"
if [ -n "${MSAGENT_VERSION:-}" ]; then
  INSTALL_SPEC="mindstudio-agent==${MSAGENT_VERSION}"
  VERSION_SOURCE="MSAGENT_VERSION"
elif [ -z "${MSAGENT_INDEX:-}" ]; then
  LATEST_VERSION=""
  if command -v curl >/dev/null 2>&1; then
    LATEST_VERSION="$(curl -fsSL --connect-timeout 6 --max-time 15 https://pypi.org/pypi/mindstudio-agent/json 2>/dev/null | grep -o '"version":"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
  elif command -v wget >/dev/null 2>&1; then
    LATEST_VERSION="$(wget -qO- --timeout=15 https://pypi.org/pypi/mindstudio-agent/json 2>/dev/null | grep -o '"version":"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
  fi
  if [ -n "${LATEST_VERSION}" ]; then
    INSTALL_SPEC="mindstudio-agent==${LATEST_VERSION}"
    VERSION_SOURCE="latest from PyPI"
  fi
fi
log_info "Will install msagent ${INSTALL_SPEC#mindstudio-agent==} (${VERSION_SOURCE})"

# ---------------------------------------------------------------------------
# uv bootstrap: official installer, then pip + domestic mirror as fallback.
# ---------------------------------------------------------------------------
UV_BIN="${UV_BIN:-}"
bootstrap_uv() {
  if [ -n "${UV_BIN:-}" ]; then
    if [ -x "${UV_BIN}" ] || command -v "${UV_BIN}" >/dev/null 2>&1; then
      log_info "Using uv from UV_BIN: ${UV_BIN}"
      return 0
    fi
    log_warn "UV_BIN is set but not executable: ${UV_BIN}"
    UV_BIN=""
  fi
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="uv"
    return 0
  fi
  if [ -x "${HOME}/.local/bin/uv" ]; then
    UV_BIN="${HOME}/.local/bin/uv"
    return 0
  fi

  log_info "uv is not installed. Bootstrapping uv..."
  if command -v curl >/dev/null 2>&1 && curl -fsSL https://astral.sh/uv/install.sh | sh; then
    :
  elif command -v wget >/dev/null 2>&1 && wget -qO- https://astral.sh/uv/install.sh | sh; then
    :
  else
    log_warn "Official uv installer failed or timed out; trying pip + ${INDEX} ..."
    local pybin=""
    if command -v python3 >/dev/null 2>&1; then pybin="python3"
    elif command -v python >/dev/null 2>&1; then pybin="python"
    else
      log_error "python3 is required to bootstrap uv via pip."
      log_error "Install uv manually (https://docs.astral.sh/uv/getting-started/installation/) and re-run this installer."
      return 1
    fi
    local errfile
    errfile="$(mktemp)"
    if ! "${pybin}" -m pip install --user -q -U uv -i "${INDEX}" 2>"${errfile}"; then
      if grep -q "externally-managed" "${errfile}" 2>/dev/null; then
        log_warn "PEP 668 externally-managed environment detected; retrying with --break-system-packages ..."
        "${pybin}" -m pip install --user -q -U --break-system-packages uv -i "${INDEX}" || {
          cat "${errfile}" >&2
          rm -f "${errfile}"
          log_error "uv could not be installed. Install it manually and re-run this installer."
          return 1
        }
      else
        cat "${errfile}" >&2
        rm -f "${errfile}"
        log_error "uv could not be installed. Install it manually and re-run this installer."
        return 1
      fi
    fi
    rm -f "${errfile}"
  fi

  if [ -x "${HOME}/.local/bin/uv" ]; then
    UV_BIN="${HOME}/.local/bin/uv"
    return 0
  fi
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="uv"
    return 0
  fi
  if [ -f "${HOME}/.local/bin/env" ]; then
    # shellcheck source=/dev/null
    . "${HOME}/.local/bin/env"
    if command -v uv >/dev/null 2>&1; then
      UV_BIN="uv"
      return 0
    fi
  fi
  log_error "uv is not available after bootstrap. Restart your shell and re-run, or add ~/.local/bin to PATH."
  return 1
}
bootstrap_uv

# ---------------------------------------------------------------------------
# Managed CPython downloads: prefer Huawei Cloud, then NJU, only when the
# certificate is trusted (corporate proxies may reject them); otherwise leave
# uv's defaults (official source or an existing local Python) to handle it.
# ---------------------------------------------------------------------------
if [ -z "${UV_PYTHON_INSTALL_MIRROR:-}" ]; then
  PY_MIRROR_OK=""
  for mirror in \
    "https://mirrors.huaweicloud.com/github-release/astral-sh/python-build-standalone" \
    "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"; do
    if probe_url "${mirror}/"; then
      export UV_PYTHON_INSTALL_MIRROR="${mirror}"
      log_info "UV_PYTHON_INSTALL_MIRROR=${mirror} (set it yourself to override)"
      PY_MIRROR_OK=1
      break
    fi
    log_warn "Python mirror unreachable: ${mirror}"
  done
  if [ -z "${PY_MIRROR_OK:-}" ]; then
    log_warn "Python download mirrors are not reachable with a trusted certificate;"
    log_warn "using uv's default Python source or an existing local Python."
    log_warn "To use an internal mirror, set UV_PYTHON_INSTALL_MIRROR yourself."
  fi
fi

# ---------------------------------------------------------------------------
# Path setup helpers (defined before use)
# ---------------------------------------------------------------------------
ORIGINAL_PATH="${PATH:-}"
TOOL_BIN_DIR="$("${UV_BIN}" tool dir --bin 2>/dev/null || true)"
[ -n "${TOOL_BIN_DIR}" ] || TOOL_BIN_DIR="${HOME}/.local/bin"
PROFILE_FILE="${HOME}/.bashrc"

path_contains() {
  case ":${ORIGINAL_PATH}:" in
    *":$1:"*) return 0 ;;
    *) return 1 ;;
  esac
}

add_to_profile() {
  local profile="$1"
  local line="export PATH=\"${TOOL_BIN_DIR}:\$PATH\""
  if [ -f "${profile}" ] && grep -qF "${line}" "${profile}" 2>/dev/null; then
    return 0
  fi
  touch "${profile}" 2>/dev/null || return 1
  printf '\n# Added by the msagent installer\n%s\n' "${line}" >> "${profile}"
  log_success "Added ${TOOL_BIN_DIR} to PATH in ${profile}"
}

setup_path() {
  if [ "${MSAGENT_NO_MODIFY_PATH:-0}" = "1" ]; then
    log_warn "MSAGENT_NO_MODIFY_PATH is set; skipping PATH modification."
    log_warn "Add ${TOOL_BIN_DIR} to PATH yourself, e.g.:"
    log_warn "  export PATH=\"${TOOL_BIN_DIR}:\$PATH\""
    return 0
  fi
  if path_contains "${TOOL_BIN_DIR}"; then
    log_info "${TOOL_BIN_DIR} is already on PATH."
    return 0
  fi
  PROFILE_FILE=""
  # fish
  if command -v fish >/dev/null 2>&1 || [ -d "${HOME}/.config/fish" ]; then
    local fish_dir="${HOME}/.config/fish/conf.d"
    mkdir -p "${fish_dir}" 2>/dev/null || true
    local fish_file="${fish_dir}/msagent-path.fish"
    if [ ! -f "${fish_file}" ]; then
      printf 'fish_add_path %s\n' "${TOOL_BIN_DIR}" > "${fish_file}" 2>/dev/null || true
      log_success "Added ${TOOL_BIN_DIR} via ${fish_file}"
    fi
  fi
  # bash/zsh: cover every profile a login or interactive shell may read.
  # Only create ~/.bashrc (safe everywhere); existing ~/.bash_profile and
  # ~/.profile are updated too so login shells pick the entry up.
  local updated=0
  local candidate
  for candidate in "${HOME}/.bashrc" "${HOME}/.bash_profile" "${HOME}/.profile"; do
    if [ -f "${candidate}" ] || [ "${candidate}" = "${HOME}/.bashrc" ]; then
      if add_to_profile "${candidate}"; then
        [ -z "${PROFILE_FILE}" ] && PROFILE_FILE="${candidate}"
        updated=1
      fi
    fi
  done
  if [ -n "${SHELL:-}" ] && [[ "${SHELL}" == *zsh* ]]; then
    local zshrc="${HOME}/.zshrc"
    [ -n "${ZDOTDIR:-}" ] && zshrc="${ZDOTDIR}/.zshrc"
    if add_to_profile "${zshrc}"; then
      [ -z "${PROFILE_FILE}" ] && PROFILE_FILE="${zshrc}"
      updated=1
    fi
  fi
  if [ "${updated}" = "1" ]; then
    log_success "Run 'source ${PROFILE_FILE}' (or open a new shell) to use msagent."
  else
    log_warn "Could not update a shell profile; add '${TOOL_BIN_DIR}' to PATH yourself."
  fi
  # uv's own shell-profile updater covers more edge cases; best effort only.
  "${UV_BIN}" tool update-shell >/dev/null 2>&1 || true
  fix_owner "${TOOL_BIN_DIR}" "${HOME}/.local/share/uv" "${HOME}/.cache/uv" 2>/dev/null || true
}

# Last-resort fallback when `uv tool install` fails: an isolated venv.
install_venv_fallback() {
  local venv_dir="${MSAGENT_FALLBACK_VENV:-${HOME}/.msagent-venv}"
  local pybin=""
  if command -v python3 >/dev/null 2>&1; then pybin="python3"
  elif command -v python >/dev/null 2>&1; then pybin="python"
  else
    log_error "python3 is required for the venv fallback."
    return 1
  fi
  log_info "Creating isolated venv at ${venv_dir} ..."
  if ! "${pybin}" -m venv "${venv_dir}" 2>/dev/null; then
    # python3-venv (ensurepip) may be missing; uv can create venvs without it.
    if [ -n "${UV_BIN:-}" ] && "${UV_BIN}" venv "${venv_dir}" 2>/dev/null; then
      log_success "Created the venv with uv (python3-venv is not installed)."
    else
      log_error "Could not create a virtualenv (python3-venv may be missing)."
      log_error "On Debian/Ubuntu, install it first:  sudo apt install python3-venv"
      log_error "or install msagent manually:"
      log_error "  ${pybin} -m venv ${venv_dir} && ${venv_dir}/bin/pip install -U -i ${INDEX} ${INSTALL_SPEC}"
      return 1
    fi
  fi
  log_info "Installing ${INSTALL_SPEC} from ${INDEX} into the venv..."
  if [ -x "${venv_dir}/bin/pip" ]; then
    "${venv_dir}/bin/pip" install -q -U -i "${INDEX}" "${INSTALL_SPEC}" || return 1
  elif [ -n "${UV_BIN:-}" ]; then
    # A venv created by uv has no pip; install with uv pip instead.
    "${UV_BIN}" pip install --python "${venv_dir}/bin/python" -q -U --index-url "${INDEX}" "${INSTALL_SPEC}" || return 1
  else
    return 1
  fi
  TOOL_BIN_DIR="${venv_dir}/bin"
  setup_path
  local vout
  vout="$("${TOOL_BIN_DIR}/msagent" --version 2>&1)" || true
  log_success "Verified: ${vout}"
  log_warn "Installed via the venv fallback (${venv_dir})."
  log_warn "Prefer 'uv tool install'; re-run this installer once uv works to switch."
}

# uv sometimes only links the primary tool's executables into the tool bin
# directory. The msprof-mcp MCP server is spawned by name from PATH, so make
# sure its executable (and msprof-analyze) are reachable. Best effort only.
expose_tool_executables() {
  local tool_env_bin
  tool_env_bin="$("${UV_BIN}" tool dir 2>/dev/null || true)/mindstudio-agent/bin"
  [ -d "${tool_env_bin}" ] || return 0
  local exe
  for exe in msprof-mcp msprof-analyze; do
    if [ -x "${tool_env_bin}/${exe}" ] && [ ! -e "${TOOL_BIN_DIR}/${exe}" ]; then
      ln -s "${tool_env_bin}/${exe}" "${TOOL_BIN_DIR}/${exe}" 2>/dev/null && \
        log_success "Exposed ${exe} via ${TOOL_BIN_DIR}/${exe}"
    fi
  done
}

# ---------------------------------------------------------------------------
# Install mindstudio-agent as an isolated uv tool (target version resolved above)
# ---------------------------------------------------------------------------
if command -v msagent >/dev/null 2>&1; then
  log_info "Updating existing msagent installation..."
else
  log_info "Installing ${INSTALL_SPEC} into an isolated uv tool environment..."
fi

UV_TOOL_ARGS=(tool install -U --python "${MSAGENT_PYTHON}" --default-index "${INDEX}")
if [ -n "${MSAGENT_WITH_EXECUTABLES_FROM:-}" ]; then
  UV_TOOL_ARGS+=(--with-executables-from "${MSAGENT_WITH_EXECUTABLES_FROM}")
fi
UV_TOOL_ARGS+=("${INSTALL_SPEC}")

INSTALL_OK=0
if "${UV_BIN}" "${UV_TOOL_ARGS[@]}"; then
  INSTALL_OK=1
fi

# Mirror sync may lag the latest weekly release; retry with official PyPI.
if [ "${INSTALL_OK}" = "0" ] && [ -z "${MSAGENT_INDEX:-}" ] && [ "${INDEX}" != "https://pypi.org/simple" ]; then
  log_warn "Install failed with ${INDEX}; retrying once with official PyPI (mirror sync may lag)..."
  UV_TOOL_ARGS=(tool install -U --python "${MSAGENT_PYTHON}" --default-index "https://pypi.org/simple")
  if [ -n "${MSAGENT_WITH_EXECUTABLES_FROM:-}" ]; then
    UV_TOOL_ARGS+=(--with-executables-from "${MSAGENT_WITH_EXECUTABLES_FROM}")
  fi
  UV_TOOL_ARGS+=("${INSTALL_SPEC}")
  if "${UV_BIN}" "${UV_TOOL_ARGS[@]}"; then
    INSTALL_OK=1
  fi
fi

if [ "${INSTALL_OK}" = "0" ]; then
  # Retry once with a freshly upgraded uv: a pre-existing uv may be old or in
  # a broken state (e.g. failing managed-Python resolution on Windows).
  log_warn "First install attempt failed; upgrading uv and retrying once..."
  RETRY_PY=""
  if command -v python3 >/dev/null 2>&1; then RETRY_PY="python3"
  elif command -v python >/dev/null 2>&1; then RETRY_PY="python"
  fi
  if [ -n "${RETRY_PY}" ]; then
    "${RETRY_PY}" -m pip install --user -q -U uv -i "${INDEX}" 2>/dev/null || \
      "${RETRY_PY}" -m pip install --user -q -U --break-system-packages uv -i "${INDEX}" 2>/dev/null || true
  fi
  [ -x "${HOME}/.local/bin/uv" ] && UV_BIN="${HOME}/.local/bin/uv"
  if "${UV_BIN}" "${UV_TOOL_ARGS[@]}"; then
    INSTALL_OK=1
  fi
fi

if [ "${INSTALL_OK}" = "0" ]; then
  # The tool may exist in a broken/stale state (e.g. its Python interpreter
  # no longer matches). Remove it and retry the install from scratch once.
  log_warn "Install still failing; removing any existing msagent tool and retrying once more..."
  "${UV_BIN}" tool uninstall mindstudio-agent >/dev/null 2>&1 || true
  if "${UV_BIN}" "${UV_TOOL_ARGS[@]}"; then
    INSTALL_OK=1
  fi
fi

if [ "${INSTALL_OK}" = "0" ]; then
  log_error "uv tool install failed. See the error above."
  if [ "${MSAGENT_NO_FALLBACK:-0}" = "1" ]; then
    log_error "MSAGENT_NO_FALLBACK is set; skipping the venv fallback."
    log_error "Retry after fixing the issue, or install manually:"
    log_error "  python3 -m venv ~/.msagent-venv && ~/.msagent-venv/bin/pip install -U -i ${INDEX} ${INSTALL_SPEC}"
    exit 1
  fi
  if prompt_yn "Try the isolated venv fallback instead?"; then
    if ! install_venv_fallback; then
      log_error "The venv fallback also failed. Please retry later or open an issue at"
      log_error "  https://gitcode.com/Ascend/msagent/issues"
      exit 1
    fi
  else
    log_error "Install aborted. You can retry with:"
    log_error "  curl -LsSf https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.sh | bash"
    exit 1
  fi
else
  # -------------------------------------------------------------------------
  # PATH: make the tool bin directory available in new shells.
  # -------------------------------------------------------------------------
  setup_path
fi

# Ensure msprof-mcp / msprof-analyze are reachable from PATH (best effort).
expose_tool_executables

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
MSAGENT_BIN=""
if [ -x "${TOOL_BIN_DIR}/msagent" ]; then
  MSAGENT_BIN="${TOOL_BIN_DIR}/msagent"
elif command -v msagent >/dev/null 2>&1; then
  MSAGENT_BIN="msagent"
fi

if [ -z "${MSAGENT_BIN}" ]; then
  log_warn "msagent was installed but is not on PATH in this shell."
  log_warn "Run 'source ${PROFILE_FILE}' or open a new shell, then check 'msagent --version'."
  exit 0
fi

VERSION_OUTPUT="$("${MSAGENT_BIN}" --version 2>&1)" || {
  log_error "msagent installed, but '--version' failed:"
  printf '%s\n' "${VERSION_OUTPUT}" >&2
  log_error "Try: source ~/.local/bin/env   (or open a new shell), then re-run."
  exit 1
}
log_success "Verified: ${VERSION_OUTPUT}"

# The verify above uses the absolute path, which always works. The user's
# real shell may not have the tool bin dir on PATH yet (e.g. right after
# `curl ... | bash`), so check resolvability and warn with the exact fix.
PATH_MSAGENT="$(command -v msagent 2>/dev/null || true)"
if [ -z "${PATH_MSAGENT}" ]; then
  log_warn "msagent is installed, but this shell cannot find 'msagent' on PATH yet."
  log_warn "Run the following in THIS terminal (or open a new shell):"
  log_warn "  export PATH=\"${TOOL_BIN_DIR}:\$PATH\""
  log_warn "The installer already added the PATH entry to ${PROFILE_FILE:-your shell profile} for future shells."
elif [ "${PATH_MSAGENT}" != "${TOOL_BIN_DIR}/msagent" ] && [ "${PATH_MSAGENT}" != "${TOOL_BIN_DIR}/msagent.exe" ]; then
  log_warn "Note: 'msagent' on PATH resolves to ${PATH_MSAGENT} (possibly an older pip install)."
  log_warn "The uv tool install is at ${TOOL_BIN_DIR}/msagent. Consider: pip uninstall mindstudio-agent"
fi

MSPROF_BIN=""
if [ -x "${TOOL_BIN_DIR}/msprof-mcp" ]; then
  MSPROF_BIN="${TOOL_BIN_DIR}/msprof-mcp"
elif command -v msprof-mcp >/dev/null 2>&1; then
  MSPROF_BIN="msprof-mcp"
fi
if [ -z "${MSPROF_BIN}" ]; then
  log_warn "msprof-mcp is not on PATH in this shell yet; restart your shell if msagent reports it missing."
else
  log_success "msprof-mcp executable is available."
fi

log_success "msagent installation complete."
log_info "Next steps:"
log_info "  msagent --help"
log_info "  msagent config --llm-provider openai --llm-model <model> ...   (see Quick Start)"
log_info "Upgrade any time by re-running this installer (always installs the latest)."
