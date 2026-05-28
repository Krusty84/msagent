# 快速入门指导

本文承接 [入门安装指南](installation_guide.md)，介绍如何配置模型、选择 Agent，并进入最小可用的交互流程。

## 配置 LLM

当前 `config` 子命令直接支持 `openai`、`anthropic`、`google` 三类 Provider。对于自部署服务、企业网关或代理层，请根据接口协议兼容性复用上述 Provider，并通过 `--llm-base-url` 指定服务地址。

OpenAI 兼容接口示例：

```bash
export OPENAI_API_KEY="your-key"
msagent config --llm-provider openai --llm-base-url "https://api.deepseek.com/v1" --llm-model "deepseek-chat"
```

本地 OpenAI 兼容服务示例：

```bash
export OPENAI_API_KEY="dummy"
msagent config --llm-provider openai --llm-base-url "http://127.0.0.1:8000/v1" --llm-model "your-model"
```

Anthropic 兼容服务示例：

```bash
export ANTHROPIC_API_KEY="your-key"
msagent config --llm-provider anthropic --llm-base-url "https://example.com/anthropic" --llm-model "claude-sonnet-4-20250514"
```

Google / Gemini 服务示例：

```bash
export GOOGLE_API_KEY="your-key"
msagent config --llm-provider google --llm-base-url "https://example.com/google" --llm-model "gemini-2.5-pro"
```

查看当前配置：

```bash
msagent config --show
```

## 启动会话

进入默认交互式会话：

```bash
msagent
```

手动指定启动 Agent：

```bash
msagent --agent Hermes
msagent --agent Accuracy
msagent --agent Zephyr
msagent --agent Minos
msagent --agent Icarus
```

## 常用命令

| 命令 | 说明 |
|---|---|
| `/help` | 查看当前支持的命令列表。 |
| `/agents` | 打开 Agent 选择器。 |
| `/model` | 打开模型选择器。 |
| `/threads` | 浏览并恢复历史会话线程。 |
| `/tools` | 查看当前可用工具。 |
| `/skills` | 浏览当前可用 Skills。 |
| `/mcp` | 管理 MCP 服务启用状态。 |
| `/tool-output` | 打开最近一次可展开的工具输出。 |
| `/clear` | 清屏并开启新线程。 |
| `/exit` | 退出当前会话。 |

## 常用快捷键

| 快捷键 | 说明 |
|---|---|
| `Ctrl+C` | 有输入时清空输入框；连续按两次退出会话。 |
| `Ctrl+J` | 插入换行，便于多行输入。 |
| `Shift+Tab` | 循环切换审批模式。 |
| `Ctrl+B` | 切换 bash mode。 |
| `Ctrl+K` | 打开快捷键说明。 |
| `Ctrl+O` | 打开最近一次可展开的工具输出。 |
| `Tab` | 应用第一个补全项。 |
| `Enter` | 提交输入；如果当前选中了补全项，则先应用补全。 |

## 继续了解

- 常见问题见 [FAQ](../user_guide/faq.md)
- 本地配置、MCP 和 Skills 说明见 [配置与扩展](../user_guide/configuration-and-extension.md)
- 各 Agent 的能力说明见 [文档首页](../../index.md)
