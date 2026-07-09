# Interface Reference

This page summarizes the common CLI and configuration interfaces. For interactive slash commands, see the Chinese [usage guide](../../zh/user_guide/usemap.md). For local configuration files, see [configuration and extension](../../zh/user_guide/configuration-and-extension.md).

## Main Command

```bash
msagent [options] [message]
```

Without `message`, `msagent` starts an interactive session. With `message`, it sends a one-shot request.

| Option | Description |
|---|---|
| `--stream` | Stream model output. |
| `--no-stream` | Render only the final response. |
| `-v`, `--verbose` | Enable verbose logging; logs are written to `.msagent/logs/app.log`, and the terminal shows the log file path. |
| `-w`, `--working-dir` | Set the working directory for the session. |
| `-a`, `--agent` | Select an Agent, such as `Profiler`, `Accuracy`, `Quantizer`, `Modeling`, `Operator`, or `Minos`. |
| `-m`, `--model` | Select an LLM model alias. |
| `-am`, `--approval-mode` | Select tool approval mode: `semi-active`, `active`, or `aggressive`. |
| `--trace-jsonl` | Write trace events to a JSONL file. |

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

## Configuration Command

```bash
msagent config [options]
```

| Option | Description |
|---|---|
| `--show`, `-s` | Show current configuration. API keys are masked. |
| `--llm-provider` | Set the provider, such as `openai`, `anthropic`, or `google`. |
| `--llm-base-url` | Set the base URL for a compatible service or proxy. |
| `--llm-model`, `-m` | Set the model name. |
| `--llm-api-key` | Set an API key for this process. Environment variables or `.env` are recommended for daily use. |
| `--llm-max-tokens` | Set max output tokens. `0` means provider or model default. |
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

## Related Interfaces

- Skill development: [Skill Development](skill-development.md)
- Agent, Tool, and Skill filters: [Chinese filter guide](../../zh/user_guide/agent-tool-skill-filter-rules.md)
- MCP configuration: [Chinese configuration guide](../../zh/user_guide/configuration-and-extension.md)
