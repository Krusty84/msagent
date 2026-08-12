## 3.3 安全隐私与DFX设计

### 安全隐私

- **数据不出本地**：所有算子采集和对比分析均在本地 NPU 环境和文件系统完成，不涉及网络传输
- **脚本备份隔离**：修改 RL 训练脚本前自动备份至带时间戳的 `.bak` 文件，避免误操作丢失原始配置
- **敏感路径处理**：报告中不暴露用户的绝对路径中可能包含的敏感信息（如用户名、内部项目名），使用相对路径或变量名替代

### 兼容性

| 维度 | 说明 |
|------|------|
| CANN 版本 | 依赖 `msprof` 和 CANN level1 profiling 能力，需 CANN 8.0.RC1+ |
| verl 版本 | 依赖 verl 的 `global_profiler` 和组件级 profiler 配置（`actor.profiler`、`rollout.profiler`） |
| vLLM Ascend | 推理路径依赖 `vllm-ascend` 插件提供的融合算子 |
| Megatron | 训练路径依赖 Megatron-Core 的标准 Transformer 层实现 |
| Python 依赖 | `torch`、`torch_npu`、`megatron-core`、`vllm`、`vllm-ascend`、`mbridge` |

### 可维护性

- **模板驱动报告**：报告格式由 `references/report_template.md` 定义，格式变更只需修改模板
- **差异规则可扩展**：算子差异等级判定规则（4.3 节）以规则表形式维护，新增算子类型只需追加规则行
- **中间产物可审计**：每个阶段保留中间产物（`megatron_runtime_ops.json`、`vllm_runtime_ops.json`），支持事后回溯验证

### 可测试性

- **阶段独立验证**：每个阶段有明确的输入输出，阶段 3.2 的 DB 文件验证独立于后续分析
- **降级策略可测**：vLLM device-side 数据缺失时自动降级到 `api_statistic_*.csv`，降级行为可通过模拟空数据验证

### 可靠性

- **环境前置检查**：阶段 1 执行 NPU 可用性、依赖包版本、模型文件存在性检查，失败即中断
- **DB 存在性验证**：阶段 3.2 强制验证两个 DB 文件均存在后才进入分析阶段
- **Profiler 冲突检测**：阶段 2.1 检查脚本是否已含 profiler 配置，避免重复注入

---

## 3.4 编程与调用设计

### 3.4.1 编程模型基本设计

**开发环境**：
- 硬件：Ascend NPU（910B/910C 等），至少 1 卡
- 软件：CANN 8.0.RC1+、msprof、Python 3.10+
- 框架：verl（含 Megatron-Core + vLLM + vllm-ascend）
- 分析工具：SQLite（读取 profiler DB）、msprof-mcp（MCP 工具集）

**开发约束**：
- **必须运行完整 RL 脚本**：不允许编写独立脚本分离运行 Megatron 和 vLLM
- **必须依赖运行时 Profiling**：不支持纯离线或静态源码扫描模式
- **单步采集**：`global_profiler.steps=[1]`，仅采集 step 1 的算子数据
- **唯一触发入口**：用户提供 RL 训练脚本路径

**可验收设计**：
- 验收环境：1-8 卡 Ascend NPU，安装 CANN 8.0.RC1+ 及 verl 全家桶
- 验收用例：使用 Qwen3-0.6B + 标准 RL 脚本（`references/run_qwen3_0_6b_megatron_vllm_ascend.sh`）
- 验收标准：生成包含注意力、归一化、激活、RoPE、线性投影 5 类模块对比的完整报告，所有 🔴🟠 差异算子含代码调用栈

### 3.4.2 接口定义与设计

#### 3.4.2.1 Skill 触发接口（自然语言）

- **接口描述**：用户在对话中提供 RL 训练脚本路径，配合触发词激活 skill
- **触发词**：`"训推算子扫描"`、`"训推差异性"`、`"算子差异报告"`、`"融合算子对比"`、`"train vs infer op diff"`
- **输入参数**：

| 参数名称 | 输入/输出 | 类型 | 描述 | 取值范围 |
| --- | --- | --- | --- | --- |
| script_path | 输入 | string (path) | RL 训练脚本的绝对路径 | 存在的可执行 `.sh` 文件 |

- **异常处理**：脚本不存在或不可读时，返回错误并中断；NPU 不可用或依赖缺失时，返回具体缺失项并中断
- **约束说明**：每次执行会修改脚本文件（注入 profiler 配置），自动备份原始脚本
- **调用参考代码**：

```
用户: "请对 /workspace/rl/run_qwen3_0_6b_megatron_vllm_ascend.sh 做训推算子扫描"
```

#### 3.4.2.2 报告生成接口（内部调用）

- **接口描述**：阶段 5 生成 Markdown 报告和 CSV 对比表
- **输入**：训练和推理的算子列表（JSON）+ 差异对比分析结果
- **输出**：

| 产物 | 路径 | 格式要求 |
|------|------|---------|
| 完整报告 | `<工作目录>/train_infer_op_diff_report.md` | Markdown，含 HTML 颜色标记和折叠块 |
| 算子对比表 | `<工作目录>/operator_diff_table.csv` | CSV，UTF-8 BOM 编码 |

- **异常处理**：DB 查询失败（表不存在、字段缺失）时，先查询 schema 确定可用表和字段，降级使用 api_statistic CSV，报告中标注

### 3.4.3 使用说明

1. **Profiler 配置参数说明**：

| 配置项 | 说明 | 可选值 |
|--------|------|--------|
| `global_profiler.tool` | 全局 profiler 工具类型 | `npu` |
| `global_profiler.steps` | 采集的 step 编号列表 | `[1]`（建议单步，节省时间） |
| `*.profiler.tool_config.npu.level` | CANN profiling 级别 | `level1`（算子级别） |
| `*.profiler.tool_config.npu.contents` | 采集内容 | `['npu','cpu','shapes']` |
| `rollout.profiler.tool_config.npu.discrete` | rollout 算子分离存储 | `True`（**必须**） |

2. **使用约束和限制**：
   - 单次采集仅覆盖 step 1，若需多 step 分析需修改 `global_profiler.steps`
   - vLLM Ascend 推理的 device-side 数据可能不完整，报告中会标注降级来源
   - 大模型（>7B）运行时间显著增加，30 分钟以上
   - 本 skill 仅做算子层面的差异对比，不做运行时 dump 数据的逐值精度对比

---

# 4.测试设计

### 单元测试

| 测试模块 | 测试用例 | 输入 | 预期输出 |
|---------|---------|------|---------|
| 配置解析器 | 标准 verl 脚本解析 | Qwen3-0.6B RL 脚本 | 正确提取所有关键配置字段 |
| 配置解析器 | 缺字段脚本解析 | 缺少 `rollout.name` 的脚本 | 字段标注为 "未配置" 并警告 |
| 环境检查 | NPU 可用检查 | NPU 正常环境 | 通过，返回 NPU 数量 |
| 环境检查 | NPU 不可用检查 | 无 NPU 环境 | 中断，返回 "NPU 不可用" |
| 环境检查 | 依赖缺失检查 | 缺少 vllm-ascend | 中断，返回缺失包名 |
| 脚本修改 | Profiler 注入 | 未含 profiler 的脚本 | 脚本新增 PROFILER 配置块 |
| 脚本修改 | 重复注入防护 | 已含 `global_profiler` 的脚本 | 跳过注入，日志提示"已存在" |
| DB 提取 | 训练算子 SQL | 标准训练 DB | 返回算子名+调用次数列表 |
| DB 提取 | 推理算子 SQL | 标准推理 DB | 返回算子名+调用次数列表 |
| 差异判定 | 融合 vs 非融合 | FlashAttentionScore vs FusedInferAttentionScore | 🔴 高差异 |
| 差异判定 | 相同算子 | MatMulV2 vs MatMulV2 | 🟢 无差异 |
| CSV 生成 | UTF-8 BOM 验证 | 任意对比结果 | 文件头为 `\xef\xbb\xbf` |

### 集成测试

| 测试场景 | 测试方法 | 验证点 |
|---------|---------|--------|
| 端到端 Qwen3-0.6B | 完整运行 5 阶段 | 报告产出、CSV 产出、备份文件存在 |
| vLLM DB 缺失降级 | 模拟 rollout DB 不存在 | 降级到 api_statistic CSV，报告中标注 "数据来源：api_statistic（device-side DB 不可用）" |
| 多 NPU 环境 | 8 卡运行 | 所有 rank DB 均生成，只分析 rank 0 |
| 大模型 (>7B) | Qwen-7B 运行 | 运行时间预估准确，不超时 |

### 端到端测试

| 测试场景 | 步骤 | 验收标准 |
|---------|------|---------|
| 标准场景 | 1. 提供 Qwen3-0.6B RL 脚本<br>2. 等待完整执行<br>3. 检查产物 | 报告包含注意力/归一化/激活/RoPE/线性投影 5 类模块对比 |
| 差异等级准确性 | 人工审核报告 | 🔴🟠🟡🟢 标记 100% 符合 4.3 节规则 |
| 调用栈完整性 | 检查每个差异算子 | 每个 🔴🟠 差异算子有完整调用栈（文件→函数→CANN 算子） |
| CSV 可用性 | Excel 打开 CSV | 中文正常显示，列对齐正确 |

---

# 5.缺点和风险（可选）

| 风险 | 等级 | 说明 | 应对措施 |
|------|:----:|------|---------|
| vLLM Ascend 数据不完整 | 中 | vLLM rollout 在 Ascend 上的 device-side profiling 数据可能为空 | 自动降级到 `api_statistic_*.csv`，报告中明确标注数据来源 |
| 单步采集局限性 | 低 | 仅采集 step 1 的算子，若后续 step 因动态 shape 触发不同算子则漏检 | 文档中明确说明，用户可自行修改 `global_profiler.steps` |
| 大模型耗时 | 中 | >7B 模型运行时间可能超过 30 分钟 | 阶段 3 开始前告知用户预计等待时间 |
| 脚本修改风险 | 低 | Profiler 注入可能影响原有脚本逻辑（极低概率） | 自动备份原始脚本，用户可随时恢复 |
| 算子名随版本变化 | 低 | CANN/vllm-ascend 版本升级后算子名可能变化，导致差异判定规则不适配 | 差异规则表以 CANN 算子名为 key，升级后可维护规则表 |
| MSTX 不可用 | 低 | Profiler DB 中 MSTX 表可能不存在，无法获取精确调用栈 | 降级到预定义源码路径映射表 |
| 反向传播算子差异 | 低 | 当前仅对比前向传播算子（Megatron actor/ref 前向 vs vLLM rollout 前向），反向传播算子仅在训练侧存在，不作跨路径对比 | 报告中训练独有算子（含反向传播）标注为 🟡，单独列出 |

---

# 6.现有技术（可选）

### 同类方案对比

| 方案 | 方法 | 优点 | 缺点 |
|------|------|------|------|
| 静态源码扫描 | 解析 Megatron 和 vLLM 源码中的算子调用 | 快速、不依赖 NPU | 无法获取运行时融合策略，融合算子 vs 单算子的最终执行形态不可知 |
| 独立分离运行 | 分别写脚本跑 Megatron 和 vLLM | 灵活、可独立调试 | 权重不一致、batch size 等运行时参数不统一、无法反映 RL 训练真实场景 |
| **本 skill（运行时统一采集）** | 通过 verl profiler 在完整 RL 脚本中同时采集两条路径 | 反映真实运行时算子、权重共享、参数一致 | 依赖 verl profiler 集成、耗时较长 |

### 借鉴

- **PyTorch Profiler**：借鉴其 trace 级别的算子记录机制，通过 CANN level1 获取 NPU 侧真实算子
- **verl profiler 框架**：复用其 `global_profiler` + 组件级 profiler 的分层采集架构
- **msprof-analyze**：算子级数据提取和分析思路借鉴自 Ascend msprof 工具链

---

# 7.未解决问题（可选）

| 待解决问题 | 优先级 | 说明 |
|-----------|:------:|------|
| 多 step 采集支持 | P1 | 当前仅支持单 step，若需多 step 对比需修改配置，是否应作为参数暴露？ |
| 反向传播算子跨路径对比 | P2 | 训练路径的反向传播算子（如 `FlashAttentionScoreGrad`）是否能与推理路径找到等价物？当前方案仅做前向对比 |
| vLLM device-side 数据完整性 | P1 | 依赖 vllm-ascend 团队的 device-side profiling 能力完善 |
| 自定义融合算子识别 | P2 | 用户自定义的融合算子（非 vllm-ascend 标准融合）如何纳入差异判定规则？ |
| 多 GPU 集群场景 | P2 | 多卡 TP/PP 场景下，不同 rank 的算子可能不同，当前方案仅分析 rank 0，是否需要跨 rank 聚合？ |

---

附录

- **参考资料链接**
  - verl profiler 文档
  - Megatron-Core Transformer 层源码：`megatron/core/transformer/`
  - vLLM Ascend 插件源码：`vllm_ascend/`
- **术语表**

| 术语 | 说明 |
|------|------|
| RL | Reinforcement Learning，强化学习 |
| verl | Volcano Engine Reinforcement Learning，字节跳动开源 RL 训练框架 |
| Megatron | NVIDIA 的大规模 Transformer 训练框架（Megatron-Core） |
| vLLM | 高性能 LLM 推理引擎 |
| CANN | Ascend Compute Architecture for Neural Networks |
| e2e profiler | 端到端 profiler，采集完整训练流程 |
| discrete profiler | 分离式 profiler，将不同组件数据分开存储 |
| MSTX | Profiler DB 中的调用栈标记表 |

- **文档更新计划**
  - vLLM Ascend device-side profiling 能力完善后，更新降级策略说明
  - CANN 版本升级导致算子名变化时，更新差异规则表
