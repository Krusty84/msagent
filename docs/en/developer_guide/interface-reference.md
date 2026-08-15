# Interface Reference

This page summarizes the common CLI and configuration interfaces. For interactive slash commands, see the Chinese [usage guide](../../zh/user_guide/usemap.md). For local configuration files, see [Configuration and Extensions](../user_guide/configuration-and-extension.md).

## Main Command

```bash
msagent [options] [message]
```

Without `message`, `msagent` starts an interactive session. With `message`, it sends a one-shot request.

| Option | Description |
|---|---|
| `--stream` | Stream model output. |
| `--no-stream` | Render only the final response. |
| `-v`, `--verbose` | Logs are always written to `.msagent/logs/app.log`; this option prints the log file path in the terminal. |
| `-w`, `--working-dir` | Set the working directory for the session. |
| `-a`, `--agent` | Select an Agent, such as `Profiler`, `Accuracy`, `Quantizer`, `Modeling`, `Operator`, or `Minos`. |
| `-m`, `--model` | Select an LLM model alias. |
| `--timer` | Print startup timing information for diagnostics. |
| `-am`, `--approval-mode` | Select tool approval mode: `semi-active`, `active`, or `aggressive`. |
| `--trace-jsonl` | Write trace events to a JSONL file. |
| `--version` | Print the current msAgent version. |

Examples:

```bash
msagent --agent Profiler
msagent --no-stream "Summarize this repository."
msagent --approval-mode active --trace-jsonl traces/run.jsonl
```

From source:

```bash
uv run msagent --agent Profiler
```

## Web Command

The published package does not include Web UI dependencies by default. Install the optional extra before first use:

```bash
python -m pip install "mindstudio-agent[web]"
```

For a source checkout, use `uv sync --extra web` and run the following command as `uv run msagent web ...`.

```bash
msagent web [options]
```

The `web` subcommand starts the LangGraph API server and, by default, the deep-agents-ui frontend.

| Option | Description |
|---|---|
| `--host` | Set the LangGraph server host interface. |
| `--port` | Set the LangGraph server port. |
| `--ui-port` | Set the deep-agents-ui frontend port. |
| `--no-ui` | Start only the LangGraph API server. |
| `--no-open` | Do not open the browser after startup. |
| `-v`, `--verbose` | Print the log file path in the terminal. |
| `-w`, `--working-dir` | Set the project working directory. |
| `-a`, `--agent` | Select the Agent. |
| `-m`, `--model` | Select an LLM model alias. |

For integration details, see the Chinese [msAgent integration guide](../../zh/user_guide/integration-guide.md).

## Configuration Command

```bash
msagent config [options]
```

| Option | Description |
|---|---|
| `--show`, `-s` | Show current configuration. API keys are reported only as `Set` or `Not set`. |
| `--llm-provider` | Set the provider, such as `openai`, `anthropic`, or `google`. |
| `--llm-base-url` | Set the base URL for a compatible service or proxy. |
| `--llm-model`, `-m` | Set the model name. |
| `--llm-api-key` | Set an API key only for this `config` process. It is not persisted for later sessions; use an environment variable or `.env` for normal use. |
| `--llm-max-tokens` | Set max output tokens. `0` means provider or model default. |
| `-v`, `--verbose` | Print the log file path in the terminal. |
| `-w`, `--working-dir` | Set the working directory for project-local `.msagent` configuration. |

Example:

```bash
export OPENAI_API_KEY="your-key"
msagent config --llm-provider openai --llm-base-url "https://api.deepseek.com" --llm-model "deepseek-v4-flash"
msagent config --show
```

## Provider and API Key Mapping

| Provider | Default environment variable |
|---|---|
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `google` | `GOOGLE_API_KEY` |

For self-hosted or third-party compatible services, reuse the matching provider and set `--llm-base-url`.

Verify the documented interfaces with:

```bash
msagent --version
msagent --help
msagent config --help
msagent web --help
```

## Related Interfaces

- Skill development: [Skill Development](skill-development.md)
- Agent, Tool, and Skill filters: [Chinese filter guide](../../zh/user_guide/agent-tool-skill-filter-rules.md)
- MCP configuration: [Configuration and Extensions](../user_guide/configuration-and-extension.md)
