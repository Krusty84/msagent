# 接口说明

本文覆盖当前公开的 `msagent` 主命令、`config` 和交互式斜杠命令。配置文件结构见 [配置与扩展](../user_guide/configuration-and-extension.md)，具体操作流程见 [msAgent 使用指南](../user_guide/usemap.md)。

## 使用分级

- **基础**：首次安装、配置和启动时需要了解。
- **常用**：日常会话和能力管理中按需使用。
- **高级**：诊断、上下文管理或定制集成时使用。

该分级仅用于阅读顺序，不表示权限或接口稳定性。

## 1. 主命令

```bash
msagent [message] [options]
```

不传 `message` 时进入交互式会话；传入 `message` 时发送一次任务请求。

| 级别 | 参数 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 基础 | `[message]` | 不传时进入交互会话 | 发送一次任务请求。 |
| 基础 | `-h`, `--help` | - | 显示主命令帮助。 |
| 基础 | `-V`, `--version` | - | 显示版本并退出；该参数直接跟在 `msagent` 后。 |
| 基础 | `--stream` | 已启用 | 使用流式输出。 |
| 常用 | `--no-stream` | 未启用 | 只渲染最终回复。 |
| 高级 | `-v`, `--verbose` | 未启用 | 启用详细日志输出。 |
| 常用 | `-w`, `--working-dir` | 当前目录 | 指定会话工作目录。 |
| 常用 | `-a`, `--agent` | 当前配置 | 指定 `Profiler`、`Accuracy`、`Quantizer`、`Modeling`、`Operator` 或 `Minos`。 |
| 常用 | `-m`, `--model` | 当前 Agent 配置 | 指定 LLM 模型别名。 |
| 高级 | `--timer` | 未启用 | 输出启动阶段计时。 |
| 高级 | `-am`, `--approval-mode` | `active` | 指定工具审批模式：`semi-active`、`active` 或 `aggressive`。 |
| 高级 | `--trace-jsonl <path>` | 不写入文件 | 将 trace 事件写入 JSONL 文件。 |

示例：

```bash
msagent --agent Profiler
msagent --no-stream "请总结当前仓库的文档入口"
msagent --approval-mode active --trace-jsonl traces/run.jsonl
```

源码运行时，在命令前添加 `uv run`：

```bash
uv run msagent --agent Profiler
```

## 2. 交互式斜杠命令

进入交互式会话后可使用以下完整命令集：

| 类别 | 级别 | 命令 | 说明 |
| --- | --- | --- | --- |
| 帮助 | 基础 | `/help` | 显示可用斜杠命令。 |
| 帮助 | 常用 | `/hotkeys` | 显示键盘快捷键。 |
| 能力选择 | 基础 | `/agents` | 打开 Agent 选择器。 |
| 能力选择 | 常用 | `/model` | 打开模型选择器。 |
| 能力选择 | 常用 | `/skills [<skill-name> [task...]]` | 浏览 Skill，或加载指定 Skill 并执行任务。 |
| 能力选择 | 常用 | `/<skill-name> [task...]` | 名称唯一且不与内置命令冲突时，直接调用 Skill。重名时使用 `/<category>:<name>` 或 `/<category>/<name>`。 |
| 会话 | 常用 | `/threads` | 浏览并恢复历史会话线程。 |
| 会话 | 基础 | `/clear` | 清空屏幕并创建新线程。 |
| 会话 | 基础 | `/exit` | 退出 msAgent。 |
| 扩展管理 | 常用 | `/tools` | 打开工具选择器。 |
| 扩展管理 | 常用 | `/mcp` | 打开 MCP 管理界面。 |
| 扩展管理 | 常用 | `/add-skill <path>` | 从本地路径安装自定义 Skill。 |
| 上下文 | 高级 | `/remember <content>` | 将内容保存到当前项目的长期记忆。 |
| 上下文 | 高级 | `/showmemory` | 查看当前项目的长期记忆。 |
| 上下文 | 高级 | `/offload` | 汇总较早消息，并将原始历史转存到后端存储。 |
| 诊断 | 高级 | `/tool-output` | 打开最近一次工具输出的交互式查看器。 |

## 3. 配置命令

```bash
msagent config [options]
```

不传配置修改参数时，与 `--show` 一样显示当前配置。

| 级别 | 参数 | 默认行为 | 说明 |
| --- | --- | --- | --- |
| 基础 | `-h`, `--help` | - | 显示配置命令帮助。 |
| 基础 | `--show`, `-s` | 无修改参数时自动显示 | 查看当前配置；API Key 只显示 `Set` 或 `Not set`。 |
| 基础 | `--llm-provider <provider>` | 保持当前值 | 设置 provider：`openai`、`anthropic`、`google` 或 `gemini`。`gemini` 是 `google` 的别名。 |
| 基础 | `--llm-model <model>`, `-m <model>` | 保持当前值 | 设置模型名称。 |
| 常用 | `--llm-base-url <url>` | 保持当前值 | 设置兼容服务或代理的 Base URL；与其他非空配置参数一同传入空字符串时，可清除现有覆盖值。 |
| 常用 | `--llm-max-tokens <number>` | 保持当前值 | 设置最大输出 token；`0` 表示使用 provider 或模型默认值。 |
| 常用 | `-w`, `--working-dir <path>` | 当前目录 | 指定项目本地 `.msagent` 配置所在的工作目录。 |
| 高级 | `--llm-api-key <key>` | 不设置 | 仅在本次 `config` 进程中设置 API Key，不为后续会话持久化。 |
| 高级 | `-v`, `--verbose` | 未启用 | 启用详细日志输出。 |

DeepSeek 等 OpenAI-compatible 服务示例：

```bash
export OPENAI_API_KEY="your-key"
msagent config --llm-provider openai --llm-base-url "https://api.deepseek.com" --llm-model "deepseek-v4-flash"
msagent config --show
```

也可以在运行命令的工作目录创建 `.env`：

```text
OPENAI_API_KEY=your-key
```

`.env` 和 `export` 的区别见 [FAQ](../user_guide/faq.md)。

### Provider 与 API Key

| Provider | 别名 | 默认环境变量 | 用途 |
| --- | --- | --- | --- |
| `openai` | - | `OPENAI_API_KEY` | OpenAI 及 OpenAI-compatible 服务。 |
| `anthropic` | - | `ANTHROPIC_API_KEY` | Anthropic 服务及兼容代理。 |
| `google` | `gemini` | `GOOGLE_API_KEY` | Google Gemini 服务。 |

官方服务不需要自定义 Base URL。若此前配置过兼容服务或代理，请在设置 provider 或模型的同一条命令中加入 `--llm-base-url ""` 清除旧地址；不要单独运行该参数。

## 4. Agent、MCP 与 Skill 入口

- Agent 列表和领域边界见 [文档首页](../../index.md) 的“内置 Agent 与能力分工”。
- Agent YAML、Tool 和 Skill 过滤规则见 [Agent / Tool / Skill 过滤规则](../user_guide/agent-tool-skill-filter-rules.md)。
- MCP 配置字段见 [配置与扩展](../user_guide/configuration-and-extension.md)。
- Skill 开发、部署与排错见 [Skill 开发部署排错](skill-development.md)。

## 5. 本地验证命令

修改接口相关文档后，至少运行：

```bash
msagent --version
msagent --help
msagent config --help
msagent config --show
```

源码运行时，将上述命令写成 `uv run msagent ...`。
