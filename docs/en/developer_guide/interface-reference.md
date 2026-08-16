# Interface Reference

This page covers the public `msagent` command, `config`, `web`, and interactive slash commands. For local configuration files, see [Configuration and Extensions](../user_guide/configuration-and-extension.md). For workflow examples, see the Chinese [msAgent Usage Guide](../../zh/user_guide/usemap.md).

## Usage Levels

- **Basic**: needed for initial installation, configuration, and startup.
- **Common**: used as needed during normal sessions and capability management.
- **Advanced**: used for diagnostics, context management, or custom integration.

These levels indicate a suggested reading order only. They do not indicate permissions or API stability.

## 1. Main Command

```bash
msagent [options] [message]
```

Without `message`, `msagent` starts an interactive session. With `message`, it sends a one-shot request.

| Level | Option | Default | Description |
| --- | --- | --- | --- |
| Basic | `[message]` | Interactive session when omitted | Send a one-shot request. |
| Basic | `-h`, `--help` | - | Show main command help. |
| Basic | `--version` | - | Show the version and exit; place this option directly after `msagent`. |
| Basic | `--stream` | Enabled | Stream model output. |
| Common | `--no-stream` | Disabled | Render only the final response. |
| Advanced | `-v`, `--verbose` | Disabled | Logs are always written to `.msagent/logs/app.log`; enabling this option prints the log path. |
| Common | `-w`, `--working-dir` | Current directory | Set the session working directory. |
| Common | `-a`, `--agent` | Current configuration | Select `Profiler`, `Accuracy`, `Quantizer`, `Modeling`, `Operator`, or `Minos`. |
| Common | `-m`, `--model` | Current Agent configuration | Select an LLM model alias. |
| Advanced | `--timer` | Disabled | Print startup timing information. |
| Advanced | `-am`, `--approval-mode` | `active` | Select `semi-active`, `active`, or `aggressive`. |
| Advanced | `--trace-jsonl <path>` | No file | Write trace events to a JSONL file. |

Examples:

```bash
msagent --agent Profiler
msagent --no-stream "Summarize this repository."
msagent --approval-mode active --trace-jsonl traces/run.jsonl
```

From source, prefix the command with `uv run`:

```bash
uv run msagent --agent Profiler
```

## 2. Interactive Slash Commands

The interactive session provides the following complete command set:

| Category | Level | Command | Description |
| --- | --- | --- | --- |
| Help | Basic | `/help` | Show available slash commands. |
| Help | Common | `/hotkeys` | Show keyboard shortcuts. |
| Capability | Basic | `/agents` | Open the Agent selector. |
| Capability | Common | `/model` | Open the model selector. |
| Capability | Common | `/skills [<skill-name> [task...]]` | Browse Skills, or load a Skill and run a task. |
| Capability | Common | `/<skill-name> [task...]` | Invoke a uniquely named Skill when it does not conflict with a built-in command. For duplicates, use `/<category>:<name>` or `/<category>/<name>`. |
| Session | Common | `/threads` | Browse and restore conversation threads. |
| Session | Basic | `/clear` | Clear the screen and start a new thread. |
| Session | Basic | `/exit` | Exit msAgent. |
| Extensions | Common | `/tools` | Open the tool selector. |
| Extensions | Common | `/mcp` | Open MCP management. |
| Extensions | Common | `/add-skill <path>` | Install a custom Skill from a local path. |
| Context | Advanced | `/remember <content>` | Save content to persistent project memory. |
| Context | Advanced | `/showmemory` | Show persistent project memory. |
| Context | Advanced | `/offload` | Summarize older messages and offload raw history to backend storage. |
| Diagnostics | Advanced | `/tool-output` | Open the interactive viewer for the latest tool output. |

## 3. Configuration Command

```bash
msagent config [options]
```

With no configuration update options, the command displays the current configuration, like `--show`.

| Level | Option | Default behavior | Description |
| --- | --- | --- | --- |
| Basic | `-h`, `--help` | - | Show configuration command help. |
| Basic | `--show`, `-s` | Used automatically with no update options | Show configuration; API keys appear only as `Set` or `Not set`. |
| Basic | `--llm-provider <provider>` | Keep current value | Set `openai`, `anthropic`, `google`, or `gemini`. `gemini` is an alias for `google`. |
| Basic | `--llm-model <model>`, `-m <model>` | Keep current value | Set the model name. |
| Common | `--llm-base-url <url>` | Keep current value | Set the Base URL for a compatible service or proxy; an empty string clears the override only when the same command includes another non-empty update option. |
| Common | `--llm-max-tokens <number>` | Keep current value | Set max output tokens; `0` means provider or model default. |
| Common | `-w`, `--working-dir <path>` | Current directory | Set the working directory for project-local `.msagent` configuration. |
| Advanced | `--llm-api-key <key>` | Not set | Set an API key only for this `config` process; it is not persisted for later sessions. |
| Advanced | `-v`, `--verbose` | Disabled | Print the log file path. |

Example for DeepSeek and other OpenAI-compatible services:

```bash
export OPENAI_API_KEY="your-key"
msagent config --llm-provider openai --llm-base-url "https://api.deepseek.com" --llm-model "deepseek-v4-flash"
msagent config --show
```

You can instead create `.env` in the directory where you run the command:

```text
OPENAI_API_KEY=your-key
```

### Provider and API Key Mapping

| Provider | Alias | Default environment variable | Use |
| --- | --- | --- | --- |
| `openai` | - | `OPENAI_API_KEY` | OpenAI and OpenAI-compatible services. |
| `anthropic` | - | `ANTHROPIC_API_KEY` | Anthropic services and compatible proxies. |
| `google` | `gemini` | `GOOGLE_API_KEY` | Google Gemini services. |

Official services do not require a custom Base URL. If a compatible service or proxy was configured earlier, include `--llm-base-url ""` in the same command that sets the provider or model. Do not run the empty option by itself.

## 4. Web Command

The published package does not include Web UI dependencies by default. Install the optional extra before first use:

```bash
python -m pip install --pre --upgrade "mindstudio-agent[web]>=26.1.0a2,<26.2"
```

The frontend also requires Node.js. The source or fallback frontend path uses `npm` and `npx`; verify that the commands are available before startup:

```bash
node --version
npm --version
npx --version
```

To run only the LangGraph API without the frontend or Node.js, use `msagent web --no-ui`.

For a source checkout, first run `uv sync --extra web`, then use `uv run msagent web ...`.

```bash
msagent web [options]
```

By default, `web` starts both the LangGraph API server and the deep-agents-ui frontend.

| Level | Option | Default | Description |
| --- | --- | --- | --- |
| Basic | `-h`, `--help` | - | Show Web command help. |
| Basic | `--host <host>` | `127.0.0.1` | Set the LangGraph server host interface. |
| Basic | `--port <port>` | `2024` | Set the LangGraph server port. |
| Basic | `--ui-port <port>` | `3000` | Set the deep-agents-ui frontend port. |
| Common | `--no-ui` | Disabled | Start only the LangGraph API server. |
| Common | `--no-open` | Disabled | Do not open a browser after startup. |
| Common | `-w`, `--working-dir <path>` | Current directory | Set the project working directory. |
| Common | `-a`, `--agent <name>` | Current configuration | Select the Agent. |
| Common | `-m`, `--model <alias>` | Current Agent configuration | Select an LLM model alias. |
| Advanced | `-v`, `--verbose` | Disabled | Print the log file path. |

For integration details, see the Chinese [msAgent Integration Guide](../../zh/user_guide/integration-guide.md).

## 5. Related Interfaces

- Skill development: [Skill Development](skill-development.md)
- Agent, Tool, and Skill filters: [Chinese Filter Guide](../../zh/user_guide/agent-tool-skill-filter-rules.md)
- MCP configuration: [Configuration and Extensions](../user_guide/configuration-and-extension.md)

## 6. Local Verification

After changing interface documentation, run at least:

```bash
msagent --version
msagent --help
msagent config --help
msagent web --help
msagent config --show
```

From source, prefix these commands with `uv run`.
