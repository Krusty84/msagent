# msAgent Quick Start

This page shows how to configure a model, select an Agent, and enter a minimal working msAgent session.

## 1. Prepare the Environment

```bash
python -m pip install --pre --upgrade "mindstudio-agent>=26.1.0a2,<26.2"
msagent --version
```

This version range matches the 26.1 CLI and built-in Agents documented on this page. For the latest stable package or other installation methods, see the [Installation Guide](install_guide.md).

For source development or documentation validation, follow the Chinese [Contribution Guide](../../zh/developer_guide/contributing.md) and prefix later commands with `uv run`.

## 2. Configure the LLM

1. Obtain an API key from a model provider.

   Common provider links include:

   | Provider | Link |
   | --- | --- |
   | DeepSeek | [DeepSeek Platform](https://platform.deepseek.com/) |
   | Alibaba Cloud Model Studio | [Get an API key](https://help.aliyun.com/zh/model-studio/get-api-key) |

2. Configure the matching API key environment variable, provider, and model. A Base URL is required only for a compatible service, proxy, or self-hosted endpoint.

   DeepSeek and other OpenAI-compatible services:

   ```bash
   export OPENAI_API_KEY="your-key"
   msagent config --llm-provider openai --llm-base-url "https://api.deepseek.com" --llm-model "deepseek-v4-flash"
   ```

   Local OpenAI-compatible services:

   ```bash
   export OPENAI_API_KEY="dummy"
   msagent config --llm-provider openai --llm-base-url "http://127.0.0.1:8000/v1" --llm-model "your-model"
   ```

   Anthropic official service:

   ```bash
   export ANTHROPIC_API_KEY="your-key"
   msagent config --llm-provider anthropic --llm-base-url "" --llm-model "claude-sonnet-4-5"
   ```

   Google Gemini official service:

   ```bash
   export GOOGLE_API_KEY="your-key"
   msagent config --llm-provider google --llm-base-url "" --llm-model "gemini-2.5-pro"
   ```

   The Anthropic and Google Gemini model names come from the repository's built-in configuration. In the same configuration command, `--llm-base-url ""` clears a previously saved custom endpoint so that the provider default is used.

   To avoid exporting the key in every terminal, create `.env` in the directory where you run `msagent`:

   ```text
   OPENAI_API_KEY=your-key
   ```

   Do not commit `.env` to Git. See [FAQ](../user_guide/faq.md) for details.

3. Check the current configuration.

   ```bash
   msagent config --show
   ```

   `API Key` should show `Set`; the command does not print the actual key.

## 3. Start a Session

Start the default interactive session:

```bash
msagent
```

To select a built-in Agent at startup:

| Agent | Purpose | Command |
| --- | --- | --- |
| [Profiler](../agent_guide/Profiler.md) | Performance profiling and optimization | `msagent --agent Profiler` |
| [Accuracy](../agent_guide/Accuracy.md) | Accuracy analysis and debugging | `msagent --agent Accuracy` |
| [Quantizer](../agent_guide/Quantizer.md) | Model quantization | `msagent --agent Quantizer` |
| [Modeling](../agent_guide/Modeling.md) | Simulation modeling and automated optimization | `msagent --agent Modeling` |
| [Operator](../agent_guide/Operator.md) | Operator performance tuning | `msagent --agent Operator` |
| [Minos](../agent_guide/Minos.md) | Documentation assistance | `msagent --agent Minos` |

For all commands, see [Interface Reference](../developer_guide/interface-reference.md).

## 4. Usage Tips

Inside an interactive session, use slash commands for session and capability management.

### 4.1 Restore a Conversation Thread

Conversation history is saved as independent threads that can be browsed and restored.

| Command | Description |
| --- | --- |
| `/threads` | Open the thread list with recent conversations first. |

### 4.2 Select and Load a Skill

Use `/skills` to browse available Skills, or provide a Skill name directly.

| Command | Description |
| --- | --- |
| `/skills` | Open the interactive Skill list. |
| `/skills <skill-name>` | Load a specific Skill. |
| `/skills <skill-name> <prompt>` | Load a Skill and run a task. |

### 4.3 Install a Custom Skill

Use `/add-skill` to install a Skill directory or `SKILL.md` file from a local path. The Skill becomes available immediately.

| Command | Description |
| --- | --- |
| `/add-skill <path-to-skill>` | Install a custom Skill from a local path. |

### 4.4 Inspect Tool Output

Use `/tool-output` or `Ctrl+O` to inspect long tool output in the full-screen viewer.

| Action | Description |
| --- | --- |
| `/tool-output` or `Ctrl+O` | Open the tool output viewer. |
| Left and right arrows | Switch between tool outputs. |
| Up and down arrows, `PageUp`, or `PageDown` | Scroll the content. |
| `Enter`, `Ctrl+O`, or mouse click | Expand or collapse full output. |
| `Esc` | Close the viewer. |

### 4.5 Save Persistent Memory

Use `/remember` for information that should remain available in later sessions. Project memory is stored in `.msagent/memory.md` and loaded by later sessions.

| Command | Description |
| --- | --- |
| `/remember <content>` | Append an item to project memory. |
| `/showmemory` | Show the current project memory. |

Do not save API keys, passwords, tokens, or other secrets in project memory.

### 4.6 Record Intermediate Results

For long tasks, ask the Agent to write a Markdown report at important checkpoints so that the result can be loaded again after context compression. For example:

> Based on the analysis above, write a complete Markdown report with the issue summary, root cause, key data, and optimization recommendations.

For the complete command and hotkey reference, see [Interface Reference](../developer_guide/interface-reference.md).
