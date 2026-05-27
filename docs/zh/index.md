# MindStudio-Agent 中文文档

MindStudio-Agent 是面向 Ascend NPU 场景的一站式调试调优 Agent。中文文档现在统一维护在 `docs/zh/` 下，按新用户上手、日常使用、Agent 能力和开发维护四类组织。

```{toctree}
:maxdepth: 2
:caption: 快速入门

quick_start/installation_guide
quick_start/op_tool_quick_start
```

```{toctree}
:maxdepth: 2
:caption: Agent 指南

agent_guide/Hermes
agent_guide/Accuracy
agent_guide/Zephyr
agent_guide/Minos
agent_guide/Icarus
```

```{toctree}
:maxdepth: 2
:caption: 用户指南

user_guide/faq
user_guide/configuration-and-extension
user_guide/document-ux-review
user_guide/context-compaction-guide
user_guide/retry-middleware-guide
user_guide/agent-tool-skill-filter-rules
```

```{toctree}
:maxdepth: 2
:caption: 开发指南

developer_guide/build-and-package
developer_guide/version-and-compatibility
developer_guide/arch_overview
developer_guide/tag-release
developer_guide/test_docs
```

## 内置 Agent 与能力分工

| 形象 | 名称 | 领域定位 | 说明 |
|---|---|---|---|
| <img src="./images/Hermes.png" alt="Hermes" width="120"> | **Hermes** | 性能调优 | 聚焦 Ascend Profiling 分析，覆盖单卡、多卡、集群等场景，擅长快慢卡、慢节点、MFU、通信瓶颈、算子热点、下发调度等性能问题定位与优化建议。详见 [Hermes 说明](agent_guide/Hermes.md)。 |
| <img src="./images/Accuracy.png" alt="Accuracy" width="120"> | **Accuracy** | 精度调优 | 聚焦 Ascend 精度分析与优化，覆盖 RL 训推一致性分析、loss / gnorm NaN 分析等常见精度问题。详见 [Accuracy 说明](agent_guide/Accuracy.md)。 |
| <img src="./images/Zephyr.jpg" alt="Zephyr" width="120"> | **Zephyr** | 模型量化 | 聚焦 msModelSlim 量化与压缩场景，协助完成模型适配可行性、结构风险评估与基础适配器开发。详见 [Zephyr 说明](agent_guide/Zephyr.md)。 |
| <img src="./images/Minos.png" alt="Minos" width="120"> | **Minos** | 文档体验与代码审查 | 聚焦 README 走查、安装流程验证、Quick Start 体验、新手 onboarding、文档可用性评估，以及 GitCode PR 审查与评审意见整理。详见 [Minos 说明](agent_guide/Minos.md)。 |
| <img src="./images/Icarus.png" alt="Icarus" width="120"> | **Icarus** | 算子调优 | 聚焦 Ascend NPU 算子性能调优，包括算子性能深度分析、端到端算子性能优化，辅助提升调优效率并降低开发难度。详见 [Icarus 说明](agent_guide/Icarus.md)。 |

## 推荐阅读路径

新用户建议先阅读 [入门安装指南](quick_start/installation_guide.md)，完成安装后再按 [快速入门指导](quick_start/op_tool_quick_start.md) 配置模型并启动会话。

日常使用中，优先从 [FAQ](user_guide/faq.md) 和 [配置与扩展](user_guide/configuration-and-extension.md) 定位常见问题；需要参与开发、发布或本地验证文档时，再进入开发指南。
