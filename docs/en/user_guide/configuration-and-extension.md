# Configuration and Extensions

msAgent keeps project-local runtime configuration under `.msagent/` in the selected working directory. The directory can also contain session history, memory, logs, checkpoints, and custom extensions, so do not delete it as a general troubleshooting step.

## Inspect and Update Model Configuration

Show the effective configuration without printing the API key value:

```bash
msagent config --show
```

Set the provider, compatible service URL, and model name:

```bash
msagent config \
  --llm-provider openai \
  --llm-base-url "https://api.deepseek.com" \
  --llm-model "deepseek-v4-flash"
```

For all supported options, see the [Interface Reference](../developer_guide/interface-reference.md) or run `msagent config --help`.

## Configure an API Key

Use the environment variable expected by the selected provider:

| Provider | Environment variable |
|---|---|
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `google` | `GOOGLE_API_KEY` |

Set a key for the current Bash session:

```bash
export OPENAI_API_KEY="your-key"
```

For project-local reuse, create `.env` in the directory where `msagent` runs:

```text
OPENAI_API_KEY=your-key
```

Do not commit `.env` or a real credential. Run `msagent config --show` and confirm that `API Key` reports `Set`.

## Working Directory

Use `--working-dir` when configuration belongs to another project directory:

```bash
msagent --working-dir /path/to/project
msagent config --working-dir /path/to/project --show
```

Both commands resolve project-local `.msagent/` content from that working directory. The `.env` file is loaded from the directory where the command is executed, so change into the project directory first when using `.env`.

## Skills and Other Extensions

- Create, install, and troubleshoot Skills with the [Skill Development Guide](../developer_guide/skill-development.md).
- Inspect available Skills in an interactive session with `/skills`.
- Install a local Skill with `/add-skill /path/to/my-skill`.
- See the Chinese [full configuration guide](../../zh/user_guide/configuration-and-extension.md) for MCP fields, loading precedence, and detailed Agent configuration.

## Troubleshooting

| Symptom | Check first |
|---|---|
| `API Key` reports `Not set` | The variable name matches the provider; `.env` is in the directory where the command is executed; the terminal was restarted after changing shell configuration. |
| A configuration change is ignored | The command and session use the same working directory; the intended `.msagent/agents/<Agent>.yml` was edited; a new session was started. |
| A Skill is missing | The directory is scanned, `SKILL.md` is valid, and the current Agent allows it in `skills.patterns`. |
