# msAgent Local Config

This directory stores project-local runtime configuration for `msagent`.

- `config.agents.yml`: agent selection and defaults
- `config.llms.yml`: LLM aliases and provider settings
- `config.mcp.json`: MCP server configuration, including `msprof-mcp`
- `config.approval.json`: deepagents Human-in-the-Loop (`interrupt_on`) plus fine-grained `decision_rules`
- `skills/`: project-local skills loaded in addition to the bundled default skills
- `sandboxes/`: sandbox profiles used by tools and MCP servers

These files are copied into `./.msagent/` on first run.

## Tavily API key setup

This README is the single source of truth for Tavily MCP configuration in the default local config template.

If you enable `tavily-mcp` in `config.mcp.json`, set `TAVILY_API_KEY` in the MCP server `env` block so the server can use the Tavily API directly.

Example:

```json
{
  "mcpServers": {
    "tavily-mcp": {
      "command": "npx",
      "args": ["-y", "tavily-mcp@latest"],
      "transport": "stdio",
      "env": {
        "TAVILY_API_KEY": "tvly-your-api-key-here"
      },
      "enabled": true,
      "stateful": true,
      "repair_timeout": 30,
      "invoke_timeout": 120.0
    }
  }
}
```

If you do not want to write the key directly into the file, you can also reference an existing environment variable:

```json
{
  "env": {
    "TAVILY_API_KEY": "${TAVILY_API_KEY}"
  }
}
```

Recommended steps:

1. Get your Tavily API key from the Tavily dashboard.
2. Open `.msagent/config.mcp.json`.
3. Find the `tavily-mcp` server entry.
4. Add `TAVILY_API_KEY` under `env`.
5. Restart the `msagent` session so the MCP server is started again with the new environment.

Notes:

- If `tavily-mcp` is enabled but no `TAVILY_API_KEY` is available, Tavily behavior depends on the MCP server implementation and runtime environment.
- In this project, when Tavily is enabled and a key is available, Tavily tools are preferred over the built-in `web_search`.
- When Tavily is enabled but the key is missing, the built-in `web_search` is kept as a fallback tool.
