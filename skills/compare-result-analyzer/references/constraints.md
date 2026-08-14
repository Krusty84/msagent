# 约束规则参考

> **用途**: 本文件汇总 compare-result-analyzer skill 的所有约束规则，按分析阶段分为三节。分析步骤 4（首差异链定位）前，agent 必须先 Read 本文件。
>
> **版本规则**: 每条约束有唯一标识 `C-<阶段>-<序号>`，新增规则追加在对应章节末尾，已有标识不随新增而改变。

---

## 输入解析约束

### C-INPUT-000: row_index 1-based 口径
所有约束规则和报告中引用的 `row_index` SHALL 为 1-based（含 CSV 表头行）。CSV 第 1 行为表头，数据行从 row_index=2 开始。此口径 SHALL 在报告模板和脚本输出中保持一致。

#### Scenario: row_index 口径一致
- **WHEN** 任何约束规则或报告引用行号
- **THEN** 行号 SHALL 为 1-based（含表头行）
- **AND** 文档中 SHALL 注明此口径

### C-INPUT-001: 数值列规范化
- 数值列比较前先做规范化：去掉百分号后缀、把空字符串和 `N/A` 视为缺失、把 `nan` / `unsupported` 视为不可直接比较。`inf` / `-inf` 是合法无穷大值，可正常参与比较。
- `MaxRelativeErr`、`MinRelativeErr`、`MeanRelativeErr`、`NormRelativeErr` 在不同结果文件里可能带 `%`，阈值判断前必须统一转成数值。

### C-INPUT-002: 元信息异常检测
`dtype`、`shape`、`requires_grad` 不一致或出现 `NAN` 的节点，属于元信息异常，与数值误差分析并行判断。由脚本 `analyze_stat.py` 自动检测，结果写入报告 §7。

**shape_mismatch_ratio 计算口径**：`shape_mismatch_ratio = shape 不一致节点数 / 有效可比对节点数`（非全部行数）。有效可比对节点 = NPU Dtype 和 Bench Dtype 均非 N/A 的行。此口径 SHALL 在文档中注明。

### C-INPUT-003: 阈值确定（全自动自适应）
分析阈值**仅通过自适应阈值级联检测确定**（序列变点 → 锚定回溯 → Delta-NRE 离群 → 分布间隙 → 统计兜底），无需用户手动指定。脚本默认启用自适应检测，自动从数据分布中确定最优 NRE 阈值。不保留手动阈值选项。

### C-INPUT-005: input 侧 NRE/MeanBias 缺失的处理规则
分析过程中遇到 input 侧 NRE 或 MeanBias 缺失（值为 N/A 或 None）时，按以下规则处理：

1. **全缺失不参与传播分类**：当算子的所有 input 均缺失时，该算子不参与 ROOT CAUSE / PROPAGATION / PASS_THROUGH / ABSORBED 分类判定，仅在报告中列出时标注"input 全缺失，跳过传播分析"
2. **input 缺失的常见原因**：常量/非张量输入（如 scale、offset 等标量参数）、广播标量输入、in-place 操作覆盖输入快照、模型输入层（input_ids 等）不参与统计、框架算子注册的虚拟输入

#### 与 C-ANALYSIS-003 的配合
当某算子的 input 缺失导致无法判定 input→output 传播时，该算子不进入 C-ANALYSIS-003 的五种分类。但若该算子的 output 触发不可忽略判定，仍可作为首问题点的候选。

---

## 分析逻辑约束

### C-ANALYSIS-001: 不可忽略判定标准（NRE 为主、MeanBias 补充）
分析中所有"不可忽略"的判断统一使用此标准：
- **NRE 为主**：NormRelativeErr >= 阈值 → 直接判定为不可忽略
- **MeanBias 补充**：NRE < 阈值时，若 MeanBias >= α×阈值 → 也判定为不可忽略（α = 1.2）
- MeanBias = |Mean diff| / Bench L2norm
- **Mean diff 定义**：`Mean diff = mean(NPU tensor) − mean(Bench tensor)`，即两个 tensor 分别求均值后的差值。CSV 中所有数值列均为 tensor 级聚合统计量，非逐元素比较结果
- **Read `references/thresholds.md` 中「MeanBias 定义与判定」章节**了解 α 系数说明

⚠️ **小范数张量 NRE 放大解读提示**：当候选节点 l2 < 1e-3 且 NRE > 10% 时，NRE 可能因分母效应（bench_l2norm 小）而放大——实际绝对差异（Max diff / Mean diff）可能很小。报告 SHALL 在相关章节展示此提示，建议结合绝对量级判断严重程度。

### C-ANALYSIS-002: 首问题点判定（必须经过上下游传播检查后方可确认）
不能仅凭 output 指标触发 C-ANALYSIS-001 就认定为首问题点。**必须经过上下游传播检查后方可确认**，严格按以下流程执行：

1. 按执行顺序找到首个触发不可忽略判定的 OUTPUT 节点
2. **输入继承检查**：将该 output 与同一算子的所有 input 比对，若按 C-ANALYSIS-004 判定误差继承自 input（NRE+dtype+MeanRE+MaxRE 四维一致），标记为 INPUT_PROPAGATION，跳过
3. **下游吸收检查**：按 C-ANALYSIS-007 检查下游是否存在吸收节点（DOWNSTREAM_ABSORBED），若被吸收则跳过
4. 继续向后查找，直到找到第一个非继承、非吸收的节点，确认为首问题点

如果最终发现的首问题点不是最初触发的那个节点，报告中 MUST 记录跳过过程和原因。

### C-ANALYSIS-003: input→output 误差传播判断
判断一个模块的误差变化模式，必须比较其 input 和 output 的误差大小。基准线为 C-ANALYSIS-001 的不可忽略判定标准：

- **input 未触发 → output 触发**：算子**引入**了新误差 → **ROOT CAUSE**，最高优先级
- **input 触发 → output 触发且更差（output > input）**：算子**放大**了已有误差 → **PROPAGATION**
- **input 触发 → output 触发但更好（output < input 且超出相对容差 0.1%）**：误差**缩小** → **PASS_THROUGH**，排查优先级较低
- **input 触发 → output 未触发**：误差被**消除** → **ABSORBED**

**关键约束**：优先关注 input 干净但 output 不可忽略的算子（ROOT CAUSE），而非 input 已有误差但 output 缩小或消除的算子（PASS_THROUGH / ABSORBED）。

**复合优先级规则（C-ANALYSIS-014）**：多 input 算子遍历所有 (input, output) 组合后，按以下优先级分配单一标签：
- 若存在至少一条"干净 input（NRE < threshold）→ 脏 output（NRE ≥ threshold）"路径 → ROOT_CAUSE
- 仅当所有 input 都脏且 output 误差缩小 → PASS_THROUGH
- 同一 (prefix, direction) 不再同时标记为 ROOT_CAUSE 和 PASS_THROUGH

### C-ANALYSIS-004: 多 input 算子检查
对于有多个 input 的算子（如 Tensor.__mul__、Tensor.__add__ 等），**必须检查所有 input 的误差**。如果一个算子的 output 误差与某个 input 的误差**多维度一致**（NRE 匹配（相对比例 |out-in|/max(|in|,eps) <= 0.1%）**且** dtype 一致 **且** MeanRE 一致 **且** MaxRE 一致），说明该误差是**从 input 继承来的，算子本身没有引入新误差**，不应将其标记为根因候选或首问题点。这种情况应分类为 **INPUT_PROPAGATION（输入传播）**。
- **正确做法**：跳过继承了 input 误差的 output 节点，继续按执行顺序找下一个真正由算子本身引入误差的节点。
- **错误做法**：仅看 input.0 的误差，忽略 input.1/input.2 等后续 input 的误差，将本应归为 INPUT_PROPAGATION 的节点错误标记为 ROOT CAUSE。

### C-ANALYSIS-006: 算子分组
按 `(prefix, direction)` 分组（direction = `forward` / `backward`），组内按 `_get_param_type(param_key)` 分类：`input` → 输入侧（含 `input` / `kwargs` / `parameters`），`output` → 输出侧（含 `output` 和 `parameters_grad`）。`parameters_grad` 行归到 backward 方向下。若算子缺少输入行则跳过（无法判定传播跳变）。

### C-ANALYSIS-007: 下游吸收检查
非 INPUT_PROPAGATION 的候选节点需进一步检查**下游吸收**——即使算子自身引入了误差，如果该误差被紧接的下游算子（如归一化层）吸收，导致下游 output 恢复到不可忽略标准以下，则此误差**对整网精度影响有限**。这类节点应标记为 **DOWNSTREAM_ABSORBED**，跳过并继续向后查找。判断方法：
1. 对候选节点，按 C-ANALYSIS-011 限定的范围（下游 500 行）扫描执行顺序中位于其后的 output 节点（传播总是按顺序，无需全量扫描）
2. 检查是否有下游算子的某个 input 与候选节点的 output **多维度一致**：NRE 匹配（相对比例 |out-in|/max(|in|,eps) <= 0.1%）**且** dtype 一致 **且** MeanRE 一致 **且** MaxRE 一致
3. 若该下游算子的 output 未触发 C-ANALYSIS-001 的不可忽略判定（即 NRE < 阈值 且 MeanBias < α×阈值，α = 1.2）→ 候选节点的误差被下游吸收了，跳过
4. 若所有下游消费此 output 的算子的 output 均触发不可忽略判定 → 误差持续传播，确认候选为首问题点

**豁免条款**：含 `parameters_grad` 的 backward 算子 SHALL NOT 参与本吸收检查。其参数梯度输出（grad_weight/grad_bias）由 C-ANALYSIS-021 独立评估——即使同算子的 output 行被下游吸收，也不改变参数梯度输出已判 ROOT_CAUSE 的事实。报告 §5.1 以 JSON `top_root_causes.backward` / `propagation.root_cause` 的 `param_grad_output: true` 分类为准，禁止用 output 行把 parameters_grad 根因手工改判为 ABSORBED。

### C-ANALYSIS-008: INPUT_PROPAGATION 溯源
当首问题点候选因 INPUT_PROPAGATION（误差继承自 input）被跳过时，**必须对 input 误差来源进行溯源**——沿执行链向上追溯，找出是哪个上游节点/算子最先引入了该误差。溯源方法：
1. 从被跳过的节点出发，获取其继承误差的 input 名称（如 `input.0`、`input.1`）
   - **继承判断需 NRE + dtype + MeanRE + MaxRE 多维度一致**，避免因数值巧合误判
2. 在 CSV 数据中，**限定当前行向上 500 行范围内**，查找产出该 input 的上游节点——通过 NRE + dtype + MeanRE + MaxRE 多维度一致来匹配，确认该 INPUT 行与上游 OUTPUT 行描述的是同一份数据
3. 检查该上游节点的 input→output 误差变化，判断是"算子引入新误差"还是"继续向上继承"
4. **循环追溯**，直到找到误差源头（引入点）或到达数据覆盖范围的边界。**关键约束**：禁止在找到第一个上游节点后就停止——必须检查该上游节点本身是否也是继承关系，一直追溯到真正的 ROOT CAUSE 或数据边界
5. **如果追溯到数据边界仍无法定位源头**（如 CSV 数据未覆盖到更上游的节点、该 input 来自模型外部输入等），必须在报告中明确说明"无法溯源——数据覆盖不足/input 来自模型外部"
6. 溯源结果必须在报告第 4 节（首问题点）中体现，形成完整的误差传播链描述

### C-ANALYSIS-009: 反向重计算处理
反向过程中可能存在**重计算（recomputation）**——即模型反向传播阶段（backward propagation）重新执行部分前向计算，在 CSV 中表现为以 `.backward.N` 为主体的行序列之间穿插了 `.forward.N` 节点（此处 "forward" / "backward" 指 NPU Name 中的 direction 字段，与 Result 列的 pass/warning/error 无关）。对此类重计算的前向节点，**忽略，不作为独立分析目标**。真实前向（反向传播阶段开始之前的 `.forward.N` 节点）和真实反向（`.backward.N` 节点）正常分析，不做区别对待。

### C-ANALYSIS-011: 分析范围限制
上游溯源限定当前行向上 **500 行**，下游吸收检查限定当前行向下 **500 行**。传播总是按执行顺序发生，无需全量扫描。此范围限制必须在报告 §1（输入信息）和 §4（首问题点）中明确说明。

### C-ANALYSIS-012: 辅助信息处理
- **分析前必须确认辅助信息**：若用户未在初始请求中提供辅助信息，使用 `AskUserQuestion` 工具按 `references/aux-info.md` 中定义的方式询问。若用户已在请求中附带辅助信息（如历史报告路径、已知根因等），直接使用，不再重复询问。
- **禁止自行搜索历史报告**：用户未提供辅助信息时，**禁止**自行搜索、查找或读取历史分析报告（如同目录下的 `*_analysis_report_*.md` 文件），也不得在报告中自行添加"与历史结论对比"类内容。分析者应将本次分析视为独立的全新分析。
- 参见 `references/aux-info.md` 中的完整交互模板和处理规则。

### C-ANALYSIS-013: 首问题点行号范围记录
确认首问题点后，**必须在中间过程中记录其行号范围的起始行号**，作为后续报告 §3 和 §5 各子节过滤的基准。报告中所有可疑节点列表（§3、§5.1~§5.6）均不展示行号范围起始值小于首问题点行号范围起始值的节点。§4（首问题点）不受此限制。

### C-ANALYSIS-014: 多 input 算子复合优先级规则
多 input 算子进行传播跳变分析时，遍历所有 (input, output) 组合后按以下复合优先级分配单一标签：

1. **数据输入优先检查**：若存在任一非参数数据输入（`input.X` 而非 `parameters.X`）的 NRE ≥ threshold，SHALL 先检查算子是否真正放大了误差——仅当 `output NRE > max(数据输入的 NRE) * 1.1`（10% 容差）时，才标记为 ROOT_CAUSE。若 output NRE ≤ max(数据输入的 NRE) * 1.1，算子未放大误差，按 PASS_THROUGH 或 PROPAGATION 处理
2. 所有数据输入都干净（NRE < threshold）时，保持原规则——任一干净 input（含参数 input）→ 脏 output 仍触发 ROOT_CAUSE
3. 非张量输入（bool/int 常量参数，NRE 为 None/N/A）SHALL NOT 参与 clean→dirty 路径判定
4. 仅当所有数据输入都脏（NRE ≥ threshold）且 output 误差相对于最大 input 误差缩小 → 标记为 PASS_THROUGH
5. 同一 (prefix, direction) SHALL NOT 同时标记为 ROOT_CAUSE 和 PASS_THROUGH

**显著放大算子独立呈现**：Jump > 2× 且所有 input NRE < threshold 的 ROOT_CAUSE 算子从主列表移入 `significant_amplifiers` 字段，报告中独立展示。

**参数误差优先级提升**：参数梯度输出（backward `parameters_grad`，如 grad_weight/bias）自动提升优先级一级。参数误差是持久化的——写入权重的偏差影响后续所有 forward，排查优先级高于临时激活误差。

### C-ANALYSIS-015: 首问题点执行顺序链追溯
当首问题点 input 已有显著 NRE（≥ threshold）时，MUST 按执行顺序链（CSV 行号递减）向上游回溯，而非按 tensor shape 匹配：
1. 按 CSV 行号递减回溯（范围 ±500 行），dump 中算子的执行顺序天然反映数据流
2. 跳过纯 shape 变换算子（Reshape/Transpose/Permute/Squeeze/Unsqueeze/Expand/Flatten/View/Contiguous/Split/Cat/Chunk），它们不改变数据值
3. 追溯路径在报告 §1「误差来源追溯」子节中以箭头链呈现
4. 保留 shape 匹配作为辅助验证手段（当执行顺序链结果不确定时交叉验证）

### C-ANALYSIS-017: 分段阈值检测
自适应阈值级联检测 SHALL 在级联前先执行数据分段检测，识别结构性断点（dtype 变化、NRE ≥10× 跃迁；shape 变化不作分段判据——相邻算子 shape 几乎必然不同，按 shape 分段会产生数千微段），对每个段独立运行级联（序列变点 → 锚定回溯 → Delta-NRE 离群 → 分布间隙 → 统计兜底）。全局阈值 = min(所有段有效阈值)。当全局阈值 > 参考值（5%）时，SHALL 触发低信号回退——回溯阈值前 shape/dtype 一致的段，找出超过局部基线（min(0.1%, p50)）的节点作为附加候选。

**分段检测规则**：
- 每段至少包含 30 个节点，否则与前一段合并
- dtype 变化 / NRE ≥10× 跃迁两种断点条件任一满足即分段（shape 不参与）
- 全局阈值取各段阈值的最小值（而非各段的全局级联结果）
- 低信号回退仅在全局阈值偏高时触发，避免引入噪声

**报告中使用规则**：若检测到多段且全局阈值来自前段，报告 §2 中标注"自适应阈值分段检测——数据存在结构性断点，全局阈值由前段确定"。若低信号回退发现附加候选，在报告 §3 中列出。

### C-ANALYSIS-018: Backward 方向感知
Backward pass 中算子的上下游关系以 dump 实际记录顺序为准。若 dump 按执行顺序记录且行号递增，则 backward 方向上：
- **上游**（误差来源方向）SHALL 取更大行号（更接近网络深层）
- **下游**（误差传播方向）SHALL 取更小行号

不假设"backward 行号递减"——以 dump 的实际行号顺序为准。分析执行顺序时 SHALL 检测当前节点是否在 backward pass 中（通过 prefix 含 `.backward` 判断）。

### C-ANALYSIS-019: 多输入算子脏输入路径保留
多 input 算子进行 ROOT_CAUSE 分类时，SHALL 记录脏输入路径信息：
- `dirty_inputs` 字段 SHALL 记录所有 NRE ≥ threshold 的输入，含 param_key、NRE、upstream_source
- `input_subtype` SHALL 区分三类：`INPUT_ALL_CLEAN`（所有输入干净）、`INPUT_PARTIALLY_DIRTY`（部分输入脏但仍有干净输入 → 算子仍可能是误差引入点）、`INPUT_ALL_DIRTY`（所有输入脏 → 纯传播）
- 报告 §5.1 中对 `INPUT_PARTIALLY_DIRTY` 节点 SHALL 列出脏输入路径和上游来源

### C-ANALYSIS-020: 数据覆盖缺口检测
`analyze_stat.py` SHALL 在传播分析完成后自动检测数据覆盖缺口：遍历执行顺序链，若相邻两个可比对节点 A→B 满足 A.output NRE ≤ 噪声水平 且 B.input NRE > threshold 且 A→B 之间不存在其他可比对节点，则标记为 `data_coverage_gap`。

**报告中使用规则**：若存在数据覆盖缺口，报告 §1 SHALL 增加「数据覆盖缺口」子节，列出缺口位置和隐藏节点推测。缺口标注为"误差的真正引入点可能隐藏在不可比对算子中——建议补充数据覆盖"。

**Shape 不匹配分级告警规则**：shape 不一致节点占比 >10% → 报告 §1 标注"关键数据异常"；>50% → 标注"严重数据异常——建议先修复数据结构对齐再进行精度分析"。shape 不一致节点在传播分类中标注"NRE 可能不可信——shape 不一致"。

### C-ANALYSIS-021: Backward 参数梯度输出独立评估（含继承性对照三档）
Backward 方向 `parameters_grad` 输出（grad_weight, grad_bias）SHALL NOT 参与 C-ANALYSIS-014 的 input→output 传播比较。它们是 backward kernel 独立计算的产物，不在梯度传播链上。这些参数梯度输出 SHALL 独立评估，并在**全量分析**（本算子 input_nres 非空）时做**继承性对照三档**判定（2026-08-07 修订）：

1. **继承（inherited）**：backward 输入 max_input_nre ≥ threshold 且 grad NRE ≤ max_input_nre × 2 → 误差继承自本算子输入（grad_output / saved tensors），**降级为传播**（`PARAM_GRAD_INHERITED`，归入 `propagation.input_propagation`），不进 ROOT_CAUSE，指向脏输入来源供溯源
2. **放大（amplified）**：backward 输入 max_input_nre ≥ threshold 且 grad NRE > max_input_nre × 2 → backward kernel 放大/引入误差，真根因特征，ROOT_CAUSE，`inheritance: "amplified"`
3. **实现问题（impl_only）**：backward 输入全部干净（max_input_nre < threshold）仍超阈值 → 纯 backward kernel 实现差异，ROOT_CAUSE，`inheritance: "impl_only"`，最高优先排查

**聚焦子集**（`--keep-only parameters_grad`，input_nres 为空）无输入可对照 → 保持独立评估 NRE ≥ threshold → ROOT_CAUSE（`inheritance: null`），不降级。MeanBias 触发条目不参与继承降级（NRE 比对不可靠），保持 ROOT_CAUSE。

JSON 中 ROOT_CAUSE 条目携带 `param_grad_output: true` + `inheritance` 字段；继承条目出现在 `propagation.input_propagation`，category 为 `PARAM_GRAD_INHERITED`。报告 §5.1 按 `inheritance` 区分：impl_only 最高优先，amplified 次之，inherited 不列为根因。

Forward 方向的 `parameters.weight/bias`（作为算子 input）与 backward `parameters_grad` 不同——前者是合法的算子输入，参与计算，但受 C-ANALYSIS-014 数据输入优先检查约束。

#### Scenario: SyncBatchNorm.backward has both output types
- **WHEN** backward operator has `output.0` (grad_input, NRE=0.578%) and `parameters_grad.0.weight` (NRE=1.563%)
- **THEN** `output.0` SHALL be classified via normal C-ANALYSIS-014 propagation comparison
- **AND** `parameters_grad.0.weight` SHALL be independently evaluated

#### Scenario: 继承档——grad_output 已脏，参数梯度同量级
- **WHEN** backward 算子 `input.0`(grad_output) NRE=2.0% ≥ threshold，`parameters_grad.0.weight` NRE=2.1% (≤ 2×2.0)
- **THEN** `parameters_grad.0.weight` SHALL 归入 `PARAM_GRAD_INHERITED`（input_propagation），SHALL NOT 判 ROOT_CAUSE
- **AND** 根因定位 SHALL 指向 grad_output 的上游来源

#### Scenario: 放大档——输入脏且参数梯度放大超 2×
- **WHEN** backward 算子 `input.0` NRE=1.0% ≥ threshold，`parameters_grad.0.weight` NRE=3.5% (> 2×1.0)
- **THEN** `parameters_grad.0.weight` SHALL 判 ROOT_CAUSE，`inheritance: "amplified"`

#### Scenario: 实现问题档——输入干净仍超阈值
- **WHEN** backward 算子所有输入 NRE < threshold，`parameters_grad.0.weight` NRE=3.0% ≥ threshold
- **THEN** `parameters_grad.0.weight` SHALL 判 ROOT_CAUSE，`inheritance: "impl_only"`，最高优先排查

### C-ANALYSIS-022: Dirty input upstream trace boundary declaration
当首问题点 input 已脏（NRE ≥ threshold）且上游追溯（C-ANALYSIS-008）无法定位误差来源时，SHALL 显式声明数据边界：
- `dirty_inputs` 条目中，若 `upstream_source` 为 null，SHALL 附带 `trace_boundary_reason` 字段说明中断原因：
  - `data_gap`: 无可比对输出节点——数据覆盖不足
  - `no_match`: 存在上游输出但 shape/dtype 不匹配
  - `out_of_range`: 追溯超出分析范围
- `trace_execution_chain` 返回结果中 SHALL 携带 `trace_boundary_reason` 字段
- 报告 §4 中 SHALL 显式声明数据边界："⚠️ 数据覆盖限制：input.X NRE=Y%，但上游无可比对输出节点，误差来源无法在此追溯链上定位"

### C-ANALYSIS-023: 补充候选检测规则（防漏检）
传播分析完成后，SHALL 对未被 `top_root_causes` 合并池覆盖的节点执行补充候选检测。检测 SHALL 使用通用启发特征（不绑定具体算子名/参数名/场景名）：

1. **Family 首脏成员** (a)：各 family（模块族/算子类型族）内执行序最早且 NRE ≥ 阈值的脏成员
2. **Output 显著放大** (b)：output NRE 相对 input 显著放大（jump > threshold × 2）
3. **无 input 参数梯度** (c)：无 input 的参数梯度类节点按 output NRE ≥ 阈值强特征识别

判定指标 SHALL 仅使用 NRE 和 jump，不引入新指标。检测结果 SHALL 写入 JSON `pool_external_indicators` 字段。

#### Scenario: Family 首脏成员标记
- **WHEN** 某 family 内存在多个 NRE ≥ 阈值的节点且均未被 `top_root_causes` 覆盖
- **THEN** 该 family 内 row_range 起始最小的脏成员 SHALL 标记为补充候选
- **AND** 标记理由 SHALL 注明 "family 首脏成员"

#### Scenario: 通用性约束
- **WHEN** 补充候选检测逻辑实现
- **THEN** SHALL NOT 硬编码具体算子名、参数名、场景名作为触发条件

### C-ANALYSIS-024: 首问题点数据质量稀释保护
当比对文件存在高比例 shape/dtype 不一致（critical 级数据异常）、大量脏行 `Result=error` 时，若首问题点节点**自身**的 `input→output` 签名清晰，SHALL 将该首问题点保持为首要可行动结论；数据质量告警 SHALL 降级为「局限说明」，而非唯一结论。

首问题点签名清晰定义：
- **单侧脏输入**：仅一侧 input NRE ≥ 阈值，其他 input 干净，output NRE ≥ 阈值
- **明显引入误差**：所有 input 干净（NRE < 阈值），output NRE ≥ 阈值

#### Scenario: 首点签名清晰且邻域大量 shape 不一致
- **WHEN** 首问题点自身的 input→output 签名清晰（单侧脏输入或明显引入误差）
- **AND** 邻域存在高比例 shape/dtype 不一致（>10%）
- **THEN** 首问题点 SHALL 保持为首要可行动结论
- **AND** 数据质量告警 SHALL 降级为「局限说明」——提示 NRE 可信度受结构错位影响，标注 "⚠️ 数据质量局限：邻域存在结构伪象，首问题点 NRE 可信度可能受结构错位影响，但其 input→output 签名清晰——仍为首要排查方向"
- **AND** SHALL NOT 以数据质量告警作为唯一结论

#### Scenario: 首点签名不清晰且邻域大量 shape 不一致
- **WHEN** 首问题点自身的 input→output 签名不清晰（如所有 input 均脏且 output 无明显放大）
- **AND** 邻域存在高比例 shape/dtype 不一致
- **THEN** 数据质量告警 SHALL 保持 critical 级别
- **AND** 建议 SHALL 优先级为「先修复结构对齐再重比」

#### Scenario: 首点签名清晰且邻域无数据质量问题
- **WHEN** 首问题点自身的 input→output 签名清晰且 shape/dtype 不一致比例 ≤10%
- **THEN** SHALL NOT 触发数据质量稀释保护逻辑
- **AND** 正常按首问题点呈现，无需降级数据质量告警

---

## 报告输出约束

### C-REPORT-001: 报告必须保存为文件
分析完成后，**必须**将完整报告写入 Markdown 文件，保存到比对结果文件所在目录。报告文件名规则：`<比对结果文件名（去掉扩展名）>_analysis_report_<时间戳>.md`，时间戳格式为 `YYYYMMDDHHmmss`。不允许仅在对话回复中输出报告而不写文件。最终输出仅允许 Markdown (.md) 格式文件。

### C-REPORT-002: 必选章节
§1~§7 为必选章节，共 7 个必选章节，缺一不可。§0（辅助信息）仅在用户提供了辅助信息时包含。报告完成后必须自检：逐一核对 §1~§7 章节编号是否存在且顺序正确。报告模板以 `assets/report_template.md` 为权威标准。

### C-REPORT-003: 以算子/模块为粒度呈现
报告中的候选列表、首问题点、传播跳变分析等所有问题节点条目，**必须以算子/模块为单位聚合呈现**，不得按 input/output 细分条目。一个算子的 input/output 数据在 CSV 中通常集中在约 50 行内，应聚合为以一个算子前缀为标识的单一条目。命中原因中可引用 input/output 的具体数值作为证据，但条目本身必须是算子/模块级别。**禁止**将同一个算子的 output.0 行和 output.1 行或 input.0 行列为独立的问题条目。

### C-REPORT-004: 所有节点必须标注行号范围
报告中列出的每个节点/算子必须附带其在原始比对文件中的**行号范围**（CSV/XLSX 行号范围，格式为 `起始行~结束行`，如 `42~48`）。每个算子在比对结果表格中通常占多行（input/output），行号范围必须覆盖该算子在 CSV 中的**所有关联行**。**禁止只标单个行号**。范围确定方法：按 `(prefix, direction)` 分组后，取该组所有行的最小行号和最大行号作为范围边界。

### C-REPORT-005: 行号范围直接体现在表格中
行号范围必须直接出现在所在表格的 `行号范围` 列中，**禁止**以"详见附录"、"见备注"、"参见下方说明"等间接引用方式替代。每个问题节点的行号范围必须与其名称、指标在同一行内直接呈现。

### C-REPORT-006: NPU Name / Bench Name 命名规则
报告中显示的节点名称必须去掉 `.input.N`、`.output.N`、`.parameters.N`、`.parameters_grad.N` 及之后的内容（含这些关键词本身），保留前面的完整路径（包括 `Module.` 前缀、`.forward.N` / `.backward.N` 等调用序号）。

### C-REPORT-007: 可疑节点列表过滤规则
报告中所有可疑节点列表（§3 可疑候选列表、§5 传播跳变分析各子节 §5.1~§5.5、以及 §5.6 传播链总结）中，**均不展示行号范围起始值小于首问题点行号范围起始值的节点**。§4（首问题点）不受此限制。

### C-REPORT-008: 算子列表排序检查
报告所有章节（§3~§5）中列出的算子列表，在确认后**必须再检查一遍**，确保所有算子**按出现顺序（即 CSV 行号升序）排列**。检查方法：逐表核对每行的行号范围起始值是否递增。

### C-REPORT-009: 上下文节省——条目上限
以下章节条目数有上限（均按出现顺序排列，**上限指已过滤后的条目数**——即先按"行号范围起始值 >= 首问题点行号范围起始值"过滤，再取上限条数）：
- 第 5.1 节"ROOT CAUSE"：最多 **15 条**（保底 ≤5 + 常规 ≤10，整表不区分方向）
- 第 5.1a 节"显著放大算子"：最多 **5 条**
- 第 5.2 节"PROPAGATION"：最多 **5 条**
- 第 5.3 节"PASS_THROUGH"：最多 **5 条**
- 第 5.4 节"INPUT_PROPAGATION"：最多 **5 条**
- 第 5.5 节"ABSORBED"：最多 **5 条**
- 第 5.6 节"传播链总结"：汇总 §5.1~§5.5 **所有条目**（不限制条数），**按行号范围升序排列**
- §3 SHALL NOT 包含分类为 INPUT_PROPAGATION 或 ABSORBED 的节点

### C-REPORT-010: 后续建议按排查顺序
后续建议按排查顺序排列，首问题点始终排在第一位。建议项需附带用途说明——第⑤项"关注重点区域"中若有需要补充分析数据的建议，应简要说明其用途。

### C-REPORT-011: 禁止使用 memory
分析过程中不得读取或写入 memory（包括 project_memory、session_memory、user_profile 等），所有分析上下文仅来自当前对话和 CSV 数据。

### C-REPORT-013: §3 内容构成规则
§3（可疑候选列表）的条目构成：**{首问题点} ∪ §5.1 全部条目 ∪ §5.1a 显著放大算子 ∪ §5.2 全部条目 ∪ §5.3 全部条目**，上限 **31 行**（= 首问题点 1 + §5.1 至多 15 + §5.1a 至多 5 + §5.2 至多 5 + §5.3 至多 5），另可追加**至多 5 条「补充候选」**。**不含 INPUT_PROPAGATION 和 ABSORBED**（分别在 §4 首问题点发现链和 §5.4/§5.5 中记录）。

附加规则：
- 首问题点所在行**置顶为第一行**（首问题点可能已在 §5.1~§5.3 中出现，§3 中不额外重复，仅置顶即可）
- §3 中不得出现行号范围起始值 < 首问题点行号范围起始值的节点
- §5.1a 条目标注"显著放大算子"来源标签
- §3 中同一 `(prefix, direction)` 跨 §5.1/§5.2/§5.3 出现时，**合并为一行**：命中原因合并各节信息，分类标签取优先级更高者（ROOT CAUSE > PROPAGATION > PASS_THROUGH），不重复列两行
- 其余按 CSV 行号升序排列

### C-REPORT-012: 行号范围格式强制
报告中所有表格的`行号范围`列 SHALL 使用 `{最小行号}~{最大行号}` 格式（如 `42~48`）。SHALL NOT 出现单个行号数字（如 `42`）。SHALL NOT 使用"详见附录"、"见备注"等间接引用方式替代。

#### Scenario: 行号范围始终为范围格式
- **WHEN** 在报告 §3、§4、§5、§7 等任何表格中展示节点的行号
- **THEN** `行号范围`列 SHALL 始终格式化为 `{最小行号}~{最大行号}`
- **AND** SHALL NOT 出现单个数字或不含 `~` 的字符串

#### Scenario: 范围确认方式
- **WHEN** 确定某算子的行号范围
- **THEN** 按 `(prefix, direction)` 分组后，取该组所有行的最小行号和最大行号作为范围边界
- **AND** 格式化为 `{最小行号}~{最大行号}`（如 `42~48`）

---

## 脚本输出约束

### C-OUTPUT-001: 输出目录规范
所有脚本的中间产物（缓存 JSON、查询结果、验证 JSON 等）SHALL 写入 `<csv_dir>/.compare_result_analyzer/` 子目录。分析报告（`*_analysis_report_*.md`）和验证报告（`*_verify_report_*.md`）SHALL 直接写入 CSV 所在目录。

#### Scenario: 中间产物目录
- **WHEN** `analyze_stat.py` 以 `--format json` 运行且未指定 `-o`
- **THEN** JSON 缓存写入 `<csv_dir>/.compare_result_analyzer/<csv_stem>_result.json`

#### Scenario: 报告位置不变
- **WHEN** agent 生成分析报告
- **THEN** 报告 MUST 保存到 CSV 所在目录，不得进入 `.compare_result_analyzer/` 子目录

#### Scenario: verify_op.py 默认 JSON 路径
- **WHEN** `verify_op.py` 运行且未指定 `-o`
- **THEN** JSON 结果写入 `<csv_dir>/.compare_result_analyzer/<csv_stem>_verify.json`

### C-JSON-001: JSON 输出格式约束
`analyze_stat.py` 以 `--format json` 运行时，SHALL 输出完整的 `AnalysisResult` JSON 结构。`--summary-only` 标记 SHALL 跳过 `output_nodes`、`all_bad_nodes` 和 `op_groups` 的 per-param 明细。`--format text` MUST 保持为默认格式，确保向后兼容。

#### Scenario: JSON 输出完整性
- **WHEN** agent 以 `--format json` 运行 `analyze_stat.py`
- **THEN** 输出 JSON SHALL 包含 `meta`、`first_point`、`propagation`、`noise_filter`、`meta_errors`、`op_groups` 字段

#### Scenario: summary-only 跳过无界明细
- **WHEN** agent 以 `--format json --summary-only` 运行 `analyze_stat.py`
- **THEN** `output_nodes` 和 `all_bad_nodes` SHALL 为空数组
- **AND** `op_groups` 中每个 group 仅含聚合指标（不含 per-param 明细）

#### Scenario: JSON 直接查询
- **WHEN** agent 需要获取特定行号范围或 op 的详细信息
- **THEN** SHALL 直接读取 JSON 文件中的对应字段
- **AND** SHALL NOT 重新运行全量分析

---

## Agent-Side 分类规则

> **用途**: 以下规则由 Agent 执行，替代已移除的 Python 分类函数。
> 数据来源为 `analyze_stat.py --format json` 输出的 Agent-side 预计算字段。

### C-ANALYSIS-025: 显著放大算子判定（Agent 侧）

Agent SHALL 从 JSON `amplifier_candidates` 字段中按以下规则筛选 §5.1a 显著放大算子：

1. **条件 1**: `all_inputs_clean == true`（所有 input NRE < threshold）
2. **条件 2**: 同 (prefix, direction) 组内存在任一 `amplification_ratio > 2.0` 的条目
3. **整组判定**: 任一条目满足条件 2，整个 (prefix, direction) 组即合格
4. **参数梯度输出跳过**: `has_param_grad_output == true` 的条目不参与判定
5. **结果分区**:
   - `output_nre >= threshold` → 保底维度候选（纳入 §5.1），特征: 从干净输入产生异常输出
   - `output_nre < threshold` → 显著放大算子（纳入 §5.1a），特征: 有放大趋势但未达阈值
6. **排序**: `|jump|` 降序，至多 5 条进入 §5.1a

#### Scenario: 无合格条目
- **WHEN** `amplifier_candidates` 中无满足条件的条目
- **THEN** 报告 §5.1a SHALL 标注"无显著放大算子"

### C-ANALYSIS-016: Grad Norm Spike 检测（Agent 侧）

Agent SHALL 从 JSON `spike_indicators` 字段读取预计算指标，判定是否触发 spike 模式：

1. **判定条件**: `has_extreme_backward == true`（存在 backward 方向 NRE > 100% 的极端节点）。
   `forward_backward_ratio > 10.0` 仅作辅助参考——完整前向+反向 dump 的比值通常 < 10，
   不能作为排除 spike 的依据（main 分支 `detect_grad_norm_spike` 同样不要求比值）。
2. **触发后动作**:
   - 清空 `top_root_causes.forward`（全量 `propagation.root_cause` 列表不变）
   - `top_root_causes.backward` 中参数梯度输出节点置顶（`input_nre == null` 的条目）
   - `top_root_causes.backward` 顶部的 FB 关联同族 backward（`fb_associated == true`，
     由脚本从 `fb_association_candidates` 按「与首点同算子族 + backward_nre 最大」置顶）
     SHALL 保留并置为最优先排查项——这是「首点算子的 backward 实现可能有问题」的直接证据
   - 标注 spike 场景已触发
3. **不触发时**: 保持 `top_root_causes` 原样
4. **backward 稀疏属正常**: spike 场景下，若 backward 方向无 ROOT_CAUSE（`top_root_causes.backward` 仅含 FB 关联置顶或为空），属正常——真实反向误差以 PROPAGATION 形态存在于 §5.2，由 §5.2 + spike 置顶 + 补充候选承接，§5.1 不需强行填充 backward 条目

#### Scenario: spike 模式下 forward 为空
- **WHEN** `spike_condition_met == true`
- **THEN** 报告 §5.1 forward 方向 SHALL 标注"梯度爆炸场景，前向候选已自动清空——优先排查 backward 方向"
- **AND** 引导用户执行 §3.7 场景定向过滤流程

### C-ANALYSIS-026: FB Association 检测（Agent 侧）

Agent SHALL 读取 JSON `fb_association_candidates` 字段中脚本预计算的 `confidence` 字段（high/medium/low），**无需自行计算**。该字段由脚本按以下信号强度规则派生：

**置信度判定（纯信号强度，脚本派生规则）**:

| 置信度 | 条件 |
|--------|------|
| high | `forward_jump > threshold` AND `backward_nre > 100%`（仅 forward_dominant 可产生） |
| medium | `forward_jump > threshold` AND `backward_nre` 在 50%~100% |
| medium | `backward_nre > 1000%`（即使 forward_jump 不显著） |
| low | 无 forward 跳变（`forward_jump ≤ threshold` 或 backward_dominant）且 `backward_nre` 在 50%~1000% |

**backward_dominant**（首问题点在 backward 方向）无 forward 跳变可交叉印证，SHALL NOT 判为 high——NRE > 1000% 为 medium，其余为 low。

**报告呈现**:
- high confidence 关联优先排查（脚本已在 `confidence` 字段给出 high/medium/low）
- 与首问题点同族（相同 module path 前缀）的关联为最高优先
- **spike 场景**：与首点同族的 backward 关联已被脚本置顶进 `top_root_causes.backward`
  （`fb_associated == true`），Agent SHALL 在 §5.1 置顶呈现，不得只留在 §4 参考
- 条目多时（>5）只列 top 5，按置信度 + `backward_nre` 降序
- 报告 §4 增加「前向/反向根因关联」子节

### C-ANALYSIS-027: 方向池合并规则（Agent 侧）

Agent SHALL 直接读取 JSON `top_root_causes` 字段填充 §5.1（脚本已按三池合并去重，整表总上限 15 条、不区分方向），**无需自行合并**。三池合并规则（由脚本执行，供参考）：

1. **C-ANALYSIS-013 行号过滤**: 脚本已在 `pool_input` 中预过滤（`row_start >= first_point_row_start`）
2. **候选集合并**: 将 `pool_input.forward[]` 与 `pool_input.backward[]` 合并为统一候选集（每条保留 direction 标注）
3. **执行序池**: 按 `row_start` 升序取前 10 条
4. **量级池**: 筛选 `tagging != 'denominator_effect'`，取 `abs_magnitude` 最大的 10 条
5. **保底池**: 筛选 `is_true_root_cause_feature == true`，取执行序前 5 条
6. **合并去重**: 保底 ∪ 执行序 ∪ 量级，按 `(prefix, direction)` 去重
7. **排序**: 整表统一按 `row_start` 升序（出现顺序为最高优先级；保底只保证入选，不改变位置）
8. **条目上限**: 整表总计 ≤15 条（保底 ≤5 + 常规 ≤10），不区分 forward/backward

#### Scenario: grad_norm_spike 触发
- **WHEN** C-ANALYSIS-016 判定 spike 触发
- **THEN** 脚本已清空 `top_root_causes.forward`，Agent 直接读取
- **AND** backward 参数梯度输出 / FB 关联同族 backward 已由脚本置顶

### C-ANALYSIS-028: 补充候选检测（Agent 侧）

Agent SHALL 从 JSON `pool_external_indicators` 字段按以下特征筛选 §3 补充候选：

1. **(a) Family 首脏成员**: `is_earliest_in_family == true` AND `output_nre >= threshold`
2. **(b) Output 显著放大**: `jump > threshold × 2`（input_nre 不为 null 时）
3. **(c) 无 input 参数梯度**: `is_param_grad_no_input == true` AND `output_nre >= threshold`

筛选结果取 `output_nre` 最大的至多 5 条，并入 §3 表尾（标注"补充候选"来源）。此追加 SHALL NOT 改变主候选池排序语义和各节条目上限。补充候选不得与首问题点或 §5.1~§5.3 已呈现条目重复——若某条同时命中主候选与补充候选，§3 中以主候选呈现，该条补充候选不追加（补充候选优先级最低）。

**Backward / grad_norm_spike 场景族内首脏成员强制补入**:
- 当分析涉及 backward 方向，且同一 `family_key` 有 ≥2 个成员在 `root_cause` 或 `pool_external_indicators` 中出现时
- Agent SHALL 显式列入补充候选（即使不在 NRE-top-5），标注「族内首脏成员」
- 保留完整 prefix 路径，禁止聚合抹平
- 纯 forward 场景不触发此逻辑

#### Scenario: 无补充候选
- **WHEN** `pool_external_indicators` 为空或无满足条件的条目
- **THEN** 报告 §3 不追加补充候选

### C-ANALYSIS-029: 多卡汇总（Agent 侧）

Agent SHALL 按 `references/multi_card_rules.md` 执行多卡汇总分析，不再使用独立脚本。

#### Scenario: 单卡分析
- **WHEN** 用户仅提供单张卡的数据
- **THEN** SHALL NOT 执行多卡汇总流程
- **AND** SHALL NOT Read `references/multi_card_rules.md`

### C-ANALYSIS-030: 参数梯度三分类候选并列呈现（Agent 侧）

当 grad_norm_spike 场景触发或 backward 方向存在 NRE > 100% 极端节点时，Agent SHALL 从 JSON `param_grad_three_category` 字段读取三分类候选，三类**并列呈现、不互斥、不排序压制**，均为"待排查项/候选来源"维度。

三分类定义：
1. **同块成堆**（`same_block_cluster`）：同一子模块块（同一模块路径前缀）内多个参数梯度同时超标——族内集中异常是高置信度信号
2. **孤立大NRE**（`isolated_large_nre`）：单点绝对 NRE 最大的参数梯度——防止绝对值最大的入口被成堆候选淹没
3. **执行序靠前**（`execution_order_first`）：参数梯度中执行序最早出现的超标节点——最接近误差最初扩散点的入口

**呈现规则**:
- 三类仅用于兜底防漏（确保入口不至于因某单一信号不足被整批屏蔽），各自 SHALL 保留既有传播分类标注（误差引入/误差放大/误差继承）
- SHALL NOT 因"三类并列"而全部定性为根因。是否定位为误差引入点仍需按既有传播判定规则（C-ANALYSIS-003/014/021）单独确认
- 在报告 §5.1 后增加「参数梯度三分类候选」子节（单卡）或在多卡报告对应位置增加子节
- 子节抬头 SHALL 提示"以下候选三类并列、不做优先级取舍，均为待排查项而非根因定性"
- 至多每类 5 条，按 NRE 降序排列

**多卡场景**:
- 跨卡聚合时对三分类候选人执行跨卡共识检测（同 M-002 规则，按 `(prefix, direction)` 对齐）
- 多卡报告中在对应子节展示跨卡三分类候选汇总

#### Scenario: grad_norm_spike 场景触发三分类

- **WHEN** `scenario_flags.grad_norm_spike == true` 或 backward 方向存在 NRE > 100% 极端节点
- **THEN** Agent SHALL 读取 JSON `param_grad_three_category`
- **AND** 在报告中增加「参数梯度三分类候选」子节
- **AND** 三类并列呈现、不互斥、不排序压制

#### Scenario: 非 grad_norm_spike 场景不触发

- **WHEN** `scenario_flags.grad_norm_spike == false` 且 backward 方向无 NRE > 100% 极端节点
- **THEN** SHALL NOT 触发三分类候选呈现
- **AND** 报告 SHALL NOT 包含「参数梯度三分类候选」子节
