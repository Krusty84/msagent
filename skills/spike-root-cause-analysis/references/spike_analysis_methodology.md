# Spike 根因定位方法论 (参考)

> 操作指南见 `SKILL.md`，本文为深层方法论背景。

## 概述

三阶段渐进式分析链路:

```
Phase 1 — 梯度监控数据 → 候选 spike 三维坐标
Phase 2 — 跨 step + 跨设备对比 → 根因坐标选定
Phase 3 — Dump 统计数据 (四组对照) → 激活过程差异追溯
```

## 四组对照方法

Phase 3 的核心创新点。异常侧和标杆侧可能存在系统偏差，仅靠 A/B 直接对比容易误判。

| 组 | 含义 | 作用 |
|----|------|------|
| A | 异常坐标 | 待验证的异常点 |
| B | A 的标杆 | 跨设备/跨step的参照 |
| C | 邻近正常 | 同设备另一正常点, 用于消除系统偏差 |
| D | C 的标杆 | 验证标杆对标基线 (C/D 应≈1.0x) |

异常度 = (A/B) / (C/D)。如果 C/D≈1.0x 而异常度显著偏离 → 真异常。

## 数据层次 (原始定义)

**渐进式分析原则:**
- 数据充足时: Phase 1 → Phase 2 → Phase 3 (逐步深入)
- 数据不足时: 分析到何处就输出何处结论，不强行推进
- 每个 Phase 有明确的前置条件，不满足时跳过并说明

---

## Phase 1: 异常前反向定位

### 1.1 数据识别

Phase 1 接受以下任一格式的梯度监控数据:

| 数据类型 | 识别方式 | 说明 |
|----------|----------|------|
| trend.db | SQLite 文件，含 `trend_data` / `monitoring_targets` / `monitoring_metrics` 表 | msprobe 采集的梯度趋势 |
| monitor CSV | 包含 `rank` / `step` / `parameter_name` / `grad_norm` 等列的 CSV | 监控系统导出的梯度数据 |
| parameter_grad dump | 按参数名组织的梯度 dump 文件 | 直接反映 grad norm 的尖刺分布 |

### 1.2 核心概念

**前反向 (Forward/Backward Pass):**
- 通过参数名称可以区分前向和反向过程
- 反向参数通常包含 `grad`、`_grad`、`.weight_grad`、`.bias_grad` 等标识
- 前向参数通常为 `weight`、`bias`、`running_mean`、`running_var` 等
- 同一 step 内可包含多个 micro_step，每个 micro_step 有独立的前反向

**异常前反向:**
- 在同一个参数的所有 step 中，梯度值明显高于其他前反向的过程
- 判断标准: 梯度的 norm/mean/max 偏离同参数历史分布的 N 个标准差

### 1.3 分析步骤

#### Step 1.3.0: 切分分析 (确定前反向粒度)

在开始异常检测之前，必须先分析数据的并行切分方式，以确定前反向的基本单位。

**分析内容:**

1. **PP (Pipeline Parallelism) 检测:**
   - 检查 `vpp_stage` 字段是否有多个不同值
   - 如果 vpp_stage > 1 → 存在 PP 切分，不同 stage 处理同一个前反向的不同部分
   - 需要按 vpp_stage 聚合才能得到完整的前反向

2. **TP (Tensor Parallelism) 检测:**
   - 检查不同 shard/rank 的参数名是否重叠
   - 如果 shard 间参数名**完全不同** → 存在 TP 切分，多个 rank 共同组成一个模型
   - 如果 shard 间参数名**完全相同** → 无 TP，每个 rank 有完整模型

3. **确定前反向粒度:**
   - 有 micro_step 字段 → 前反向单位 = `(step, micro_step)`
   - 无 micro_step 字段 → 前反向单位 = `(step,)`
   - 有 TP → 同一前反向的梯度需要跨 TP rank 聚合后再对比
   - 仅 DP → 每个 rank 独立构成一次前反向，直接按 rank+step 对比

**输出示例:**
```
切分分析结果:
  - vpp_stage: 仅 0 → PP=1
  - 跨 shard 参数重叠: 100% → 无 TP
  - micro_step 范围: 0-71 → 前反向单位 = (step, micro_step)
  - 结论: 纯 DP，单卡有完整模型，每个 (rank, step, micro_step) = 一次完整前反向
```

#### Step 1.3.1: 基线学习 + 异常检测

**不使用静态阈值。** 从正常样本中学习基线分布，然后在趋势上判断差异。

1. **基线学习:**
   - 对所有 step 的该参数梯度统计量，计算分布特征: median, Q1, Q3, IQR
   - 排除明显异常 step 后重新计算鲁棒基线 (MAD-based)
   - 基线即为该参数的"正常范围"

2. **异常判定:**
   - 计算每个 step 的梯度统计量偏离基线的程度
   - 使用基于基线分布的动态阈值 (如 > Q3 + k*IQR，k 根据数据特性自适应)
   - 考虑趋势特征: 单点飙升 vs 逐步攀升 vs 持续高位

3. **输出:**
   - 异常 step 列表，每个 step 携带相对于基线的偏离度量
   - 异常类型标记: 突发 / 持续 / 累积

#### Step 1.3.3: 前反向聚合

将所有参数的异常按前反向维度聚合:

1. 同一个前反向内的多个参数都异常 → 该前反向异常
2. 某个前反向内仅有部分参数异常 → 该前反向局部异常，需记录具体参数
3. 输出: 异常前反向列表 `[{step, micro_step, direction(fwd/bwd), abnormal_params, deviation}]`

#### Step 1.3.4: 根因前反向判定

在多个异常前反向中，判定最可能是根因的那个。

**规则 A: 单运行内分析 (仅有一份监控数据时)**

```
IF step-N 的所有前反向梯度普遍偏高 (相比 step-(N-1))
THEN 根因在 step-(N-1) 的前向过程
     (前向参数变化 → 导致 step-N 整体梯度异常)

IF step-N 有 M 次前反向，其中仅第 K 次异常
THEN 第 K 次前反向就是候选根因
     需进一步分析该次前反向的计算过程
```

**规则 B: 跨设备对比 (有标杆设备和异常侧设备两组数据时)**

```
示例: 标杆设备 GPU (如 H800)，异常侧设备 NPU (如昇腾)——标杆不限于 GPU，可为任意对照设备

IF NPU 和 GPU 在 step-N 都有 M 次前反向异常
   BUT NPU 第 K 次的偏离幅度显著大于 GPU 第 K 次
THEN NPU 第 K 次前反向 → 根因 (设备差异导致)

IF NPU 有异常，GPU 没有异常
THEN 异常是 NPU 特异的 → 高概率根因

IF 两者异常模式高度一致
THEN 排除设备特异性问题 → 可能是数据或模型本身的问题
```

跨设备对比只用于标注「双端共有/设备特异」，**不推翻规则 A 的跨 step 根因判定**——标杆侧同转折也增长不否定根因 step（异常侧增长就是前一步污染的结果，前一步即根因）。

### 1.4 用户交互场景

| 场景 | 需要确认的内容 |
|------|----------------|
| micro_step 编号不一致 | "监控数据的 step 编号和 dump 数据的 step 编号是什么对应关系？" |
| 数据中参数名前缀不一致 | "数据中的参数名格式与预期不符，请确认模型的参数命名规则" |
| 仅有单设备数据 | "当前仅有 NPU 数据，无法进行跨设备对比。建议补充标杆设备数据以获得更可靠的根因判断" |
| 多个候选根因无法区分 | "定位到 N 个异常前反向候选，无法进一步区分。建议补充 [具体数据类型] 以进行 Phase 2 分析" |

### 1.5 输出格式

```json
{
  "phase": 1,
  "status": "completed" | "partial",
  "anomalous_passes": [
    {
      "step": 5933,
      "micro_step": 0,
      "direction": "backward",
      "abnormal_params": ["layers.4.q_proj.weight_grad", "layers.4.k_proj.weight_grad"],
      "max_z_score": 12.5,
      "baseline_norm": 1.23,
      "observed_norm": 15.4,
      "deviation_ratio": 12.5
    }
  ],
  "root_cause_pass": {
    "step": 5933,
    "micro_step": 0,
    "direction": "forward",
    "reasoning": "step-5933 反向梯度异常追溯: step-5933 前向计算产生异常的激活值，导致后续反向梯度普遍偏高。跨设备对比: NPU 偏离幅度显著大于 GPU，排除数据问题。"
  },
  "cross_device_comparison": {
    "available": true,
    "baseline_device": "GPU",
    "conclusion": "NPU 前向异常幅度为 GPU 的 3.2 倍，判断为 NPU 硬件/算子实现差异"
  },
  "next_steps": "建议进行 Phase 2 分析: 对比异常前向的 dump 统计数据与标杆前向"
}
```

---

## Phase 2: 激活过程差异追溯

### 2.1 数据识别

Phase 2 需要 msprobe dump 统计数据:

| 文件 | 内容 |
|------|------|
| `dump.json` | 各算子的输入/输出张量统计 (Max/Min/Mean/Norm) |
| `construct.json` | 算子调用层级关系 (算子是哪个模块的哪部分) |
| `stack.json` | 算子的 Python 调用栈 |
| `config.json` | dump 配置 (scope/list/level 等) |

### 2.2 核心概念

**标杆前反向:**
- 同一次运行中，spike 之前的正常 step 对应的前反向
- 或另一台设备的同一 step 对应前反向
- 标杆的选择由用户能提供什么数据决定
- 与用户确认: "你希望用哪个 step/哪个设备作为标杆进行对比？"

### 2.3 分析步骤

#### Step 2.3.1: 数据对齐

1. 确认异常前反向对应的 dump 数据位置 (哪个 step/rank 目录)
2. 确认标杆前反向对应的 dump 数据位置
3. 确认两份数据的 construct.json 算子命名一致（不同设备可能有命名差异，如 `NPU.npu_rms_norm` vs `Triton.rms_norm`）
4. 建立异常与标杆的算子名称映射表

#### Step 2.3.2: 反向往前向追溯

**核心逻辑:** 追溯的目标是找到异常**首次出现**的计算位置，即异常开始的算子。loss 只是途经点，不影响追溯过程。

```
追溯方向: 反向末端 → 反向中间 → 反向起始 → (跨 loss) → 前向输出 → 前向中间 → 前向输入

                       前向过程 →
  输入 → [op1] → [op2] → [op3] → loss
                               loss ← [grad3] ← [grad2] ← [grad1]
                       反向过程 ←

追溯从 grad1 开始，往 grad3 方向追溯，跨过 loss 后往前向 op3/op2/op1 追溯
```

**跨 loss 边界说明:**

用户通常没有直接提供 loss 的数据。跨 loss 时有两种可能:
- **loss 层面可见差异:** 前向激活值异常导致了 loss 的 max/min 也异常 → 异常在 loss 处可见
- **loss 层面不可见差异:** 前向激活值异常，但 loss 聚合后看不出明显异常 → 异常在 loss 处不可见

两种情况的追溯逻辑一致，不依赖 loss 数据:
1. 对比当前算子(异常 vs 标杆)的输入/输出统计值
2. 有差异 → 标记该算子，继续往**计算顺序的前方**追溯
3. 无差异 → 该算子不是异常引入的来源，但继续往前方检查
   - **局部拉回:** 可能出现中间某个算子差异消失，但更前方的算子差异又复现的情况
   - 这是因为某些算子(如 normalization)可能暂时将异常值拉回正常范围
   - 必须追溯**全程**，不能因为中间某个算子无差异就停止
4. 追溯终点: 计算图中最早出现差异的算子，该算子是异常**开始**的位置

#### Step 2.3.3: 差异判定

**不使用静态阈值。** 从标杆数据中学习每个算子的正常统计分布，然后在趋势上判断异常侧的偏离。

1. **基线学习:**
   - 从标杆 dump 数据中，提取每个算子的 Max/Min/Mean/Norm 统计值
   - 如果标杆有多个 step/rank 样本，计算跨样本的均值和方差，建立该算子的"正常值范围"
   - 如果标杆只有一个样本，使用该样本值作为参考点，结合 Phase 1 中该算子的梯度波动特性估算合理范围

2. **差异度量:**
   - 不只看单点比值 (relative_diff)，还要看差异在计算链上的**趋势变化**
   - 一个算子本身差异不大，但如果其上游算子也无差异、下游算子差异突然增大 → 该算子可能是引入点
   - 差异不仅看绝对值，还看差异在输入/输出之间是否被**放大**
     - 输入差异小 → 输出差异大: 该算子放大了异常
     - 输入差异大 → 输出差异大: 异常来自上游，该算子只是传递

3. **关键判断:**
   - Max 差异大但 Min 差异小 → 局部尖峰 (可能是某个 token 位置异常)
   - Max 和 Min 都差异大 → 整体偏移 (可能是算子计算逻辑问题)
   - Mean 差异大 → 系统性偏差
   - Norm 差异大 → 整体幅值异常
   - 跟踪差异在算子链上的**趋势**: 放大/缩小/持平

#### Step 2.3.4: 调用链分析

使用 `construct.json` 构建异常算子的调用层级链:

```
Module.module.module.decoder.layers.4.TransformerLayer.forward
  ├── input_layernorm.RMSNorm.forward
  ├── self_attention.MLASelfAttention.forward
  │   ├── linear_q_proj.TEColumnParallelLinear.forward
  │   │   ├── Tensor.reshape.forward
  │   │   └── Torch.matmul.forward          ← 差异首次出现!
  │   ├── linear_kv_down_proj.forward
  │   └── TEDotProductAttention.forward
  └── mlp.MoE.forward
```

结合 `stack.json` 获取代码位置信息。

### 2.4 输出格式

```json
{
  "phase": 2,
  "status": "completed" | "partial",
  "baseline_info": {
    "type": "same_run_normal_step" | "cross_device",
    "source": "GPU step 62, rank 59"
  },
  "trace_path": [
    {
      "op_name": "layers.4.self_attention.linear_q_proj.TEColumnParallelLinear.forward",
      "direction": "forward",
      "order": 1,
      "input_diff": {"Max": {"abnormal": 125.3, "baseline": 3.2, "ratio": 39.2, "level": "critical"}},
      "output_diff": {"Max": {"abnormal": 98.7, "baseline": 2.8, "ratio": 35.3, "level": "critical"}},
      "construct_chain": ["TransformerLayer.forward", "MLASelfAttention.forward", "TEColumnParallelLinear.forward"],
      "stack_info": "Megatron-LM/megatron/core/tensor_parallel/layers.py, line 245"
    }
  ],
  "divergence_point": {
    "op_name": "Torch.matmul.25.forward",
    "reason": "这是追溯链中最早出现显著差异的算子，输入正常但输出异常，说明该算子的计算过程引入了差异"
  },
  "next_steps": "建议补充 Torch.matmul.25 的输入/输出 .pt 张量做元素级验证"
}
```

---

---

## 综合报告格式

完成所有可能的分析阶段后，汇总为结构化报告:

```markdown
# Spike 根因分析报告

## 1. 分析概要
- 数据范围: [数据层次说明]
- 分析阶段完成情况: Phase 1 ✓ / Phase 2 ✓ / Phase 3 -
- 置信度: [高/中/低]

## 2. 异常前反向定位 (Phase 1)
- 异常前反向: [step/micro_step/方向]
- 偏离程度: [Z-score 或比率]
- 根因判定: [推理过程]
- 跨设备对比结论: [如有]

## 3. 激活过程差异追溯 (Phase 2)
- 标杆来源: [说明]
- 差异追溯路径: [从反向到前向的算子链]
- 分叉点: [最早出现差异的算子]

## 4. 结论与建议
- 根因: [最终判断]
- 修复建议: [具体措施]
- 数据补充建议: [如分析不完整，建议提供什么数据]
```

---

## 用户交互原则

分析过程中遇到以下情况时需要与用户确认，其他情况按方法论自动推进:

1. **数据对应关系不明确:** 监控数据的 step 编号与 dump 数据的 step 编号不一定对应，需要确认
2. **缺少标杆数据:** 无法进行对比分析时，告知用户需要什么数据，输出当前结论
3. **分析无法继续:** 当前数据已分析到头，输出结论并说明缺失项

核心原则: **数据够就分析，不够就输出当前结论，不强行推进。**
