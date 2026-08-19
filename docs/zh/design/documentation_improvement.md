# msAgent 文档完善提案技术方案设计（RFC）

状态（Status）：Reviewing

作者（Authors）：@weixin_56825549

创建日期（Created）：2026-08-15

更新日期（Updated）：2026-08-19

相关 Issue/PR：[Issue #10](https://gitcode.com/Ascend/msagent/issues/10)、[PR #115](https://gitcode.com/Ascend/msagent/pull/115)

---

## 1. 概述

### 1.1 简介

本提案完善 msAgent 面向用户和开发者的文档入口，补齐快速开始、命令行接口说明、Skill 开发部署排错、常见问题和多个 Agent 使用说明，并提供结构一致的中英文核心文档。目标是让新用户能够从安装进入首次可用会话，让开发者能够找到稳定的扩展与排错路径，同时保证新增页面可被 Sphinx 和 ReadTheDocs 收录。

本次工作仅调整文档及其导航，不改变 msAgent 的运行时代码、CLI 行为、配置格式或 Agent 能力。文档中的命令、参数和目录规则以当前仓库实现和命令帮助输出为依据。

### 1.2 动机

msAgent 已具备安装指南、用户指南和多个领域 Agent，但原有文档存在以下使用阻力：

- 新用户需要在多个页面间拼接安装、模型配置、配置校验和启动步骤。
- CLI 主命令与 `config` 子命令缺少独立、可检索的参数入口。
- Skill 的创建、加载、Agent 放行、部署位置和常见故障分散在配置或实现说明中。
- Agent 文档侧重领域介绍，启动方式、输入前提、输出预期和排错提示不够统一。
- 英文用户缺少覆盖核心上手流程、扩展入口和 Agent 定位的文档树。
- 新增页面若未加入 `toctree`，即使文件存在，也不会进入 ReadTheDocs 的主要导航。

如果不补齐这些入口，新用户仍需阅读源码或依赖维护者口头说明，示例命令也容易随 CLI 演进而失效；中英文内容缺少明确边界后，维护成本和内容漂移风险会持续增加。

### 1.3 目标

本提案的目标如下：

- 提供从安装、模型配置、配置校验到启动会话的最短可执行路径。
- 提供 `msagent`、`msagent config` 和交互式斜杠命令的中英文公开接口说明。
- 提供 Skill 开发、部署、Agent 放行、验证和排错的中英文入口。
- 至少覆盖 4 个 Agent，并统一说明其定位、启动方式、输入、输出和排错路径。
- 建立中英文核心文档的对应结构，英文范围明确为核心入口而非全量镜像。
- 将新增页面纳入 Sphinx/ReadTheDocs 导航，并通过 GitCode PR 远端文档门禁与本地构建验证。
- 复用仓库已有配置、贡献、过滤规则和 RTD 构建文档，避免重复维护同一事实。

本提案的非目标如下：

- 不修改 CLI、配置模型、Agent、MCP、Skill 加载逻辑或运行时行为。
- 不承诺将全部中文历史文档一次性翻译为英文。
- 不在快速开始中覆盖所有模型供应商、操作系统或部署拓扑。
- 不改变已有安装流程、架构设计或 ReadTheDocs 构建工具链。
- 不清理与本次新增页面无关的历史 Sphinx warning。

## 2. 用例分析

### 2.1 新用户完成首次启动

用户从文档首页进入安装和快速开始，完成以下闭环：

1. 安装发布包，或在源码仓库中使用 `uv` 运行。
2. 配置 LLM Provider、Base URL、模型名称和 API Key。
3. 使用 `msagent config --show` 确认配置已生效。
4. 启动默认 Agent 或指定领域 Agent。

示例不得包含真实密钥，必须明确 `.env` 只用于本地运行且不应提交到 Git。

### 2.2 用户选择领域 Agent

用户能够从统一导航看到 Profiler、Accuracy、Quantizer、Modeling、Operator 和 Minos 的领域边界。Profiler、Accuracy、Quantizer 和 Minos 作为本次四个核心验收对，在中英文页面中提供：

- 推荐使用场景与不适用边界。
- 可复制的启动命令。
- 运行前需要准备的数据或上下文。
- 预期输出和常见故障排查方向。

### 2.3 开发者查询 CLI 接口

开发者能够在独立接口说明中查询主命令、`config` 子命令和交互式斜杠命令，了解交互式会话、单次请求、工作目录、Agent 选择、模型选择、审批模式和配置检查等入口。CLI 参数仍以 `--help` 输出为准，文档不复制内部实现细节。

### 2.4 开发者创建和部署 Skill

开发者能够从 Skill 指南完成以下流程：

1. 创建最小 `SKILL.md` 和可选的 `scripts/`、`references/`。
2. 在目标 Agent 的 `skills.patterns` 中允许该 Skill。
3. 选择源码目录、项目目录或运行时安装目录。
4. 通过 `/skills` 验证可见性。
5. 按症状排查路径、命名、过滤规则、同名覆盖和依赖问题。

详细加载顺序和过滤语义继续由已有配置与过滤规则文档维护，Skill 指南只负责串联开发闭环。

### 2.5 文档维护者验证交付质量

文档维护者能够通过本地命令和 GitCode PR 远端检查验证：

- Markdown 代码块、标题和链接符合远端文档门禁要求。
- 新增资源与交叉引用存在。
- Sphinx HTML 构建成功，新增页面出现在 `toctree` 中。
- 文档中的 CLI 参数与当前 `--help` 输出一致。
- 中文核心页面在英文文档树中有对应入口。

### 2.6 验收要求

| 验收维度 | 要求 | 验证方式 |
|---|---|---|
| 安装与快速开始 | 覆盖安装、配置、校验和启动 | 按 Quick Start 顺序执行命令 |
| 配置与 FAQ | 覆盖本地配置、API Key、`.env` 和源码运行入口 | 检查交叉引用和配置输出 |
| 接口说明 | 覆盖主命令、`config` 与交互式斜杠命令 | 对照 CLI 帮助和斜杠命令注册表 |
| Skill 开发 | 覆盖开发、部署、放行、验证和排错 | 使用最小目录与配置示例走查 |
| Agent 文档 | 至少 4 个 Agent，结构和入口清晰 | 检查中英文 Agent 导航 |
| 中英双语 | 核心入口结构对应，范围说明明确 | 对照中英文 `toctree` |
| 构建 | Sphinx/ReadTheDocs 可正常收录新增页面 | 本地执行 Sphinx HTML 构建 |
| 示例安全 | 示例可复制，不包含真实凭据或危险命令 | 文档审查与密钥扫描 |

本提案不引入运行时性能指标。文档质量关注可执行性、可检索性、可维护性和构建可靠性。

## 3. 方案设计

### 3.1 总体方案

文档采用“中文完整入口、英文核心入口、专题页面复用已有权威说明”的组织方式：

```text
docs/index.md
├── 快速入门
│   ├── 安装指南
│   └── Quick Start
├── Agent 指南
│   └── Profiler / Accuracy / Quantizer / Modeling / Minos / Operator
├── 用户指南
│   ├── FAQ
│   └── 配置与扩展等现有专题
├── 开发指南
│   ├── 接口说明
│   ├── Skill 开发部署排错
│   ├── 贡献与 RTD 构建等现有专题
│   └── 本 RFC
└── English
    ├── Installation / Quick Start
    ├── Configuration / FAQ
    ├── Interface Reference / Skill Development / RTD Build
    └── 六个 Agent 核心页面
```

内容职责划分如下：

- Quick Start 先描述最短可用路径，再提供高频会话操作，并链接到安装、FAQ 和贡献指南。
- 安装、配置和 RTD 本地构建在中英文目录中各有独立核心入口。
- 接口说明提供稳定的 CLI 查询入口，并提醒以 `--help` 为准。
- Skill 指南串联开发到排错流程，复杂加载和过滤规则链接到已有专题。
- Agent 页面按统一问题回答“何时使用、如何启动、准备什么、得到什么、失败时查什么”。
- 英文文档覆盖核心入口，不复制全部中文专题，以减少双语漂移。
- `docs/index.md` 是 ReadTheDocs 主导航的唯一汇总入口。

### 3.2 技术选型

考虑过的方案如下：

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 仅扩写现有中文页面 | 文件少，初始改动小 | 无法满足中英文核心入口，专题边界继续混杂 | 不采用 |
| 新增一份大型综合手册 | 内容集中 | 重复已有安装、配置、贡献和 RTD 文档，后续容易过时 | 不采用 |
| 分层专题页面并复用已有文档 | 入口清晰，职责可维护，支持中英文核心结构 | 文件数量增加，需要维护导航和交叉引用 | 采用 |

构建技术沿用仓库现有 Sphinx、MyST Markdown、ReadTheDocs 配置和 `docs/requirements.txt`，不引入新的文档生成器或依赖。

### 3.3 功能与性能设计

#### 3.3.1 快速开始与 FAQ

快速开始保留发布包安装作为默认路径，并明确当前 26.1 文档与已发布版本的边界；源码运行作为贡献和本地验证补充。LLM 配置提供 OpenAI-compatible 及仓库内置 provider 示例，并通过 FAQ 说明 `export` 与 `.env` 的适用范围。配置是否成功统一使用 `msagent config --show` 检查，不打印真实 API Key。

FAQ 只保留高频、跨页面仍需要解释的问题；已有配置专题能够完整回答的内容使用链接复用，不在 FAQ 中重复字段级说明。

#### 3.3.2 接口说明

接口说明覆盖以下稳定入口：

- `msagent [message] [options]`
- `msagent config [options]`
- Agent、MCP、Skill 相关文档入口
- 本地验证命令

参数名称、可选值和默认语义应对照当前 CLI 帮助；新增或删除参数时，接口说明与中英文页面需要同步更新。

#### 3.3.3 Skill 开发部署排错

Skill 指南提供最小可运行结构和生命周期：创建、Agent 放行、部署、发现、调用与排错。加载优先级、Pattern 语法和源码/wheel 差异由以下现有文档维护：

- [配置与扩展](../user_guide/configuration-and-extension.md)
- [Agent / Tool / Skill 过滤规则](../user_guide/agent-tool-skill-filter-rules.md)
- [贡献指南](../developer_guide/contributing.md)

#### 3.3.4 Agent 文档

中文 Agent 页面在原有定位基础上补充启动方式、输入要求、输出预期和排错；英文页面提供对应的核心说明。Profiler、Accuracy、Quantizer 和 Minos 采用统一最小字段验收，Modeling 与 Operator 保留已有领域深度并提供启动、输入和输出入口。不同 Agent 的领域知识和依赖差异保留在各自页面，不抽象成可能失真的统一输入格式。

#### 3.3.5 构建与导航

所有新增核心页面必须进入中文或英文 `toctree`。本地构建以仓库 `.readthedocs.yaml` 和 `docs/requirements.txt` 为准；构建产物写入 `docs/_build/`，不得提交到 Git。

### 3.4 安全隐私与 DFX 设计

#### 3.4.1 安全与隐私

- API Key 示例只使用 `your-key` 或本地无鉴权服务所需的明确占位值。
- 文档不展示、记录或提交真实密钥、令牌、日志和本地配置。
- `.env`、`.msagent/`、`.venv/` 和构建产物保持在 Git 忽略范围内。
- 示例不要求关闭 TLS 校验、扩大文件权限或执行与目标无关的危险命令。

#### 3.4.2 兼容性

- 普通用户命令使用已发布包的 `msagent` 入口；Agent 名称和参数以安装版本的帮助输出为准。
- 源码开发命令使用仓库推荐的 `uv run msagent` 入口。
- Shell 示例采用项目现有文档使用的 Bash 语法；平台差异交由安装和贡献指南维护。
- 英文核心页面与中文页面保持命令一致，不要求段落逐字翻译。

#### 3.4.3 可维护性

- 一个事实只指定一个权威专题，其他页面使用交叉引用。
- CLI 变更需要同步检查中英文 Quick Start 和接口说明。
- Agent 增删或改名需要同步首页、两种语言的 Agent 导航和对应页面。
- Skill 加载规则变更由配置专题维护，Skill 指南只更新流程影响。

#### 3.4.4 可测试性与可靠性

- 使用 `git diff --check` 检查空白和补丁格式。
- 推送后使用 GitCode PR 远端文档门禁检查 Markdown、链接、资源和标签闭合。
- 使用 Sphinx 强制重新读取文档，验证导航、引用和构建结果。
- 使用 CLI `--help` 输出核对参数，不依赖记忆或第三方说明。

### 3.5 编程与调用设计

本提案不新增或修改 Python API。开发者调用设计体现在对现有 CLI 与 Skill 扩展约定的文档化。

#### 3.5.1 编程模型基本设计

**开发环境设计**：源码文档验证使用 Python、`uv`、Sphinx、MyST Parser 和仓库现有文档依赖。普通用户不需要安装文档构建依赖。

**开发约束**：命令示例必须能在对应安装方式下执行；模型名称和服务地址属于示例，用户需要替换为供应商实际提供的值；真实凭据不得进入文档或仓库。

**可验收设计**：按第 2.6 节验收矩阵执行，并记录本地构建结果、warning 基线和 GitCode PR 远端文档门禁状态。

#### 3.5.2 接口定义和设计

本提案记录但不变更以下接口：

| 接口 | 输入 | 输出 | 约束 |
|---|---|---|---|
| `msagent [message] [options]` | 会话参数和可选消息 | 交互式会话或单次回复 | 参数以 `msagent --help` 为准 |
| `msagent config [options]` | Provider、模型、Base URL 等配置参数 | 更新或展示项目本地配置 | API Key 展示必须脱敏 |
| `SKILL.md` | frontmatter、工作流说明及可选资源 | 可被 Agent 发现和调用的 Skill | 必须满足目录扫描和 Pattern 过滤规则 |

调用示例由 Quick Start、接口说明和 Skill 指南分别维护，本 RFC 不重复完整参数表。

#### 3.5.3 编程手册设计

本提案不新增独立手册产品。Quick Start、接口说明、Skill 指南、Agent 指南、FAQ 和既有配置/贡献专题共同组成可维护的开发与使用手册。新增内容通过 `docs/index.md` 和 `docs/en/index.md` 进入现有文档站点。

## 4. 缺点和风险

| 风险 | 影响 | 应对措施 |
|---|---|---|
| 中英文内容漂移 | 两种语言出现不同命令或 Agent 列表 | CLI 或 Agent 变更时成对检查核心页面 |
| 与已有专题重复 | 同一规则在多个页面出现不同版本 | 明确权威专题，其他页面改用链接和流程摘要 |
| CLI 示例过时 | 用户复制后执行失败 | 发布前对照 `--help`，避免记录内部实现细节 |
| 外部链接失效 | GitCode PR 远端文档门禁失败或用户无法继续 | 优先使用仓库内相对链接，外链纳入有效性检查 |
| 文档范围持续扩张 | PR 难审查，后续维护成本增加 | 坚持核心英文范围和本提案非目标 |
| 历史构建 warning 混入 | 难以判断本次改动是否回归 | 记录基线，确保本次不新增 warning 或 error |

本提案不产生 Breaking Change，不需要版本迁移，也不增加运行时依赖。主要成本是新增页面的持续维护和双语核心内容同步。

## 5. 现有技术

本提案复用以下仓库能力：

- Sphinx：负责文档解析、导航和 HTML 构建。
- MyST Parser：允许使用 Markdown 编写 Sphinx 文档和 `toctree`。
- ReadTheDocs：依据仓库现有配置自动安装依赖并构建文档。
- GitCode PR 远端文档门禁：检查 Markdown 规范、链接、资源和标签闭合；该检查不由仓库内脚本提供。
- 现有中文专题：安装、配置与扩展、过滤规则、贡献指南和 RTD 本地构建说明。

与新增一套文档平台相比，沿用当前技术栈能够保持构建入口、主题和维护流程一致。本提案的差异在于补齐信息架构与核心中英文入口，不改变底层发布方式。

## 6. 未解决问题

当前没有待决策的设计问题；是否合入仍以维护者审查和 GitCode PR 远端门禁结果为准。后续可由社区单独讨论：

- 是否逐步扩展英文文档至所有中文用户和开发专题。
- 是否为 CLI 文档增加从参数定义自动生成参考页的能力。
- 是否把历史 Sphinx warning 清理拆分为独立任务。

这些问题不影响本次核心文档交付，也不应继续扩大 PR #115 的范围。

---

## 附录

### A. 文档更新范围

| 类别 | 中文入口 | 英文入口 |
|---|---|---|
| 安装 | `docs/zh/getting_started/install_guide.md` | `docs/en/getting_started/install_guide.md` |
| 快速开始 | `docs/zh/getting_started/quick_start.md` | `docs/en/getting_started/quick_start.md` |
| 配置 | `docs/zh/user_guide/configuration-and-extension.md` | `docs/en/user_guide/configuration-and-extension.md` |
| FAQ | `docs/zh/user_guide/faq.md` | `docs/en/user_guide/faq.md` |
| 接口说明 | `docs/zh/developer_guide/interface-reference.md` | `docs/en/developer_guide/interface-reference.md` |
| Skill 指南 | `docs/zh/developer_guide/skill-development.md` | `docs/en/developer_guide/skill-development.md` |
| RTD 本地构建 | `docs/zh/developer_guide/readthedocs-local-build.md` | `docs/en/developer_guide/readthedocs-local-build.md` |
| Agent 指南 | `docs/zh/agent_guide/` | `docs/en/agent_guide/` |
| 导航 | `docs/index.md` | `docs/en/index.md` |

### B. 验证命令

以下命令用于本地复现 CLI、Sphinx 构建和补丁格式检查：

```bash
uv run msagent --version
uv run msagent --help
uv run msagent config --help
uv run msagent config --show
python -m pip install -r docs/requirements.txt
python -m sphinx -E -b html docs docs/_build/html
git diff --check origin/master
```

Markdown、链接、资源和标签闭合检查由推送后的 GitCode PR 远端文档门禁执行，结果以 PR 检查页面为准。

### C. 参考资料

- [贡献指南](../developer_guide/contributing.md)
- [配置与扩展](../user_guide/configuration-and-extension.md)
- [ReadTheDocs 本地验证说明](../developer_guide/readthedocs-local-build.md)
- [Agent / Tool / Skill 过滤规则](../user_guide/agent-tool-skill-filter-rules.md)
