# msAgent Quick Start

This page shows the shortest path to install, configure, and start `msagent`. For source development, use `uv run msagent ...` instead of `msagent ...`.

## 1. Prepare the Environment

For normal usage, install the published package:

```bash
pip install mindstudio-agent
msagent --version
```

For source-based development or documentation validation, run commands from the repository with `uv run msagent ...`.

## 2. Configure the LLM

Prepare an API key from your model provider, then configure a provider, base URL, and model name.

OpenAI-compatible services:

```bash
export OPENAI_API_KEY="your-key"
msagent config --llm-provider openai --llm-base-url "https://api.deepseek.com" --llm-model "deepseek-v4-flash"
```

Local OpenAI-compatible services:

```bash
export OPENAI_API_KEY="dummy"
msagent config --llm-provider openai --llm-base-url "http://127.0.0.1:8000/v1" --llm-model "your-model"
```

If you do not want to export the key every time, create a local `.env` file in the directory where you run `msagent`:

```text
OPENAI_API_KEY=your-key
```

Do not commit `.env` to Git. See [FAQ](../user_guide/faq.md) for details.

Check the current configuration:

```bash
msagent config --show
```

`API Key` should show `Set`; the command does not print the actual key.

## 3. Start a Session

Start the default interactive session:

```bash
msagent
```

Start a specific Agent:

```bash
msagent --agent <AgentName>
```

Available built-in Agents include `Profiler`, `Accuracy`, `Quantizer`, `Modeling`, `Minos`, and `Operator`.

Send a one-shot request:

```bash
msagent --no-stream "Summarize what this repository is used for."
```

## 4. Use Skills

Inside an interactive session:

```text
/skills
```

Load a specific Skill:

```text
/skills ascend-computation-analysis
```

For custom Skill development and troubleshooting, see [Skill Development, Deployment, and Troubleshooting](../developer_guide/skill-development.md).
