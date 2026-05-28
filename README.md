<div align="center">
<p><b><span style="font-size:24px;">面向 Ascend NPU 场景的一站式调试调优 Agent</span></b></p>

<p align="center">
  <img src="https://raw.githubusercontent.com/luelueFLY/images/main/img/msagent-hello.gif" alt="msAgent" width="720">
</p>

[![PyPI](https://badgen.net/pypi/v/mindstudio-agent?label=PyPI)](https://pypi.org/project/mindstudio-agent/)
[![文档](https://readthedocs.org/projects/mindstudio-agent/badge/?version=latest)](https://mindstudio-agent.readthedocs.io/zh-cn/latest/)
[![安装指南](https://badgen.net/badge/安装指南/Install/blue)](#-安装指南)
[![快速入门](https://badgen.net/badge/快速入门/QuickStart/blue)](#-快速入门)
[![配置文档](https://badgen.net/badge/配置文档/Docs/blue)](docs/zh/user_guide/configuration-and-extension.md)
[![昇腾社区](https://badgen.net/badge/昇腾社区/Community/blue)](https://www.hiascend.com/cn/developer/software/mindstudio)
[![报告问题](https://badgen.net/badge/报告问题/Issues/blue)](https://gitcode.com/Ascend/msagent/issues)

</div>

## ✨ 最新消息

<span style="font-size:14px;">

🔹 **[2026.05.21]**：`v26.0.0已发布，新增Icarus Agent，覆盖算子性能调优场景`。
🔹 **[2026.04.27]**：`v26.0.0.alpha1` 发布，新增 `Accuracy` / `Zephyr` Agent，覆盖精度调优与模型量化场景。  
🔹 **[2026.04.08]**：`v0.1.3` 发布，完成 DeepAgents 重构并增强 `Hermes` / `Minos` Agent。  
🔹 **[2026.03.19]**：`mindstudio-agent` 已发布到 PyPI，推荐使用 `pip install -U mindstudio-agent` 安装。
  

</span>

## ℹ️ 简介

MindStudio-Agent（简称 `msagent`）是面向昇腾 Ascend NPU 开发、调试和调优场景的 AI Agent 工作台。它将 CLI、多模型 Provider、MCP 工具、内置 Skills 与领域 Agent 组合在一起，帮助用户在性能调优、精度分析、模型量化、算子优化、文档体验与代码审查等任务中更快定位问题并形成可执行建议。

## ⚙️ 功能介绍

| 名称 | 领域定位 | 核心能力 |
|---|---|---|
| **Hermes** | 性能调优 | 聚焦 Ascend Profiling 分析，覆盖单卡、多卡、集群等场景，擅长快慢卡、慢节点、MFU、通信瓶颈、算子热点、下发调度等性能问题定位与优化建议。 |
| **Accuracy** | 精度调优 | 聚焦 Ascend 精度分析与优化，覆盖 RL 训推一致性分析、loss / gnorm NaN 分析等常见精度问题。 |
| **Zephyr** | 模型量化 | 聚焦 msModelSlim 量化与压缩场景，协助完成模型适配可行性、结构风险评估与基础适配器开发。 |
| **Icarus** | 算子调优 | 聚焦 Ascend NPU 算子性能调优，包括算子性能深度分析、端到端算子性能优化，辅助提升调优效率并降低开发难度。 |
| **Minos** | 文档体验与代码审查 | 聚焦 README 走查、安装流程验证、Quick Start 体验、新手 onboarding、文档可用性评估，以及 GitCode PR 审查与评审意见整理。 |

## 📦 安装指南

### 环境要求

🔹 Python `3.11+`  
🔹 推荐使用 `uv` 管理源码运行环境  
🔹 至少准备一个可用的 LLM API Key  
🔹 glibc `>= 2.34`，用于满足 `msprof-mcp` 中 `trace_processor` 二进制依赖  

### PyPI 安装

推荐优先使用 PyPI 安装稳定发布版本：

```bash
pip install -U mindstudio-agent
msagent
```

### 源码运行

如果你需要跟踪最新源码、参与开发，或同步最新内置 Skills，可以使用源码运行方式：

```bash
git clone https://gitcode.com/Ascend/msagent.git
cd msagent
uv sync
uv run msagent
```

> 源码运行时，下文命令中的 `msagent` 可替换为 `uv run msagent`。

🔹 **构建与打包**：需要生成 wheel 或检查构建产物时，请参见 [《编译与打包》](docs/zh/developer_guide/build-and-package.md)。  
🔹 **版本与兼容性**：Python 版本、Provider 支持和内置 MCP 版本说明，请参见 [《版本与兼容性》](docs/zh/developer_guide/version-and-compatibility.md)。  
🔹 **完整安装说明**：Web UI、日志与版本检查等更多内容，请参见 [《入门安装指南》](docs/zh/quick_start/installation_guide.md)。

## 🚀 快速入门

```bash
pip install -U mindstudio-agent
```

### 1. 配置 LLM

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

### 2. 选择 Agent

```bash
msagent --agent Hermes
msagent --agent Accuracy
msagent --agent Zephyr
msagent --agent Minos
msagent --agent Icarus
```


## 📘 使用指南

进入交互式会话后，可以直接输入问题，也可以配合 `/` 命令和快捷键提升效率。

| 命令 | 说明 |
|---|---|
| `/hotkeys` | 查看键盘快捷键说明。 |
| `/agents` | 打开 Agent 选择器。 |
| `/model` | 打开模型选择器。 |
| `/threads` | 浏览并恢复历史会话线程。 |
| `/tools` | 查看当前可用工具。 |
| `/skills` | 浏览当前可用 Skills。 |
| `/mcp` | 管理 MCP 服务启用状态。 |
| `/offload` | 压缩并卸载较早的会话消息。 |
| `/tool-output` | 打开最近一次可展开的工具输出。 |
| `/clear` | 清屏并开启新线程。 |
| `/exit` | 退出当前会话。 |

### 输入区快捷键

| 快捷键 | 说明 |
|---|---|
| `Ctrl+C` | 有输入时清空输入框；连续按两次退出会话。 |
| `Ctrl+J` | 插入换行，便于多行输入。 |
| `Shift+Tab` | 循环切换审批模式。 |
| `Ctrl+B` | 切换 bash mode。 |
| `Ctrl+K` | 直接打开快捷键说明。 |
| `Ctrl+O` | 打开最近一次可展开的工具输出。 |
| `Tab` | 应用第一个补全项。 |
| `Enter` | 提交输入；如果当前选中了补全项，则先应用补全。 |

### 工具输出查看器

当某次工具调用支持展开查看时，可用 `Ctrl+O` 或 `/tool-output` 打开工具输出查看器。查看器内支持：

- 左右方向键：切换不同工具调用输出
- 上下方向键、`PageUp` / `PageDown`、`Home` / `End`：滚动内容
- 点击、`Ctrl+O` 或 `Enter`：展开或折叠完整输出
- `Esc`：关闭查看器

更多配置、MCP、Skills 与加载顺序说明，请参见 [《配置与扩展》](docs/zh/user_guide/configuration-and-extension.md)。

## 📚 API 参考

MindStudio-Agent 当前主要通过 CLI、配置文件和 Skills 扩展机制使用。开发与扩展相关参考如下：

🔹 [《Agent / Tool / Skill 过滤规则》](docs/zh/user_guide/agent-tool-skill-filter-rules.md)  
🔹 [《上下文压缩指南》](docs/zh/user_guide/context-compaction-guide.md)  
🔹 [《Retry Middleware 指南》](docs/zh/user_guide/retry-middleware-guide.md)  
🔹 [《架构概览》](docs/zh/developer_guide/arch_overview.md)  

## ❓ FAQ

常见问题与排查入口请参见 [《FAQ》](docs/zh/user_guide/faq.md)。

## 🌌 智能检索

为提升文档查阅效率，建议优先通过以下入口定位信息：  
🔹 [中文文档首页](docs/index.md)：按快速入门、用户指南、Agent 指南和开发指南组织内容。  
🔹 [配置与扩展](docs/zh/user_guide/configuration-and-extension.md)：查询本地配置目录、MCP 配置、Skills 扩展与加载顺序。  
🔹 [版本与兼容性](docs/zh/developer_guide/version-and-compatibility.md)：查询版本要求、兼容策略与内置依赖。  
🔹 会话内直接询问 `msagent`：让对应 Agent 结合仓库文档、配置和上下文辅助定位问题。  

## 🛠️ 贡献指南

欢迎提交 Issue、PR 或补充新的领域 Skills。开始贡献前，建议先阅读 [《编译与打包》](docs/zh/developer_guide/build-and-package.md)、[《配置与扩展》](docs/zh/user_guide/configuration-and-extension.md) 与 [《Agent / Tool / Skill 过滤规则》](docs/zh/user_guide/agent-tool-skill-filter-rules.md)。

## ⚖️ 相关说明

🔹 [《版本与兼容性》](docs/zh/developer_guide/version-and-compatibility.md)  
🔹 [《配置与扩展》](docs/zh/user_guide/configuration-and-extension.md)  
🔹 [Mulan PSL v2 许可证](http://license.coscl.org.cn/MulanPSL2)  
🔹 [提交问题与建议](https://gitcode.com/Ascend/msagent/issues)  

## 🤝 建议与交流

| 技术交流 | 问题反馈 | 社区入口 |
|:---:|:---:|:---:|
| <a href="https://applink.feishu.cn/client/chat/chatter/add_by_link?link_token=854v5833-c03a-484e-8aac-0637f0303dc4&qr_code=true"><img src="https://img.shields.io/badge/Feishu-3370FF?style=for-the-badge&logo=lark&logoColor=white" alt="Feishu Group"></a><br><sub>加入飞书群交流使用体验与问题定位</sub> | [![Issues](https://badgen.net/badge/GitCode/Issues/blue)](https://gitcode.com/Ascend/msagent/issues)<br><sub>提交 Bug、需求和文档建议</sub> | [![Community](https://badgen.net/badge/Ascend/Community/blue)](https://www.hiascend.com/cn/developer/software/mindstudio)<br><sub>了解 MindStudio 与昇腾开发者资源</sub> |

## 🙏 致谢

感谢 MindStudio 相关团队、Ascend 生态伙伴以及社区开发者对项目能力、文档体验和使用反馈的持续贡献。欢迎通过 Issue、PR 或 Skills 扩展一起完善 MindStudio-Agent。
