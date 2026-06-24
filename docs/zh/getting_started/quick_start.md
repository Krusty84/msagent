# msAgent快速入门

本文介绍如何配置模型、选择 Agent 功能、启动并进入msAgent最小可用的交互流程。

## 1. 环境准备

1. 安装昇腾NPU驱动和配套版本的CANN软件（包含Toolkit和ops包）并配置环境变量，具体请参见《[CANN 快速安装](https://www.hiascend.com/cann/download)》。
2. 安装本工具，具体请参见《[msAgent安装指南](./install_guide.md)》。

## 2. 配置 LLM

1. 准备一个可用的 LLM API Key。

   需要用户自行登录模型服务商网站进行创建。

2. 配置 LLM。

   包括 LLM 服务的环境变量（*_API_KEY）、通过`msagent config`命令的`--llm-provider`参数配置LLM 服务的协议类型、`--llm-base-url`参数配置模型服务商地址、`--llm-model`参数配置模型名称（模型名称从模型服务商网站的模型广场获取）。

   | 配置场景 | 示例 |
   | --- | --- |
   | OpenAI 兼容接口（如 DeepSeek） | `export OPENAI_API_KEY="your-key"`<br>`msagent config --llm-provider openai --llm-base-url "https://api.deepseek.com" --llm-model "deepseek-v4-flash"` |
   | 本地 OpenAI 兼容服务 | `export OPENAI_API_KEY="dummy"`<br>`msagent config --llm-provider openai --llm-base-url "http://127.0.0.1:8000/v1" --llm-model "your-model"` |
   | Anthropic 兼容服务 | `export ANTHROPIC_API_KEY="your-key"`<br>`msagent config --llm-provider anthropic --llm-base-url "https://example.com/anthropic" --llm-model "claude-sonnet-4-20250514"` |
   | Google / Gemini 服务 | `export GOOGLE_API_KEY="your-key"`<br>`msagent config --llm-provider google --llm-base-url "https://example.com/google" --llm-model "gemini-2.5-pro"` |

3. 查看当前配置。

   ```bash
   msagent config --show
   ```

   显示步骤2配置的参数值则表示配置成功。

## 3. 启动会话

- 启动并进入默认交互式会话。

  ```bash
  msagent
  ```

- 常用 Agent 启动示例：

  | Agent | 说明 | 启动命令 |
  | --- | --- | --- |
  | [Profiler](../agent_guide/Profiler.md) | 性能调优 | `msagent --agent Profiler` |
  | [Accuracy](../agent_guide/Accuracy.md) | 精度调试 | `msagent --agent Accuracy` |
  | [Quantizer](../agent_guide/Quantizer.md) | 模型量化 | `msagent --agent Quantizer` |
  | [Operator](../agent_guide/Operator.md) | 算子调优 | `msagent --agent Operator` |
  | [Minos](../agent_guide/Minos.md) | 文档辅助 | `msagent --agent Minos` |

- 更多命令请参见《[msAgent使用指南](../user_guide/usemap.md)》。

## 4. 使用技巧

进入 msAgent 交互式会话后，以下技巧均以斜杠命令（slash command）的形式使用。

### 切换会话线程

会话历史会自动保存为独立线程，可随时浏览并恢复到之前的会话继续工作。

| 命令 | 说明 |
| --- | --- |
| `/threads` | 打开会话线程列表，按时间倒序展示预览摘要。做过 offload 的线程会显示 `[history offloaded]` 标记，仍可恢复查看。 |

![threads](../figures/threads.png)

### 选择并加载 Skill

Skill 是面向特定场景的专项能力模块（如性能分析、模型量化等）。输入 `/skills` 回车后，会打开交互式列表，通过上下键浏览选择、回车加载所需 Skill。

| 命令 | 说明 |
| --- | --- |
| `/skills` | 打开交互式 Skill 列表，上下键浏览、回车加载。 |
| `/skills <name>` | 直接指定 Skill 名称加载，同名冲突时带分类路径（如 `profiling/my-skill`）。 |
| `/skills <name> <task>` | 一步到位：加载 Skill 并直接传入任务执行。 |

已加载的 Skill 可直接作为斜杠快捷命令调用（如 `/my-skill`）。

![skills_browser](../figures/skills_browser.png)

### 安装自定义 Skill

除了内置 Skill，用户可通过 `/add-skill` 从本地路径安装自定义 Skill，满足个性化场景需求。支持指定 Skill 目录或 `SKILL.md` 文件，安装后立即生效。

| 命令 | 说明 |
| --- | --- |
| `/add-skill <path>` | 从本地路径安装 Skill 目录。 |
| `/add-skill <path>` | 也可直接指定 `SKILL.md` 文件路径。 |

![add_skill](../figures/add_skill.png)

### 查看工具输出详情

当 Agent 执行工具调用产生的输出较长时（如日志、配置文件、代码块等），输入 `/tool-output` 可在全屏查看器中浏览完整内容，避免输出截断影响阅读。也可用快捷键 `Ctrl+O` 直接打开。

| 操作 | 说明 |
| --- | --- |
| `/tool-output` 或 `Ctrl+O` | 打开全屏工具输出查看器。 |
| 左右方向键 | 切换多个工具输出。 |
| 上下方向键、`PageUp`/`PageDown` | 滚动内容。 |
| `Enter` / `Ctrl+O` / 鼠标点击 | 展开或折叠完整输出。 |
| `Esc` | 关闭查看器。 |

![tool_output](../figures/tool_output.png)

更完整的命令和快捷键说明请参见《[msAgent使用指南](../user_guide/usemap.md)》。
