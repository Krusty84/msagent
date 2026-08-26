<#
.SYNOPSIS
  msAgent installer for Windows (PowerShell 5.1+).

.DESCRIPTION
  Installs mindstudio-agent (latest from PyPI) as an isolated uv tool, so it
  will not conflict with any existing Python environment. Bootstraps uv when
  missing, prefers domestic PyPI mirrors, and adds the tool bin directory to
  the user PATH.

.EXAMPLE
  irm https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.ps1 | iex

.EXAMPLE
  $env:MSAGENT_VERSION = '26.1.0'
  irm https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.ps1 | iex

.NOTES
  Optional env:
    MSAGENT_VERSION           exact version to install (default: latest from PyPI)
    MSAGENT_PYTHON            Python version for the isolated tool env (default: 3.11)
    MSAGENT_INDEX             PyPI index override (default: domestic mirror chain)
    MSAGENT_NO_MODIFY_PATH    set to a non-empty value to skip PATH modification
    MSAGENT_YES               set to '1' to accept prompts without asking
    MSAGENT_NO_FALLBACK       set to '1' to disable the venv fallback
    MSAGENT_FALLBACK_VENV     venv path used by the fallback install (default: %USERPROFILE%\.msagent-venv)
    UV_DEFAULT_INDEX / UV_INDEX_URL / PIP_INDEX_URL   used as a last-resort fallback
    UV_PYTHON_INSTALL_MIRROR  mirror for managed CPython downloads (domestic default)

  Uninstall:
    uv tool uninstall mindstudio-agent

  Upgrade:
    Re-run the installer; it always upgrades to the latest release.
#>

$ErrorActionPreference = 'Stop'

# PowerShell 5.1 on older Windows may not negotiate TLS 1.2 by default.
try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
  # TLS 1.2 is already the default on newer systems; ignore failures here.
}

function Write-Step([string]$msg)    { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Success([string]$msg) { Write-Host "OK  $msg" -ForegroundColor Green }
function Write-Warn([string]$msg)    { Write-Host "WARN $msg" -ForegroundColor Yellow }
function Die([string]$msg)           { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
$pipCheck = Get-Command pip -ErrorAction SilentlyContinue
if ($pipCheck) {
  # Note: with $ErrorActionPreference='Stop', PowerShell 5.1 turns redirected
  # native stderr into a terminating NativeCommandError, so guard this probe.
  $pipHasMsagent = $false
  try {
    & pip show mindstudio-agent 2>$null | Out-Null
    $pipHasMsagent = ($LASTEXITCODE -eq 0)
  } catch {
    $pipHasMsagent = $false
  }
  if ($pipHasMsagent) {
    Write-Warn "mindstudio-agent is already installed via pip in this environment."
    Write-Warn "The uv tool install below is isolated and will not modify it, but the"
    Write-Warn "two executables may shadow each other on PATH. Consider: pip uninstall mindstudio-agent"
  }
}

# ---------------------------------------------------------------------------
# Index selection: explicit overrides first, then a domestic mirror chain.
# ---------------------------------------------------------------------------
function Test-Url([string]$url) {
  try {
    Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing -TimeoutSec 8 | Out-Null
    return $true
  } catch { }
  try {
    Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 8 | Out-Null
    return $true
  } catch { }
  return $false
}

function Select-Index {
  # Huawei Cloud first, then other domestic mirrors, official PyPI as
  # fallback; generic index env vars are used only as a last resort.
  if ($env:MSAGENT_INDEX) {
    $script:MsagentIndex = $env:MSAGENT_INDEX
    Write-Step "Using index from MSAGENT_INDEX: $script:MsagentIndex"
    return
  }
  $candidates = @(
    'https://mirrors.huaweicloud.com/repository/pypi/simple',
    'https://mirrors.aliyun.com/pypi/simple',
    'https://pypi.tuna.tsinghua.edu.cn/simple',
    'https://pypi.org/simple'
  )
  foreach ($c in $candidates) {
    if (Test-Url ($c + '/pip/')) {
      $script:MsagentIndex = $c
      Write-Step "Selected PyPI index: $c"
      return
    }
    Write-Warn "Index unreachable: $c (trying the next candidate)"
  }
  # All mirror candidates are unreachable; fall back to a user-configured
  # generic index if one exists, otherwise use official PyPI.
  if ($env:UV_DEFAULT_INDEX) {
    $script:MsagentIndex = $env:UV_DEFAULT_INDEX
    Write-Step "Fallback to UV_DEFAULT_INDEX: $script:MsagentIndex"
    return
  }
  if ($env:UV_INDEX_URL) {
    $script:MsagentIndex = $env:UV_INDEX_URL
    Write-Step "Fallback to UV_INDEX_URL: $script:MsagentIndex"
    return
  }
  if ($env:PIP_INDEX_URL) {
    $script:MsagentIndex = $env:PIP_INDEX_URL
    Write-Step "Fallback to PIP_INDEX_URL: $script:MsagentIndex"
    return
  }
  $script:MsagentIndex = 'https://pypi.org/simple'
  Write-Warn "No reachable index found; defaulting to $script:MsagentIndex."
}
Select-Index

# ---------------------------------------------------------------------------
# uv bootstrap: existing uv, official installer, then pip as fallback.
# ---------------------------------------------------------------------------
$script:UvType = $null          # 'bin' or 'module'
$script:UvPath = $null
$script:UvModulePrefix = $null

function Invoke-Uv {
  param([string[]]$UvArgs)
  # Native stderr redirects become terminating errors under 'Stop' in
  # PowerShell 5.1; run native calls with 'Continue' and check exit codes.
  $oldPref = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    if ($script:UvType -eq 'bin') {
      & $script:UvPath @UvArgs
    } else {
      $head = $script:UvModulePrefix[0]
      $tail = @($script:UvModulePrefix[1..($script:UvModulePrefix.Count - 1)])
      & $head @tail @UvArgs
    }
  } finally {
    $ErrorActionPreference = $oldPref
  }
}

function Get-Uv {
  $cmd = Get-Command uv -ErrorAction SilentlyContinue
  if ($cmd) {
    $script:UvType = 'bin'
    $script:UvPath = $cmd.Source
    return
  }
  $fixed = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
  if (Test-Path $fixed) {
    $script:UvType = 'bin'
    $script:UvPath = $fixed
    return
  }

  Write-Step 'uv is not installed. Bootstrapping uv...'
  try {
    Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing -TimeoutSec 60 | Invoke-Expression
  } catch {
    Write-Warn ('Official uv installer failed: ' + $_.Exception.Message)
  }
  if (Test-Path $fixed) {
    $script:UvType = 'bin'
    $script:UvPath = $fixed
    return
  }
  $cmd = Get-Command uv -ErrorAction SilentlyContinue
  if ($cmd) {
    $script:UvType = 'bin'
    $script:UvPath = $cmd.Source
    return
  }

  Write-Warn "Official uv installer failed or timed out; trying pip + $script:MsagentIndex ..."
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) {
    $script:UvModulePrefix = @('py', '-3', '-m', 'uv')
    & py -3 -m pip install -q -U uv -i $script:MsagentIndex
    if ($LASTEXITCODE -ne 0) {
      Die "uv could not be installed. Install it manually (irm https://astral.sh/uv/install.ps1 | iex), then re-run this installer."
    }
  } else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
      Die 'python is required to bootstrap uv via pip. Install Python 3.11+, or install uv manually, then re-run.'
    }
    $script:UvModulePrefix = @('python', '-m', 'uv')
    & python -m pip install -q -U uv -i $script:MsagentIndex
    if ($LASTEXITCODE -ne 0) {
      Die "uv could not be installed. Install it manually (irm https://astral.sh/uv/install.ps1 | iex), then re-run this installer."
    }
  }
  # Prefer the uv.exe binary if pip put it somewhere discoverable.
  $scripted = Get-ChildItem (Join-Path $env:APPDATA 'Python\Python*\Scripts\uv.exe') -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($scripted) {
    $script:UvType = 'bin'
    $script:UvPath = $scripted.FullName
  } else {
    $script:UvType = 'module'
  }
}
Get-Uv

# ---------------------------------------------------------------------------
# Managed CPython downloads: prefer a domestic mirror for python-build-standalone.
# ---------------------------------------------------------------------------
if (-not $env:UV_PYTHON_INSTALL_MIRROR) {
  $env:UV_PYTHON_INSTALL_MIRROR = 'https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone'
  Write-Step "UV_PYTHON_INSTALL_MIRROR=$env:UV_PYTHON_INSTALL_MIRROR (set it yourself to override)"
}

# ---------------------------------------------------------------------------
# PATH helper
# ---------------------------------------------------------------------------
function Add-ToUserPath([string]$dir) {
  if ($env:MSAGENT_NO_MODIFY_PATH) {
    Write-Warn "Skipping PATH update (MSAGENT_NO_MODIFY_PATH set). Add $dir to PATH yourself."
    return
  }
  $current = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (-not $current) {
    [Environment]::SetEnvironmentVariable('Path', $dir, 'User')
    Write-Success "Added $dir to the user PATH (open a new terminal to use msagent)."
    return
  }
  $parts = @($current.Split(';') | Where-Object { $_ -ne '' })
  if ($parts -contains $dir) {
    Write-Step "$dir is already on the user PATH."
    return
  }
  # Prepend so the freshly installed msagent wins over older pip installs.
  [Environment]::SetEnvironmentVariable('Path', ($dir + ';' + $current.TrimStart(';')), 'User')
  Write-Success "Added $dir to the user PATH (prepended; open a new terminal to use msagent)."
}

function Get-ToolBinDir {
  $out = Invoke-Uv @('tool', 'dir', '--bin') 2>$null
  $line = $out | Select-Object -Last 1
  if ($line) {
    return $line.Trim()
  }
  return (Join-Path $env:USERPROFILE '.local\bin')
}

# ---------------------------------------------------------------------------
# venv fallback (last resort when uv tool install fails)
# ---------------------------------------------------------------------------
function Invoke-VenvFallback {
  $venvDir = if ($env:MSAGENT_FALLBACK_VENV) { $env:MSAGENT_FALLBACK_VENV } else { Join-Path $env:USERPROFILE '.msagent-venv' }
  $py = Get-Command py -ErrorAction SilentlyContinue
  if (-not $py) {
    Write-Warn 'py launcher not found; cannot create the fallback venv.'
    return $false
  }
  Write-Step "Creating isolated venv at $venvDir ..."
  if (-not (Test-Path $venvDir)) {
    & py -3.11 -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
      & py -3 -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0) {
      Write-Warn 'Could not create the fallback venv.'
      return $false
    }
  }
  $pip = Join-Path $venvDir 'Scripts\pip.exe'
  if (-not (Test-Path $pip)) {
    Write-Warn 'pip not found inside the fallback venv.'
    return $false
  }
  Write-Step "Installing $script:MsagentSpec from $script:MsagentIndex into the venv..."
  & $pip install -q -U -i $script:MsagentIndex $script:MsagentSpec
  if ($LASTEXITCODE -ne 0) {
    Write-Warn 'The venv fallback install failed.'
    return $false
  }
  $script:ToolBin = Join-Path $venvDir 'Scripts'
  Add-ToUserPath $script:ToolBin
  return $true
}

# uv sometimes only links the primary tool's executables into the tool bin
# directory. The msprof-mcp MCP server is spawned by name from PATH, so make
# sure its executable (and msprof-analyze) are reachable. Best effort only:
# create a small .cmd shim that forwards to the real executable in the tool env.
function Expose-ToolExecutables {
  if (-not $script:ToolBin) { return }
  $toolsRoot = Invoke-Uv @('tool', 'dir') 2>$null | Select-Object -Last 1
  if (-not $toolsRoot) { $toolsRoot = Join-Path $env:USERPROFILE '.local\share\uv\tools' }
  $envBin = Join-Path $toolsRoot.Trim() 'mindstudio-agent\Scripts'
  if (-not (Test-Path $envBin)) { return }
  foreach ($exe in @('msprof-mcp', 'msprof-analyze')) {
    $real = Join-Path $envBin "$exe.exe"
    if (Test-Path $real) {
      $shim = Join-Path $script:ToolBin "$exe.cmd"
      if (-not (Test-Path $shim)) {
        "@echo off`r`n`"$real`" %*" | Set-Content -Path $shim -Encoding Ascii
        Write-Success "Exposed $exe via $shim"
      }
    }
  }
}

# ---------------------------------------------------------------------------
# Install mindstudio-agent as an isolated uv tool (always latest, no version pin)
# ---------------------------------------------------------------------------
$pythonVersion = if ($env:MSAGENT_PYTHON) { $env:MSAGENT_PYTHON } else { '3.11' }
$script:MsagentSpec = 'mindstudio-agent'
if ($env:MSAGENT_VERSION) {
  $script:MsagentSpec = 'mindstudio-agent==' + $env:MSAGENT_VERSION
  Write-Step "Pinning MSAGENT_VERSION=$env:MSAGENT_VERSION"
}

if (Get-Command msagent -ErrorAction SilentlyContinue) {
  Write-Step 'Updating existing msagent installation...'
} else {
  Write-Step 'Installing msagent (latest) into an isolated uv tool environment...'
}

$installArgs = @('tool', 'install', '-U', '--python', $pythonVersion, '--default-index', $script:MsagentIndex)
$withExe = if ($env:MSAGENT_WITH_EXECUTABLES_FROM) { $env:MSAGENT_WITH_EXECUTABLES_FROM } else { 'msprof-mcp' }
$installArgs += @('--with-executables-from', $withExe)
$installArgs += $script:MsagentSpec

Invoke-Uv $installArgs
$installOk = ($LASTEXITCODE -eq 0)
if (-not $installOk) {
  # Retry once with a freshly upgraded uv: a pre-existing uv may be old or in
  # a broken state (e.g. failing managed-Python resolution on Windows).
  Write-Warn 'First install attempt failed; upgrading uv and retrying once...'
  $retryRunner = $null
  $pyRetry = Get-Command py -ErrorAction SilentlyContinue
  if ($pyRetry) {
    & py -3 -m pip install -q -U uv -i $script:MsagentIndex | Out-Null
    $retryRunner = @('py', '-3', '-m', 'uv')
  } else {
    $pythonRetry = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonRetry) {
      & python -m pip install -q -U uv -i $script:MsagentIndex | Out-Null
      $retryRunner = @('python', '-m', 'uv')
    }
  }
  if ($retryRunner) {
    $script:UvType = 'module'
    $script:UvModulePrefix = $retryRunner
    Invoke-Uv $installArgs
    $installOk = ($LASTEXITCODE -eq 0)
  }
}

if (-not $installOk) {
  # The tool may exist in a broken/stale state (e.g. its Python interpreter
  # no longer matches). Remove it and retry the install from scratch once.
  Write-Warn 'Install still failing; removing any existing msagent tool and retrying once more...'
  Invoke-Uv @('tool', 'uninstall', 'mindstudio-agent') | Out-Null
  Invoke-Uv $installArgs
  $installOk = ($LASTEXITCODE -eq 0)
}

if (-not $installOk) {
  Write-Host 'error: uv tool install failed. See the output above.' -ForegroundColor Red
  if ($env:MSAGENT_NO_FALLBACK) {
    Die "MSAGENT_NO_FALLBACK is set; skipping the venv fallback. Retry after fixing the issue, or install manually into a venv."
  }
  $useFallback = if ($env:MSAGENT_YES -eq '1') { $true } else { $false }
  if (-not $useFallback) {
    $answer = Read-Host 'Try the isolated venv fallback instead? [y/N]'
    $useFallback = ($answer -match '^(y|yes)$')
  }
  if ($useFallback) {
    if (-not (Invoke-VenvFallback)) {
      Die 'The venv fallback also failed. Please retry later or open an issue at https://gitcode.com/Ascend/msagent/issues'
    }
  } else {
    Die 'Install aborted. You can retry with: irm https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.ps1 | iex'
  }
} else {
  $script:ToolBin = Get-ToolBinDir
  Add-ToUserPath $script:ToolBin
}
if (-not $script:ToolBin) { $script:ToolBin = Get-ToolBinDir }
Expose-ToolExecutables

# Make msagent available in the current session immediately (the user-level
# PATH change only affects newly started processes).
if ($env:PATH -notlike "*$script:ToolBin*") {
  $env:PATH = $script:ToolBin + ';' + $env:PATH
  Write-Step "Updated PATH in this session; 'msagent' is ready now."
} else {
  Write-Step "'msagent' is already resolvable in this session."
}

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
$msagentExe = Join-Path $script:ToolBin 'msagent.exe'
if (Test-Path $msagentExe) {
  & $msagentExe --version
  if ($LASTEXITCODE -ne 0) {
    Die "msagent installed, but '--version' failed. Open a new terminal and re-run, or check your PATH."
  }
} elseif (Get-Command msagent -ErrorAction SilentlyContinue) {
  & msagent --version
  if ($LASTEXITCODE -ne 0) {
    Die "msagent installed, but '--version' failed. Open a new terminal and re-run, or check your PATH."
  }
} else {
  Write-Warn "msagent was installed but is not on PATH in this shell. Open a new terminal and run 'msagent --version'."
}

# Warn when an older pip-installed msagent shadows the uv tool on PATH.
$shadowMsagent = Get-Command msagent -ErrorAction SilentlyContinue
if ($shadowMsagent -and $shadowMsagent.Source -and $shadowMsagent.Source -ne $msagentExe) {
  Write-Warn "Note: 'msagent' on PATH resolves to $($shadowMsagent.Source) (possibly an older pip install)."
  Write-Warn "The uv tool install is at $msagentExe. Consider: pip uninstall mindstudio-agent"
}

$mcpExe = Join-Path $script:ToolBin 'msprof-mcp.exe'
$mcpShim = Join-Path $script:ToolBin 'msprof-mcp.cmd'
if ((Test-Path $mcpExe) -or (Test-Path $mcpShim)) {
  Write-Success 'msprof-mcp executable is available.'
} else {
  Write-Warn 'msprof-mcp is not on PATH in this shell yet; restart your shell if msagent reports it missing.'
}

Write-Success 'msagent installation complete.'
Write-Step 'Next steps:'
Write-Step '  msagent --help'
Write-Step '  msagent config --llm-provider openai --llm-model <model> ...   (see Quick Start)'
Write-Step 'Upgrade any time by re-running this installer (always installs the latest).'
Write-Step "If 'msagent' is still not found (e.g. in cmd), open a new window or run: set PATH=$script:ToolBin;%PATH%"
