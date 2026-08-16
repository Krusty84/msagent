# msAgent Installation Guide

This page covers package installation and source-based setup. After installation, continue with the [Quick Start](quick_start.md) to configure an LLM and start a session.

## Requirements

- Python 3.11 or later.
- A supported operating system and the required Ascend software for NPU workflows.
- `glibc` 2.34 or later when using the `msprof-mcp` trace processor on Linux.

See the [CANN download page](https://www.hiascend.com/cann/download) for NPU drivers, CANN Toolkit, and ops packages.

## Install the Published Package

```bash
python -m pip install mindstudio-agent
msagent --version
msagent --help
```

The installation is usable when both commands exit successfully and print version and help information.

An unconstrained install selects the latest stable release. The current `master` documentation describes the 26.1 CLI and built-in Agents. Until a 26.1 stable package is available, install a matching 26.1 package with:

```bash
python -m pip install --pre --upgrade "mindstudio-agent>=26.1.0a2,<26.2"
```

Otherwise, use the commands and Agent names shown by the installed release's `msagent --help` output.

## Run from Source

Install [uv](https://docs.astral.sh/uv/), then run the following commands from a development environment supported by the repository:

```bash
git clone https://gitcode.com/Ascend/msagent.git
cd msagent
uv sync --dev
uv run msagent --version
```

Use `uv run msagent ...` instead of `msagent ...` for subsequent source-based commands. Repository builds and release packages use the standard MindStudio build environment described in the Chinese [installation guide](../../zh/getting_started/install_guide.md).

## Next Step

Continue with the [Quick Start](quick_start.md) to configure the model provider, API key, base URL, and model name.
