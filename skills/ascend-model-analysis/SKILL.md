---
name: ascend-model-analysis
description: 为 LLM/多模态模型生成昇腾 NPU 0day 适配分析，输出 HTML 报告 + 供 Agent 消费的 Markdown+JSON 文档。
---

# Ascend 模型结构分析

你是一个模型结构分析专家。你的任务是根据模型配置文件（config 文件、model.safetensors.index.json等）和模型源码，自动分析模型结构特点，生成分析报告。

## 1. 交付物总览（两份文件必须同源）

每次分析必须产出两个文件，写到工作目录。`<model-name>` 取自 config 目录名或用户指定：

| 文件                                  | 受众         | 用途                                |
| ----------------------------------- | ---------- | --------------------------------- |
| `<model-name>-analysis-report.html` | 人          | 一份模型结构分析报告。                       |
| `<model-name>-analysis-agent.md`    | Agent / 脚本 | 支持对接 profiling 性能拆解、模型结构可视化等下游环节。 |

两份文件的**数据必须同源**——只允许呈现形式不同，不允许数据不一致。

## 2. 获取模型相关文件

两种来源。选定后核对文件齐全与否，缺口向用户说明风险。

- **本地目录**：核对文件是否齐全，主要包括 config 文件、modeling\_\*.py 以及 ModelCard 等。
- **HuggingFace**：先 WebSearch 定位 HF 仓库（官方 org 优先，多候选列出让用户确认），例如 DeepSeek-V4-Pro 的地址为 `https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro`，确定仓库后，再按照`references/download-from-hf.md`下载。

> 缺 `modeling_*.py` 时，按"本地 transformers → weight-map 逆向"顺序降级，详见 SKILL.md §10。

## 3. HTML 报告

中文、白底、简洁，CSS 全部内联（模板见 `references/css-template.md`），表格 `min-width:600px`、容器 `overflow-x:auto`。

报告默认 4 章（Ch0–Ch3），章节按输入组合：

| 输入条件         | 章节            |
| ------------ | ------------- |
| 无 model card | Ch1–Ch3       |
| 有 model card | Ch0 + Ch1–Ch3 |

### Ch0 模型定位与官方介绍（仅 model card）

对 model card 的**事实性摘录**，不是 config 复述——记录厂商官方口径，供 Ch2 算子、Ch3 参数量对账。

1. **官方核心指标** — 4 列 grid：官方总参数、官方激活参数、官方上下文长度、官方推理吞吐（仅在有标注时）。
2. **官方信息对照表** — 两列的表，（模型类型 / 语言主干 / 视觉编码器 / 激活参数 / 上下文长度 / 推理速度 / 推理级别 / 开源协议 / 官方量化版本 / 推测解码支持）。
3. **目标场景** — 整理厂商官方公布的适用场景，附上对应的核心能力基准跑分；每个场景描述 1‑3 句话。
4. **推理生态支持** — 输出表格，列出该模型所支持的推理框架（vLLM / SGLang / Transformers /llama.cpp/ NIM / Nemo），并标注各框架的关键配置项。

官方总参量与独立估算不一致必须对账（如"本报告独立估算~196.5B vs 官方公布 198B，差异源于 MTP 3 层（\~3.5B）的归属"。

### Ch1 架构概览

1. **核心参数表** — 表格列出全部关键配置参数（hidden_size、num_layers、num_heads、head_dim、kv_lora_rank、MoE相关参数、稀疏注意力参数、vision‑tower参数等）
2. **关键指标** — 使用4‑column grid展示：总参数量、激活参数量、层数、注意力类型
3. **层分布图** — 一行小型彩色方块，每层对应一个方块，按层类型上色。推荐配色：Red = Dense MLP，Orange = SparseMoE，Blue = Full Attention，Green = Linear Attention，Purple = MTP / SparseMoE+MSA，Teal = DSA/Vision。标注分段区间（例如 "Dense: 0‑2"，"MoE: 3‑77"）
4. **层类型汇总表** — 输出各层区间对应的注意力 + FFN组合

参数计算规则见下文。

### Ch2 单层正向算子分析

针对每一种不同的层类型，输出如下内容：

1. **数据流图(DFG)** — HTML可视化图，展示算子执行流程
2. **算子明细表** — 表格，列：#、算子名称、输入Shape、输出Shape、功能描述

**DFG绘图规则：**
- 读取 `references/css‑template.md` 获取完整CSS与HTML模板
- Q/K/V投影使用横向并行泳道并排展示
- 残差跳跃连接在右侧用Red dashed wires红色虚线连线表示
- 使用 `.dfg‑2col` 将注意力通路与FFN通路左右并列摆放
- 算子颜色编码：norm=Blue，mm=Green，act=Yellow，attn=Purple，shape=Gray，merge=Red，route=Orange，dsa/conv=Teal
- 张量shape统一使用 `[B, S, dim]` 或 `[B, heads, S, head_dim]`；符号定义：`B`=batch，`S`=seq_len，`T`=KV总长度

**算子明细表规则：**
- 每个层类型，表格放在DFG图的后方
- 使用 `<h4>` 子标题，之后 `<div class="card"><table>`，共5列
- 按阶段分组，使用带颜色的表头行（colspan=5）：注意力通路(Blue)、DSA/Indexer(Teal)、稀疏注意力(Purple)、FFN/MoE通路(Orange)、共享专家(Green)、门控后处理(Yellow)
- 如果该层算子与前面某层重复，插入斜体引用行：`<tr><td colspan="5" style="text‑align:center;color:var(--dim);font‑style:italic">注意力通路同A类 (算子1‑27)</td></tr>`
- 在单个层类型内部，算子顺序连续编号

多模态模型，额外输出 **V类（视觉塔）** 层 + projector拆解。

章末放「算子图例与数量汇总」（配色图例 + 每层类型算子数）。

### Ch3 KVCache 与参数量估算

1. **每 token KVCache 占用** — 各注意力类型的每 token 每层 KV 占用 + 相对 MHA baseline 的压缩比。
2. **参数量分项表** — 按组件拆（embedding / lm\_head / dense / MoE 非专家 / routed / shared / indexer / vision tower / projector），展示公式与数值。
3. **总参数量与激活参数量** — 汇总公式，与 model card 官方数对账并解释差异（MTP/草稿层不在 checkpoint、lm\_head/embedding 共享、投影融合、vision tower 归属）。
4. **量化×并行单卡权重显存** - MoE 必需；纯 dense 小模型可跳过。

具体计算方法见 `references/param-formulas.md`。

## 4. Markdown 报告：

文件名 `<model-name>-analysis-agent.md`，章节和内容与 HTML 报告一致（Ch0 / Ch1 / Ch2 / Ch3）。目标：让下游 Agent/脚本**不解析 HTML** 就能拿到全部结构化数据。

职责拆分就一条：**给人看的叙述与表格用 Markdown，其余能 JSON 表达的数据一律用独立 JSON 块**。

特别注意：
- 算子 DFG → 独立 JSON 块（即 DFG 拓扑）：`nodes`（id / op / stage）+ `edges`（from / to）+ 可选 `groups`；**只留拓扑、严禁 shape**，残差用「分支源 → Add」edge 表达，和html不同，无需标注shape。
- 算子明细表 → 无需shape,用表格即可表达，只留算子名称、算子功能描述。
- 层分布图与层类型汇总等小节不适合用json的，可以用文字描述+表格表达。

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
2. 按照[获取模型相关文件](#2-获取模型相关文件)进行。+
3. 读取config文件以及所有的模型代码。
4. 识别架构与层变体。
5. [kvcache-与参数量估算](#ch3-kvcache-与参数量估算)，通过Bash调用Python脚本实现。脚本写成单条`python -c "..."`形式，显式打印中间计算结果，作为后面校验环节的依
6. **交叉校验**：将独立计算出的参数量估算值与model card官方数值做比对，对差异进行解释说明（关注MTP层、embedding权重共享、投影融合、lm_head统计口径）。
7. **生成HTML 和 Markdown 报告**
8. **校验环节（强制执行）**：报告生成完成后，执行独立交叉验证。即便未发现异常也不可跳过；很多严重错误第一眼看上去是正常的，参考下文[校验清单](#8-校验清单)。
9. **输出报告摘要**，向用户简洁说明：
    - 报告输出路径
    - 关键指标（总参数量、激活参数量等）
    - 和官方数值存在的差异及原因
    - 校验环节结果：输出“已交叉验证，无错误”，或列出已修正的问题清单

## 8. 校验清单

写完两份文件后、回复用户前执行。失败立即修复并复检。

1. **Config 一致性** — 重读 config 文件，Ch1 每个数值逐字匹配。注意 `intermediate_size` vs `dense_intermediate_size` vs `shared_intermediate_size`、`num_key_value_heads` vs `num_attention_heads`、`head_dim` 算得还是显式。config 数组长度须等于 `num_hidden_layers`。
2. **独立重算** — 重新用 Python 从头重算核心数字（不复制 Step 5 的脚本，从 config 值重新输入公式），至少验证：总参数、激活参数、每 token KV、目标 context 总量、显存矩阵至少 2 行。
3. **单位一致性** — 每个带量纲数值换算自洽：`1M×X=Y GB` 行，X 为字节时 `Y=1,048,576×X/1e9`，X 为 KiB 时 `Y=1,048,576×X×1024/1e9`；GiB 必须标 "GiB"；压缩比 `X=1/Y×100`（0.1 内）；显存占比重除核对。
4. **多来源佐证** — 关键架构事实至少两来源（config / HF 代码 / 本地 Transformers 代码 / index.json / model card）：层数与 dense/MoE 划分、indexer 维度与头数、MTP 额外层、vision tower 维度。仅单来源支持的事实显式标注。
5. **章节间一致** — Ch3 显存用 Ch1 参数量；Ch0 官方数字与 Ch3 独立估算对账。
6. **HTML 结构** — `<h2>` 按序齐全；`<table>`/`<div class="card">` 闭合；无 "TODO"、`<!-- ops in this lane -->` 残留；单文件 < 200KB。
7. **Agent 文档** — 所有 ` ```json ` 块逐个 `json.loads`；DFG 拓扑块 grep `"shape"`/`"\[B"` 无命中（严禁 shape）；Markdown 算子表仅两列无 Shape；JSON 头条数字与 HTML 逐项一致。

通过后在回复中汇报："Verification 发现并修正了 N 处问题：…"（具体说明），跳过或简化了检查就不要声称"已验证"。主观措辞与选型建议是否合理属于人力判断，不在校验范围。

## 9. 硬性约束与汇报规范

- HTML 自包含（CSS 内联无外部依赖）；DFG 的 shape 必须是 config 具体数字；参数量精确并展示公式。
- 表格必须有 `min-width:600px` 和 `overflow-x:auto`。
- KVCache 估算必须包含每 token 成本和相对 MHA 基线的压缩比。
- 独立参数估算必须与 model card 官方数字对账，差异必须有解释。
- agent 文档与 HTML 必须**同时交付、数字同源**。
- 校验（第八节）为强制步骤，不可跳过。
- 回复用户时给出两个文件的完整路径与头条数字。

## 10. 失败回溯：常见异常与降级策略

分析过程中可能因网络、文件缺失、代码不兼容等原因中断。遇到下列异常时自动降级而非硬失败：

- `curl` 退出码 28（Could not connect）：沙箱无外网 / 代理未配置，终止下载，向用户说明当前环境无外网，提供下载命令让用户自行下载后放到指定路径。
- `WebFetch` 返回空或 403：HF CDN 重定向 / 限流，降级走 `blob` 页面（`/blob/main/` 而非 `/resolve/main/`）；仍失败则请用户直接粘贴文件内容。
- 下载超时（> 30s）：检查文件大小，超 10MB 改用分块下载或跳过（仅 `model.safetensors.index.json` 例外，通常 < 5MB）。
- 不因网络问题阻塞整个分析：任何文件缺失都向用户说明缺口与风险，由用户决定是否继续。
- HF 仓库缺 `modeling_*.py`（MiniMax、GLM/Z.ai、部分 sglang/vllm-first 发布不带 modeling 代码）：仅当本地 transformers 也不提供实现时才回退 weight-map-only 逆向，顺序为——
  1. 定位本地 transformers 仓库（默认 `<workspace>/transformers`，缺失检查 `../transformers`，仍无则 `git clone -b main --single-branch --depth 1 https://github.com/huggingface/transformers.git`）
  2. 用 config 的 `model_type` 定位 `transformers/src/transformers/models/<model_type>/`，试规范化变体（`-` → `_`），grep `configuration_*.py` / `modeling_*.py` / `modular_*.py`
  3. 将匹配文件复制进下载的模型文件夹，让报告输入目录自包含
  4. 读本地 transformers 文件作为算子流主来源，仍用 `model.safetensors.index.json` 交叉核对哪些层 / 额外模块（如 MTP 层）实际存在
  5. 本地 transformers 也无实现 → 回退 `model.safetensors.index.json` 的 `weight_map` 键逆向层结构（按层索引分组、从命名模式推断架构，如 `block_sparse_moe.experts.N.{w1,w2,w3}`、`self_attn.index_k_proj`）
  - 降级后在 Ch0 末尾（无 Ch0 则 Ch1 开头）标注：⚠️ 本报告基于 `model.safetensors.index.json` 权重名逆向推断算子流，未交叉验证 modeling 代码，请审阅时注意。
- `torch` / `tensorflow` 未安装：用纯 Python 计算（仅算术运算，不依赖 DL 框架）。
- 除法精度 / 类型转换溢出：显式用 `Decimal` 或 `//` 整数除法，打印中间量；浮点结果保留 2 位小数。
- 显存矩阵公式报错：降级为只输出 EP=8 的 2 行（W8A8/W4A8），不扫描全矩阵。
- 数字对账不一致：差异 < 0.5% 视为浮点舍入误差（记录无需修正）；差异 0.5%–5% 检查漏计项（norm 偏置、RMSNorm 权重、attention bias 参数）修正后重算；差异 > 5% 检查 `intermediate_size` / `moe_intermediate_size` / `head_dim` 是否与代码一致，必要时从 `weight_map` 通过 `safetensors_metadata` 反向验证参数量。
- HTML / Agent 文档写入失败：写入前检查目标目录是否存在（不存在则创建）；单次写入超 100KB 拆分为多块追加；写文件工具返回空内容错误时重试 1 次，仍失败改用 `Write` 逐一写入各章节存为 `<model-name>-report-partial-*.html`。

