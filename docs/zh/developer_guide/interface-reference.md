# 接口说明

本文汇总 `msagent` 常用命令行接口、模型配置参数和扩展入口。更完整的交互式命令见 [msAgent 使用指南](../user_guide/usemap.md)，配置文件结构见 [配置与扩展](../user_guide/configuration-and-extension.md)。

## 主命令

```bash
msagent [options] [message]
```

不传 `message` 时进入交互式会话；传入 `message` 时会直接发送一次任务请求。

常用参数如下：

| 参数 | 说明 |
|---|---|
| `--stream` | 使用流式输出。 |
| `--no-stream` | 只渲染最终回复，不逐 token 输出。 |
| `-v`, `--verbose` | 日志始终写入 `.msagent/logs/app.log`；使用该参数时，终端会显示日志文件位置。 |
| `-w`, `--working-dir` | 指定会话工作目录，默认是当前目录。 |
| `-a`, `--agent` | 指定 Agent，例如 `Profiler`、`Accuracy`、`Quantizer`、`Modeling`、`Operator`、`Minos`。 |
| `-m`, `--model` | 指定 LLM 模型别名。 |
| `--timer` | 输出启动阶段计时，便于定位启动耗时。 |
| `-am`, `--approval-mode` | 指定工具审批模式，可选 `semi-active`、`active`、`aggressive`。 |
| `--trace-jsonl` | 将 trace 事件写入指定 JSONL 文件，便于排查执行链路。 |
| `--version` | 输出当前 msAgent 版本。 |

示例：

```bash
msagent --agent Profiler
msagent --no-stream "请总结当前仓库的文档入口"
msagent --approval-mode active --trace-jsonl traces/run.jsonl
```

源码运行时，可使用：

```bash
uv run msagent --agent Profiler
```

## Web 命令

发布包默认不包含 Web UI 依赖，首次使用前请安装可选 extra：

```bash
python -m pip install "mindstudio-agent[web]"
```

源码运行时可使用 `uv sync --extra web` 安装依赖，并将下列命令写成 `uv run msagent web ...`。

```bash
msagent web [options]
```

`web` 子命令启动 LangGraph API 服务，并默认同时启动 deep-agents-ui 前端。常用参数如下：

| 参数 | 说明 |
|---|---|
| `--host` | 指定 LangGraph 服务监听地址。 |
| `--port` | 指定 LangGraph 服务端口。 |
| `--ui-port` | 指定 deep-agents-ui 前端端口。 |
| `--no-ui` | 只启动 LangGraph API，不启动前端。 |
| `--no-open` | 启动后不自动打开浏览器。 |
| `-v`, `--verbose` | 在终端显示日志文件位置。 |
| `-w`, `--working-dir` | 指定项目工作目录。 |
| `-a`, `--agent` | 指定启动的 Agent。 |
| `-m`, `--model` | 指定 LLM 模型别名。 |

完整集成方式见 [msAgent 集成指南](../user_guide/integration-guide.md)。

## 配置命令

```bash
msagent config [options]
```

常用参数如下：

| 参数 | 说明 |
|---|---|
| `--show`, `-s` | 查看当前配置。API Key 只显示是否已设置，不打印真实值。 |
| `--llm-provider` | 设置 provider，例如 `openai`、`anthropic`、`google`。 |
| `--llm-base-url` | 设置 OpenAI-compatible 服务、代理或自部署服务地址。 |
| `--llm-model`, `-m` | 设置模型名称。 |
| `--llm-api-key` | 仅为本次 `config` 命令进程设置 API Key，不会持久化到后续会话；日常使用请配置环境变量或 `.env`。 |
| `--llm-max-tokens` | 设置最大输出 token，`0` 表示使用 provider 或模型默认值。 |
| `-v`, `--verbose` | 在终端显示日志文件位置。 |
| `-w`, `--working-dir` | 指定项目本地 `.msagent` 配置目录所在工作目录。 |

示例：

```bash
export OPENAI_API_KEY="your-key"
msagent config --llm-provider openai --llm-base-url "https://api.deepseek.com" --llm-model "deepseek-v4-flash"
msagent config --show
```

也可以在运行命令的工作目录创建 `.env`，写入：

```text
OPENAI_API_KEY=your-key
```

`.env` 和 `export` 的区别见 [FAQ](../user_guide/faq.md)。

## Provider 与 API Key

| Provider | 默认环境变量 | 说明 |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | 适用于 OpenAI 及 OpenAI-compatible 服务。 |
| `anthropic` | `ANTHROPIC_API_KEY` | 适用于 Anthropic-compatible 服务。 |
| `google` | `GOOGLE_API_KEY` | 适用于 Google / Gemini 服务。 |

自部署或第三方兼容服务通常复用对应 provider，并通过 `--llm-base-url` 指向服务地址。

## Agent、MCP 与 Skill 入口

- Agent 列表和领域边界见 [文档首页](../../index.md) 的“内置 Agent 与能力分工”。
- Agent YAML、Tool 和 Skill 过滤规则见 [Agent / Tool / Skill 过滤规则](../user_guide/agent-tool-skill-filter-rules.md)。
- MCP 配置字段见 [配置与扩展](../user_guide/configuration-and-extension.md)。
- Skill 开发、部署与排错见 [Skill 开发部署排错](skill-development.md)。

## 本地验证命令

修改接口相关文档后，建议至少运行：

```bash
msagent --version
msagent --help
msagent config --help
msagent web --help
msagent config --show
```

源码运行时，将上述命令替换为 `uv run msagent ...`。
