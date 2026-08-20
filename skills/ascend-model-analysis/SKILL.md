---
name: ascend-model-analysis
description: 分析模型结构，输出 HTML 报告 + 供 Agent 消费的 Markdown+JSON 文档。
---

# Ascend 模型结构分析

你是一个模型结构分析专家。你的任务是根据模型配置文件（config 文件、model.safetensors.index.json等）和模型源码，自动分析模型结构特点，生成分析报告。

## 1. 交付物总览（两份文件必须同源）

每次分析必须产出两个文件，写到工作目录。`<model-name>` 取自 config 目录名或用户指定：

| 文件                                  | 受众         | 用途                                |
| ----------------------------------- | ---------- | --------------------------------- |
| `<model-name>-analysis-report.html` | 人          | 一份模型结构分析报告。                       |
| `<model-name>-analysis-report.md`   | Agent / 脚本 | 支持对接 profiling 性能拆解、模型结构可视化等下游环节。 |

两份文件的**数据必须同源**——只允许呈现形式不同，不允许数据不一致。

## 2. 获取模型相关文件

两种来源。选定后核对文件齐全与否，缺口向用户说明风险。

- **本地目录**：核对文件是否齐全，主要包括 config 文件、modeling\_\*.py 以及 ModelCard 等。
- **HuggingFace**：先 WebSearch 定位 HF 仓库（官方 org 优先，多候选列出让用户确认），例如 `<org>/<model-name>` 的地址为 `https://huggingface.co/<org>/<model-name>`，确定仓库后，再按照`references/download-from-hf.md`下载。

> 缺 `modeling_*.py` 时，按"本地 transformers → weight-map 逆向"顺序降级，详见 SKILL.md §10。

## 3. HTML 报告

中文、白底、简洁，CSS 全部内联（模板见 `references/css-template.md`），表格 `min-width:600px`、容器 `overflow-x:auto`。

报告默认 4 章（第0–3章），章节按输入组合：

| 输入条件         | 章节    |
| ------------ | ----- |
| 无 model card | 第1–3章 |
| 有 model card | 第0–3章 |

### 第0章 模型定位与官方介绍（仅 model card）

对 model card 的**事实性摘录**，不是 config 复述——记录厂商官方口径，供第2章 算子、第3章 参数量对账。

1. **官方核心指标** — 4 列 grid：官方总参数、官方激活参数、官方上下文长度、官方推理吞吐（仅在有标注时）。
2. **官方信息对照表** — 两列的表，（模型类型 / 语言主干 / 视觉编码器 / 激活参数 / 上下文长度 / 推理速度 / 推理级别 / 开源协议 / 官方量化版本 / 推测解码支持）。
3. **目标场景** — 整理厂商官方公布的适用场景，附上对应的核心能力基准跑分；每个场景描述 1‑3 句话。
4. **推理生态支持** — 输出表格，列出该模型所支持的推理框架（vLLM / SGLang / Transformers /llama.cpp/ NIM / Nemo），并标注各框架的关键配置项。

官方总参量与独立估算不一致必须对账（如"本报告独立估算\~196.5B vs 官方公布 198B，差异源于 MTP 3 层（\~3.5B）的归属"。

### 第1章 架构概览

1. **核心参数表** — 表格列出全部关键配置参数（hidden\_size、num\_layers、num\_heads、head\_dim、kv\_lora\_rank、MoE相关参数、稀疏注意力参数、vision‑tower参数等）
2. **关键指标** — 使用4‑column grid展示：总参数量、激活参数量、层数、注意力类型
3. **层分布图** — 一行小型彩色方块，每层对应一个方块，按层类型上色。推荐配色：Red = Dense MLP，Orange = SparseMoE，Blue = Full Attention，Green = Linear Attention，Purple = MTP / SparseMoE+MSA，Teal = DSA/Vision。标注分段区间（例如 "Dense: 0‑2"，"MoE: 3‑77"）
4. **层类型汇总表** — 输出各层区间对应的注意力 + FFN组合

参数计算规则见下文。

### 第2章 单层正向算子分析

针对每一种不同的层类型，输出如下内容：

1. **数据流图(DFG)** — HTML可视化图，展示算子执行流程
2. **算子明细表** — 表格，列：#、算子名称、输入Shape、输出Shape、功能描述

**DFG绘图规则：**

- 读取 `references/css‑template.md` 获取完整CSS与HTML模板
- Q/K/V投影使用横向并行泳道并排展示
- 残差跳跃连接在右侧用Red dashed wires红色虚线连线表示
- 使用 `.dfg‑2col` 将注意力通路与FFN通路左右并列摆放
- 算子颜色编码：norm=Blue，mm=Green，act=Yellow，attn=Purple，shape=Gray，merge=Red，route=Orange，dsa/conv=Teal
- 张量shape统一使用 `[B, S, dim]` 或 `[B, heads, S, head_dim]`；符号定义：`B`=batch，`S`=seq\_len，`T`=KV总长度

**算子明细表规则：**

- 每个层类型，表格放在DFG图的后方
- 使用 `<h4>` 子标题，之后 `<div class="card"><table>`，共5列
- 按阶段分组，使用带颜色的表头行（colspan=5）：注意力通路(Blue)、DSA/Indexer(Teal)、稀疏注意力(Purple)、FFN/MoE通路(Orange)、共享专家(Green)、门控后处理(Yellow)
- 如果该层算子与前面某层重复，插入斜体引用行：`<tr><td colspan="5" style="text‑align:center;color:var(--dim);font‑style:italic">注意力通路同A类 (算子1‑27)</td></tr>`
- 在单个层类型内部，算子顺序连续编号

多模态模型，额外输出 **V类（视觉塔）** 层 + projector拆解。

章末放「算子图例与数量汇总」（配色图例 + 每层类型算子数）。

### 第3章 KVCache 与参数量估算

1. **每 token KVCache 占用** — 各注意力类型的每 token 每层 KV 占用 + 相对 MHA baseline 的压缩比。
2. **参数量分项表** — 按组件拆（embedding / lm\_head / dense / MoE 非专家 / routed / shared / indexer / vision tower / projector），展示公式与数值。
3. **总参数量与激活参数量** — 汇总公式，与官方公布数对账并解释差异（官方数来源：model card 或 HF 模型页 README，均无则跳过对账并显式声明"无官方参照"；差异常见原因：MTP/草稿层不在 checkpoint、lm\_head/embedding 共享、投影融合、vision tower 归属）。
4. **量化×并行单卡权重显存** - MoE 必需；纯 dense 小模型可跳过。

具体计算方法见 `references/param-formulas.md`。

## 4. Markdown 报告

文件名 `<model-name>-analysis-report.md`，章节和内容与 HTML 报告一致（第0章 / 第1章 / 第2章 / 第3章）。目标：让下游 Agent/脚本**不解析 HTML** 就能拿到全部结构化数据。

职责拆分就一条：**给人看的叙述与表格用 Markdown，其余能 JSON 表达的数据一律用独立 JSON 块**。逐章必须产出下列 JSON 块，不能只写 Markdown 表格：

| 章节               | 必须提供的 JSON 块                                                            |
| ---------------- | ----------------------------------------------------------------------- |
| 第0章 模型定位         | 官方核心指标；官方信息对照（key-value）                                                |
| 第1章 架构概览         | 核心参数（key-value，数组型参数给完整数组）；关键指标；层类型映射（`compress_ratios` 完整数组或 index→类型） |
| 第2章 单层算子         | DFG 拓扑（nodes/edges，每种层类型都需独立可解析结构）                                      |
| 第3章 KVCache 与参数量 | 参数量分项（组件→参数量）                                                           |

> 各 JSON 块数值须与该章 Markdown 表格、与 HTML 报告同章数值同源一致（见 §8 第 6 条）。

特别注意：

- **key-value 数据（核心参数表、官方核心指标等）→ 直接用 JSON 块表达，不要再写 Markdown 表格**：键为参数名、值为配置精确值；数组型参数（如 `compress_ratios`、`moe_layer_freq`）必须给完整数组，禁止用「长度 N」代替。下游脚本直接 `json.loads` 即可消费，无需解析 Markdown 表格。
- **算子DFG输出规范**：
  算子DFG输出为独立`json`代码块，该JSON为机器、Agent、下游脚本消费的拓扑数据，与HTML可视化报告完全分离。
  1. 顶层结构：允许 dfg\_meta, nodes, edges，可选 groups。严禁嵌入mermaid字符串、HTML片段。禁止shape、x、y、color、layout等所有渲染布局字段。
  2. nodes：算子节点数组
     每个节点必填字段：id(全局唯一字符串), op(算子/层名称), stage(执行阶段)；允许附加desc语义说明；
     层继承、复用关系放在该node对象内部，使用结构化字段，禁止使用自然语言字符串描述复用关系。
     示例：
     {
     "id": "n\_attn\_0",
     "op": "attention",
     "stage": "fwd",
     "desc": "CSA注意力层",
     "inherits": "A\_CSA\_layer",
     "omit": \["indexer", "weights\_proj"]
     }
     禁止写法："op":"attention，同A\_CSA\_layer"
  3. edges：有向数据流边数组
     字段：from（源node id），to（目标node id）；
     存在动态分支、条件算子时，增加branch\_condition字段；普通数据流branch\_condition填null。
     残差连接表达：直接增加一条edge，残差源节点 from → Add算子节点 to，不引入残差专属特殊字段。
  4. groups【可选】：算子分组子图，用于逻辑分组；groups内部仅引用node id，不重复定义节点。
  5. 语义边界：
     拓扑连接全部走edges；类型继承、字段覆写属于节点元数据，不使用edges表达继承关系。
  6. 解析约定：
     下游脚本与Agent仅解析JSON结构；可视化视图由外部转换器基于该JSON生成，不在JSON内部携带视图代码。
- 算子明细表 → 无需shape,用表格即可表达，只留算子名称、算子功能描述。
- 层分布图与层类型汇总等小节不适合用json的，可以用文字描述+表格表达（但其中关键的层类型映射数组，如 `compress_ratios`，仍须以 JSON 给出）。

## 5. 架构识别

架构识别表见 `references/arch-recognition.md`，逐条对照命中即记录对应特征。

## 6. Shape 约定

- `B` = 批大小（batch\_size）
- `S` = 当前 query 的序列长度
- `T` = KV 缓存总长度（S + past）
- `T_i` = 分派给专家 i 的 token 数
- attention 张量统一为 `[B, heads, S, head_dim]`
- hidden state 统一为 `[B, S, hidden_size]`

## 7. 工作流

1. 让用户选择输入：— 本地目录或 HuggingFace 仓库。
2. 按照[获取模型相关文件](#2-获取模型相关文件)进行。
3. 读取 `references/` 下必读文件（视作权威实现细节，禁止凭通用知识推断）：`css-template.md`、`arch-recognition.md`、`param-formulas.md`；需 HF 下载时再读 `download-from-hf.md`。随后读取 config 文件以及所有的模型代码。
4. 识别架构与层变体。
5. [KVCache 与参数量估算](#第3章-kvcache-与参数量估算)，通过终端命令行调用 Python 实现。计算复杂度高时优先写成独立脚本文件（临时文件，用完删除），简单计算也可用单条 `python -c "..."`；均须显式打印中间计算结果，作为后面校验环节的依据。
6. **交叉校验**：将独立计算出的参数量估算值与官方公布数值做比对（官方数来源：model card 或 HF 模型页，均无则跳过），对差异进行解释说明（关注MTP层、embedding权重共享、投影融合、lm\_head统计口径）。
7. **生成HTML 和 Markdown 报告**
8. **校验环节（强制执行）**：报告生成完成后，执行独立交叉验证。即便未发现异常也不可跳过；很多严重错误第一眼看上去是正常的，参考下文[校验清单](#8-校验清单)。
9. **输出报告摘要**，向用户简洁说明：
   - 报告输出路径
   - 关键指标（总参数量、激活参数量等）
   - 和官方数值存在的差异及原因
   - 校验环节结果：输出“已交叉验证，无错误”，或列出已修正的问题清单

## 8. 校验清单

写完两份文件后、回复用户前执行。失败立即修复并复检。

1. **配置与报告数据一致性校验** — 重读config.json，确认报告第 1 章核心参数表中引用的每一项数值均与原文完全一致。常见易错点：intermediate\_size、dense\_intermediate\_size、shared\_intermediate\_size（部分模型三者同时存在）；GQA 架构的num\_key\_value\_heads与num\_attention\_heads；由hidden\_size / num\_attention\_heads计算得到的head\_dim，对比配置中显式指定的head\_dim。若配置包含数组参数（moe\_layer\_freq、sparse\_attention\_freq），校验数组长度与num\_hidden\_layers相等。
2. **单位一致性** — 每个带量纲数值换算自洽：`1M×X=Y GB` 行，X 为字节时 `Y=1,048,576×X/1e9`，X 为 KiB 时 `Y=1,048,576×X×1024/1e9`；GiB 必须标 "GiB"；压缩比 `X=1/Y×100`（0.1 内）；显存占比重除核对。
3. **多来源交叉核验** — 报告中每一条架构事实，至少从 {config.json、HF模型代码、**本地Transformers模型代码**、model.safetensors.index.json、模型卡片} 当中选取两项进行核验：
   - “60层，3稠密层 + 57 MoE层” — 需要config.json（`num_hidden_layers=60`，`moe_layer_freq[:3]=0`）与权重映射（0‑2层无`block_sparse_moe`，3‑59层存在该模块）同时成立
   - “索引器维度=128，4头” — 需要config.json（`sparse_index_dim=128`，`sparse_num_index_heads=4`）、模型代码（`Indexer`投影维度）、权重映射（由文件数量推得`index_q_proj`维度）三者全部吻合
   - “共享索引器 / IndexShare” — 需要通过模型代码确认共享层是复用top‑k索引、索引器K缓存，还是两者均复用。不可仅依靠配置字段名称做推断。
   - MTP / 额外层 — 权重映射可能包含HF/Transformers常规前向传播会忽略的权重；检查模型代码的加载‑忽略规则并标注来源。
   - 视觉塔维度 — 需要config.json与权重映射的`vision_tower.*`键同时吻合
   - **若仅有单一来源支撑该事实，必须显式标注**（“配置中声明，权重清单暂未包含”或“权重清单中存在，本地 Transformers 常规 forward 未加载”）。
4. **章节间一致** — 第3章中单卡权重 GB 的计算，必须采用第1章给出的参数量；第0章官方公布数值应与第3章中独立估算结果保持一致。
5. **HTML 结构** — `<h2>` 按序齐全；`<table>`/`<div class="card">` 闭合；无 "TODO"、`<!-- ops in this lane -->` 残留；单文件 < 200KB。
6. **MarkDown 文档** — 全部JSON代码块逐个执行`json.loads`校验，无解析异常；DFG 拓扑 JSON 重新解析：节点 id 全局唯一、每条边的 from/to 均指向已存在节点、无悬空引用、节点集合与该层算子明细表一致；Markdown算子表严格为两列，不包含Shape字段；JSON头部数值与HTML报告逐项比对一致。
7. **两文件一致性** — HTML 与 Markdown 报告的章节数量与名称逐章对应（明确是否含第0章），禁止一方多出或缺少章节；同章关键数值两文件逐一一致。

通过后在回复中汇报："校验通过，发现并修正了 N 处问题：…"（具体说明），跳过或简化了检查就不要声称"已验证"。

## 9. 硬性约束与汇报规范

**重要注意事项**

- 报告必须自包含：全部 CSS 写在 `<style>` 内，不引入外部依赖
- 表格需设置 `min‑width:600px`，卡片容器配置 `overflow‑x:auto`
- HTML里面的DFG 图中展示的所有张量维度，必须取自配置文件的实际数值，不能使用符号变量
- 参数量必须为精确计算值，不可使用近似值，同时附带计算公式
- KVCache 估算必须同时给出单 Token 开销，以及相对于 MHA 基准的压缩比
- 当同时存在独立估算参数量与厂商公开数值时，务必做比对；存在差异需要说明原因。
- 返回报告前**必须执行完整校验流程**，第8节校验清单不可跳过
- 在给用户的回复中告知报告完整存储路径以及核心指标，不要让用户打开文件才能够看到结论。
- MarkDown 文档与 HTML 必须**同时交付、数据同源、章节名称和数量一致**。

## 10. 失败回溯：常见异常与降级策略

> 本章是给分析者（Agent）的操作手册，**不是报告内容**：严禁把下列降级策略写进 HTML / Markdown 报告正文。

分析过程中可能因网络、文件缺失、代码不兼容等原因中断。遇到下列异常时自动降级而非硬失败：

- `curl` 异常退出或者超时：沙箱无外网 / 代理未配置，终止下载，向用户说明当前环境无外网，提供下载命令让用户自行下载后放到指定路径。
- 不因网络问题阻塞整个分析：任何文件缺失都向用户说明缺口与风险，由用户决定是否继续。
- **HF仓库缺失模型实现文件** — 部分厂商（例如 MiniMax、GLM/Z.ai 发布版本、部分优先适配 sglang/vLLM 的发布包）不会在 HF 模型仓库中提供 `modeling_*.py`。遇到该情况，**优先尝试本地** **`transformers/`** **代码库**，实在不行再仅依靠权重映射做逆向解析：
  1. 定位项目本地的 transformers 代码库。默认路径为 `<workspace>/transformers`（示例：`d:/projects/model_analysis/transformers`）。如果不存在，检查同级目录如 `../transformers`。
  2. 在不会覆盖用户修改的前提下更新代码：执行 `git status --short`；工作区干净则执行 `git pull --ff-only`；若工作区存在修改，**禁止拉取更新**，直接使用当前代码版本并告知用户工作区存在改动。
  3. 根据 `config.json` 中的 `model_type`，在 `transformers/src/transformers/models/<model_type>/` 下查找对应实现。同时尝试名称归一化变体（`-` 替换为 `_`），必要时使用文本检索：查找 `configuration_<model_type>.py`、`modeling_<model_type>.py`、`modular_<model_type>.py`。
  4. 将匹配到的文件复制到已下载的模型目录（`configuration_*.py`、`modeling_*.py`、`modular_*.py`，相关的processor文件一并复制），保证报告输入目录具备完整自包含文件。
  5. 读取这份本地 Transformers 源码，将其作为算子执行流程的首要依据。依旧使用 `model.safetensors.index.json` 交叉核对实际存在哪些层、额外模块（例如MTP层权重）。
  6. 如果本地也不存在对应的Transformers实现，降级方案：依靠 `model.safetensors.index.json` 的 `weight_map` 键逆向推导层结构（按层索引分组，区分不同子模块集合，通过键名模式推断架构，例如 `block_sparse_moe.experts.N.{w1,w2,w3}`、`self_attn.index_k_proj`）。
- 除法精度 / 类型转换溢出：显式用 `Decimal` 或 `//` 整数除法，打印中间量；浮点结果保留 2 位小数。
- 数据对账不一致：差异 < 0.5% 视为浮点舍入误差。

