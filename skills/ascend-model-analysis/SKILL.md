---
name: ascend-model-analysis
description: |
  分析 LLM / 多模态模型架构并生成结构化报告，输出 HTML 报告 + 供 Agent 消费的 Markdown + JSON 文档。
  在用户提出"模型结构分析"、"架构分析报告"、"KVCache 估算"、"显存矩阵"，
  或指向 HuggingFace / 本地模型目录要求结构分析时触发。
---

# Ascend 模型结构分析

你是一个模型结构分析专家。你的任务是根据模型配置文件（config 文件、model.safetensors.index.json）和模型源码，自动分析模型结构特点，生成分析报告。

## 1. 交付物总览（两份文件必须同源）

每次分析必须产出两个文件，写到工作目录。`<model-name>` 取自 config 目录名或用户指定：

| 文件                                  | 受众         | 用途                              |
| ----------------------------------- | ---------- | ------------------------------- |
| `<model-name>-analysis-report.html` | 人          | 一份模型结构分析报告。                     |
| `<model-name>-analysis-agent.md`    | Agent / 脚本 | 支持对接 profiling 性能拆解、模型结构可视化等下游环节。 |

两份文件的**数据必须同源**——只允许呈现形式不同，不允许数据不一致。

## 2. 获取模型相关文件

详见 `references/file-acquisition.md`。要点摘要：

- 两种来源：本地目录 / HuggingFace 下载。选定后核对文件齐全与否，缺口向用户说明风险。
- 仅下载文本小文件，**绝不下载 safetensors 权重分片**；唯一例外是 `model.safetensors.index.json`（几 MB 的 JSON 索引）。
- 仓库根目录全部 `.py` 一律下载；`config.json` 是唯一硬性必备项，其余文件按仓库实际目录树拉取，没有就跳过。
- HF 仓库缺 `modeling_*.py` 时，按"本地 transformers → weight-map 逆向"顺序降级，详见 `references/code-fallback.md`。

## 3. 分析方法：架构识别与计算公式

详见 `references/arch-recognition.md`（架构识别表 + 12 种特征交叉判断）和 `references/param-formulas.md`（参数量 / KVCache / 显存矩阵公式 + 单位规范）。本节只给输入前置条件和章节使用指南。

### 输入前置条件

- 必填：`hidden_size`, `num_hidden_layers`, `num_attention_heads`, `vocab_size`
- MoE 必填：`n_routed_experts`, `num_experts_per_tok`
- 稀疏 attn 必填（按类型）：`index_n_heads` / `sparse_topk_blocks`
- 缺哪个键就在 Ch0 标注"无法估算 X"，**不要默认填 0**

### Shape 约定

`B`=batch、`S`=当前 query 长度、`T`=KV 总长（S+past）、`T_i`=分派给专家 i 的 token 数。attention 张量 `[B, heads, S, head_dim]`，hidden state `[B, S, hidden_size]`。

## 4. HTML 报告：章节结构与样式规范

中文、白底、简洁，CSS 全部内联（模板见 `references/css-template.md`），表格 `min-width:600px`、容器 `overflow-x:auto`。

章节按输入组合（决策表）：

| 输入条件                            | 章节                       |
| ------------------------------- | ------------------------ |
| 无 model card                     | Ch1–Ch3                  |
| 有 model card                    | Ch0 + Ch1–Ch3            |

各章要点：

- **Ch0 模型概览**（仅 model card）：官方核心指标 grid（总/激活参数、上下文长度、吞吐）、官方信息对照表、目标场景、推理生态支持。官方数字与独立估算不一致必须对账。
- **Ch1 架构概览**：核心参数表、关键指标 grid（总参数/激活参数/层数/注意力类型）、层分布图（每种层类型一个色块）、层类型汇总表。
- **Ch2 单层算子分析**：每种层类型一张 DFG 图 + 算子明细表（5 列：#/算子名称/输入Shape/输出Shape/功能描述）。DFG 用水平并行 lane 表达 Q/K/V，右侧红色虚线表示残差，`.dfg-2col` 并排 attention 与 FFN。算子配色 norm=蓝、mm=绿、act=黄、attn=紫、shape=灰、merge=红、route=橙、dsa/conv=青。shape 必须是 config 推出的具体数字。表按阶段用彩色 title 行分组（attention/DSA/FFN-MoE/共享专家/门控）。多模态额外拆 vision tower + projector。章末放算子图例与数量汇总。
- **Ch3 KVCache 与参数量**：每 token KV 占用与压缩比、参数量分项表、总/激活参数汇总（与官方对账）、量化×并行显存矩阵（MoE 时 8 行）。

## 5. Agent 文档：职责拆分与 JSON 块

文件名 `<model-name>-analysis-agent.md`，章节与 HTML 报告一致（Ch0 / Ch1 / Ch2 / Ch3）。目标：让下游 Agent/脚本**不解析 HTML** 就能拿到全部结构化数据。

职责拆分就一条：**给人看的叙述与表格用 Markdown，其余能 JSON 表达的数据一律用独立 JSON 块**（每块可直接 `json.loads`，数值纯数字、单位放字段名）。

典型两例：

- **算子明细表** → Markdown 表格（仅算子名称 + 功能描述，无 shape）
- **算子数据流 DFG** → 独立 JSON 块（即 DFG 拓扑）：`nodes`（id / op / stage）+ `edges`（from / to）+ 可选 `groups`；**只留拓扑、严禁 shape**，残差用「分支源 → Add」edge 表达

其余（层超参、attention / mask 配置、MoE 路由、量化×并行显存矩阵等）一律走独立 JSON 块，块名自解释（正例 `op-dfg` / `params` / `memory-matrix`，反例 `data3` / `block1`）；**KVCache 与参数量可使用 Markdown 表格**（人可读，便于核对公式）。

## 6. 工作流

1. **确定模型来源**（见 `references/file-acquisition.md`）— 只给模型名就先 WebSearch 定位 HF 仓库；本地目录直接分析，URL/id 走 HF 下载。
2. **读 model card**（若有）— `WebFetch`/`Read` 或已在对话中；失败则退回 `README.md` 或请用户粘贴。
3. **读 config + 模型实现代码** — 缺 `modeling_*.py` 时按 `references/code-fallback.md` 降级处理。
4. **识别架构与层变体**（`references/arch-recognition.md`）。
5. **用 Python 计算参数量与 KVCache**（`python -c "..."` 单块，显式打印中间量作为校验证据）。覆盖每层 attn/MLP/MoE/indexer/norm、每层类型合计、全模型总量、激活/每 token、每 token 每层 KV 字节、目标 context 总量。lm_head 口径在 Ch3 显式声明。
6. **交叉校验**官方数据，对账差异。
7. **用 Python 计算显存矩阵**（MoE 模型），默认 EP∈{8,16,32,64}×{W8A8,W4A8}。
8. **读** `references/css-template.md` 拿 CSS 与 DFG 模式。
9. **写两份文件**：
   - HTML：分块生成——先写骨架 + Ch0/Ch1，再逐块追加 Ch2、Ch3，避免单次 100+KB 载荷；写文件工具不要传空内容。
   - Agent 文档：按第五节骨架；JSON 数据与 HTML 同源；DFG 拓扑块由 Ch2 的 DFG 图转换而来并**剥离 shape**。
10. **校验**（见下节，必需，不跳过）。
11. **汇报** — 告知用户两个文件完整路径、头条数字（总参数/激活参数/KV per token/推荐配置）、与官方差异及解释、校验结论。不让用户开文件才能看到结论。

## 7. 强制校验清单（7 项不可跳过）

写完两份文件后、回复用户前执行。失败立即修复并复检，不只记录。此环节源于 M3 报告曾因一个单位混用失误连锁带出三个下游错误。

1. **Config 一致性** — 重读 config 文件，Ch1 每个数值逐字匹配。注意 `intermediate_size` vs `dense_intermediate_size` vs `shared_intermediate_size`、`num_key_value_heads` vs `num_attention_heads`、`head_dim` 算得还是显式。config 数组长度须等于 `num_hidden_layers`。
2. **独立重算** — 重新用 Python 从头重算核心数字（不复制 Step 5 的脚本，从 config 值重新输入公式），至少验证：总参数、激活参数、每 token KV、目标 context 总量、显存矩阵至少 2 行。
3. **单位一致性** — 每个带量纲数值换算自洽：`1M×X=Y GB` 行，X 为字节时 `Y=1,048,576×X/1e9`，X 为 KiB 时 `Y=1,048,576×X×1024/1e9`；GiB 必须标 "GiB"；压缩比 `X=1/Y×100`（0.1 内）；显存占比重除核对。
4. **多来源佐证** — 关键架构事实至少两来源（config / HF 代码 / 本地 Transformers 代码 / index.json / model card）：层数与 dense/MoE 划分、indexer 维度与头数、MTP 额外层、vision tower 维度。仅单来源支持的事实显式标注。
5. **章节间一致** — Ch3 显存用 Ch1 参数量；Ch0 官方数字与 Ch3 独立估算对账。
6. **HTML 结构** — `<h2>` 按序齐全；`<table>`/`<div class="card">` 闭合；无 "TODO"、`<!-- ops in this lane -->` 残留；单文件 < 200KB。
7. **Agent 文档** — 所有 ` ```json ` 块逐个 `json.loads`；DFG 拓扑块 grep `"shape"`/`"\[B"` 无命中（严禁 shape）；Markdown 算子表仅两列无 Shape；JSON 头条数字与 HTML 逐项一致。

通过后在回复中汇报："Verification 发现并修正了 N 处问题：…"（具体说明），跳过或简化了检查就不要声称"已验证"。主观措辞与选型建议是否合理属于人力判断，不在校验范围。

## 8. 硬性约束与汇报规范

- HTML 自包含（CSS 内联无外部依赖）；DFG 的 shape 必须是 config 具体数字；参数量精确并展示公式。
- 表格必须有 `min-width:600px` 和 `overflow-x:auto`。
- KVCache 估算必须包含每 token 成本和相对 MHA 基线的压缩比。
- 独立参数估算必须与 model card 官方数字对账，差异必须有解释。
- agent 文档与 HTML 必须**同时交付、数字同源**。
- 校验（第七节）为强制步骤，不可跳过。
- 回复用户时给出两个文件的完整路径与头条数字。

## 9. 失败回溯：常见异常与降级策略

分析过程中可能因网络、文件缺失、代码不兼容等原因中断。以下按阶段列出常见异常，Agent 遇到时应自动降级而非硬失败。

### 9.1 网络层：HuggingFace 不可达

| 症状 | 根因 | 处理方式 |
| --- | --- | --- |
| `curl` 退出码 28（Could not connect） | 沙箱无外网 / 代理未配置 | 终止下载，向用户说明当前环境无外网，提供下载命令让用户自行下载后放到指定路径 |
| `WebFetch` 返回空或 403 | HF CDN 重定向 / 限流 | 降级到走 `blob` 页面（`/blob/main/` 而非 `/resolve/main/`）获取内容；仍失败则请用户直接粘贴文件内容 |
| 下载超时（> 30s） | 网络慢 / 大文件 | 检查文件大小，超 10MB 的文件改用分块下载或跳过（仅 `model.safetensors.index.json` 例外，它通常 < 5MB） |

**原则**：不因网络问题阻塞整个分析。缺文件时向用户说明缺口与风险，由用户决定是否继续。

### 9.2 文件层：模型实现代码缺失

按 `references/code-fallback.md` 降级流程处理：先试本地 transformers 仓库，再回退 weight-map-only 逆向。降级后必须在报告中标注"基于 weight-map 推断，未交叉验证 modeling 代码"。

### 9.3 执行层：Python 计算脚本失败

| 症状 | 处理方式 |
| --- | --- |
| `torch` / `tensorflow` 未安装 | 用纯 Python 计算（仅算术运算，不依赖 DL 框架） |
| 除法精度 / 类型转换溢出 | 显式用 `Decimal` 或 `//` 整数除法，打印中间量；浮点结果保留 2 位小数 |
| 显存矩阵公式报错 | 降级为只输出 EP=8 的 2 行（W8A8/W4A8），不扫描全矩阵 |

### 9.4 校验层：数字对账不一致

- **差异 < 0.5%**：视为浮点舍入误差，记录但无需修正
- **差异 0.5%–5%**：检查公式中漏计项（如 norm 偏置、RMSNorm 权重、attention bias 参数），修正后重算
- **差异 > 5%**：检查 config 中 `intermediate_size` / `moe_intermediate_size` / `head_dim` 是否与代码实际值一致，必要时从 weight_map 通过 `safetensors_metadata` 反向验证参数量

### 9.5 报告生成层：HTML / Agent 文档写入失败

- 文件写入前检查目标目录是否存在，不存在则自动创建
- 单次写入内容超过 100KB 时，拆分为多块追加写入
- 写文件工具返回空内容错误时，重试 1 次；仍失败则改用 `Write` 工具逐一写入各章节并存为 `<model-name>-report-partial-*.html`，不丢失已生成内容
