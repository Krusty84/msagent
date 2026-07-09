# FAQ

## What local files does msagent create?

When `msagent` starts in a working directory for the first time, it creates:

```text
.msagent/
```

This directory stores local LLM configuration, Agent configuration, MCP configuration, prompts, Skills, logs, cache, checkpoints, and conversation history.

## Where should I put `.env`?

Place `.env` in the directory where you run `msagent`. For source development, the repository root is usually the most convenient location.

Example:

```text
OPENAI_API_KEY=your-key
```

Use `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY` for the corresponding provider. Do not commit `.env`.

## What is the difference between `export` and `.env`?

`export` sets an environment variable for the current shell session:

```bash
export OPENAI_API_KEY="your-key"
```

`.env` is a local file that can be reused across terminal sessions in the same working directory. Both can be used by `msagent`.

Check whether the key is recognized:

```bash
msagent config --show
```

`API Key` shows `Set` when a key is available; the real key is not printed.

## Should I use `pip` or `uv`?

Use `pip` when you only want to use the released package:

```bash
pip install mindstudio-agent
msagent --version
```

Use `uv` when working from the source repository:

```bash
uv sync --dev
uv run msagent --version
```

When running from source, replace `msagent ...` with `uv run msagent ...`.

## Does the LangChain warning affect usage?

`LangChainPendingDeprecationWarning` is usually an upstream dependency warning about future default behavior. If the command exits successfully and prints the expected output, it generally does not block current usage.

For real failures, check the exit code, traceback, and `.msagent/logs/app.log`.

## Windows / Git Bash notes

- Prefer running install, configuration, and `msagent` commands in the same Git Bash terminal.
- In the source repository, prefer `uv run msagent ...`.
- Use paths that your shell understands, such as `E:/Code/msagent` or `/e/Code/msagent`.
- Do not commit `.env`, `.msagent/`, `.venv/`, `dist/`, or `docs/_build/`.
