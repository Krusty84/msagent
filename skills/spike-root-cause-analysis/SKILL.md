---
name: spike-root-cause-analysis
description: >
  分布式训练梯度尖刺 (Gradient Spike) 根因定位。基于梯度监控数据 (trend.db/CSV)、
  msprobe dump 统计数据和张量快照，四阶段渐进式从异常坐标定位逐层深入到算子级和张量级根因。
  当用户提供 spike/梯度尖刺/梯度异常相关数据时使用此 skill。
keywords: [spike, 梯度尖刺, gradient spike, 梯度异常, 根因定位, root cause, dump_statistic,
  trend.db, parameters_grad, 参数梯度, 前反向, 激活追溯, 张量对比]
---

# Spike 根因定位分析

## 功能概述

四阶段渐进式分析链路:

```
Phase 1 — 梯度监控数据 → 候选 spike 三维坐标
Phase 2 — 跨 step + 跨设备对比 → 根因坐标选定
Phase 3 — Dump 统计数据 → 激活过程差异追溯
Phase 4 — 张量数据 → 精确定位
```

**核心原则:** 数据够就分析，不够就输出当前结论，不强行推进。

### 决策树

```
用户提供数据
  ├── 有 trend.db / monitor CSV / dump parameters_grad
  │     ├── 运行 Phase 1 → 候选 spike 坐标
  │     └── 运行 Phase 2 → 根因坐标选定
  │           ├── 有 dump_statistic → 运行 Phase 3 (四组对照追溯)
  │           │     ├── 有 .pt 张量 → 运行 Phase 4 (张量验证)
  │           │     └── 无 .pt → Phase 3 结论为终态 + 建议补充张量
  │           └── 无 dump → Phase 2 结论为终态 + 建议补充 dump
  └── 仅有 dump_statistic (无监控数据)
        └── 直接用 Phase 1 --dump 模式 → Phase 2 → Phase 3
```

---

## 数据识别

| 数据类型 | 识别方式 | 可用于 |
|----------|----------|--------|
| trend.db | SQLite，含 `trend_data`/`monitoring_targets`/`monitoring_metrics` | Phase 1 |
| Monitor CSV | 含 `vpp_stage,name,step,micro_step,min,max,mean,norm,shape,dtype` | Phase 1 |
| dump_statistic | 目录含 `dump.json`/`construct.json`/`stack.json` (parameters_grad) | Phase 1, Phase 3 |
| .pt 张量文件 | PyTorch 序列化张量，文件名含算子路径 | Phase 4 |

---

## Phase 1: 候选 spike 三维坐标

### 脚本

```bash
# trend.db / CSV
python scripts/trend_db_spike_detector.py <data.db|csv> [--csv] [-o p1.json]

# dump_statistic (parameters_grad)
python scripts/trend_db_spike_detector.py <dump_dir> --dump [-o p1.json]
```

### 分析流程

**Step 0: 切分分析**

自动检测 `vpp_stage` (PP) 和跨 rank 参数重叠度 (TP)，确定前反向粒度。

对 DB 数据，通过 norm 周期性重置检测梯度累积窗口，
判断 `step` 列实际是否为 micro_step。如有累积则派生 `optimizer_step` 和 `micro_step`。

对 dump_statistic 数据，自动检测目录结构 (单 step / 多 step)，提取所有 `parameters_grad.*` 条目的梯度 norm。

**Step 1: 异常检测 (根据数据类型分支)**

- **step 级别数据** (无累积窗口): 每个 step 是独立完整梯度 → 按参数计算全局基线 (MAD/IQR)，动态阈值检测
- **micro_step 累积数据** (有 accumulation_window):
  1. 每个 optimizer step 取最终 micro_step 的累积 norm，按绝对值降序取 **top-3** 个 `(rank, target)` 作为 suspect
  2. 对每个 suspect 展开 micro_step 累积曲线，计算相邻 delta，**组内 IQR** 检测 delta 突变点
  3. 每 suspect 取 top-2 个突变 micro_step
- **dump 单 step 数据**: 无时间维度，按梯度 norm 绝对值排序取 top-N
- **dump 多 step 数据**: 按参数计算跨 step 基线 (MAD/IQR)，动态阈值检测

**输出:** 各类数据统一输出 anomalies JSON，字段因数据类型略有差异。

### 输出 JSON (关键字段)

```json
{
  "sharding_analysis": {
    "pp_stages": <N>, "has_tp": <bool>,
    "accumulation_window": <N>, "optimizer_step_count": <N>,
    "pass_unit": "<描述>"
  },
  "target_rank_norms": { "<opt_step>": { "target_name": "<name>", "ranks": {"<rank>": <norm>, ...} } },
  "anomalies": [
    {
      "rank": <N>, "step": <N>, "micro_step": <N>, "optimizer_step": <N>,
      "target_name": "<完整参数路径>",
      "norm": <float>, "delta": <float>, "deviation_ratio": <float>,
      "suspect_final_norm": <float>, "trigger": "<方法>"
    }
  ]
}
```

关键字段:
- `sharding_analysis.accumulation_window` — 梯度累积窗口 (0 表示 step 级别或 dump 数据)
- `target_rank_norms` — 每 opt_step top target 的**全 rank** 最终 norm，供 Phase 2 跨 step 对比 (micro_step 数据专有)
- `anomalies[].delta` — 该 micro_step 的梯度增量 (micro_step 数据专有)
- `anomalies[].norm` — 梯度 norm (dump 数据以此排序)

---

## Phase 2: 根因坐标选定

### 脚本

```bash
# 单设备
python scripts/phase2_root_cause_selector.py --phase1 p1.json [-o p2.json]

# 跨设备 (有标杆)
python scripts/phase2_root_cause_selector.py --npu <p1.json> --gpu <p1.json> [-o p2.json]
```

### 分析流程

**Step 2.1: 确定关注 target**

每个 optimizer step 取 Phase 1 中 `suspect_final_norm` 最大的 target。
对 dump 数据，直接取 top-1 参数的 norm 最大的 rank。

**Step 2.2: 跨 step 根因判定** (仅 micro_step 数据，dump 数据跳过)

用 `target_rank_norms` 中全 rank 最终 norm，比较相邻 opt_step:

```
IF 相邻 opt_step 间，大多数 rank (>= 60%) 的最终 norm 增长 >= 2x
THEN 根因在前一个 opt_step (异常梯度 → 异常权重更新 → 后续爆炸)
ELSE 取异常偏离最大的 opt_step
```

**Step 2.3: 选定坐标**

- **无标杆**: 取 delta 或 norm 最大的 `(rank, target, micro_step)`
- **有标杆**: 从异常侧最异常坐标开始，逐个检查标杆侧同位置的表现:
  - 标杆侧同坐标也有异常且值接近 (比值 < 2x) → 跳过，两边表现一致
  - 标杆侧同坐标无异常 或 异常侧显著更高 (比值 >= 2x) → 选定，标记为设备特异性

### 输出 JSON

```json
{
  "phase": 2,
  "root_opt_step": <N>,
  "reasoning": "<跨 step 判定逻辑>",
  "root_cause_coordinate": {
    "optimizer_step": <N>, "rank": <N>, "micro_step": <N>,
    "target_name": "<完整参数路径>",
    "delta": <float>, "norm": <float>
  },
  "cross_device_verification": "<跨设备对比结论>"
}
```

---

## Phase 3: 激活过程差异追溯

### 前置条件
- Phase 2 已输出根因坐标
- dump_statistic 数据 (至少异常侧)

### 脚本

```bash
python scripts/dump_statistic_analyzer.py \
  --abnormal <dump_dir> [--baseline <dump_dir>] [--cross-device] [-o p3.json]
```

### 分析步骤

#### Step 3.1: 数据准备 (四组对照)

用四组数据消除系统偏差，对照组验证标杆对标基准:

| 组 | 含义 | 来源 |
|----|------|------|
| **A** | 异常坐标 | Phase 2 输出 |
| **B** | A 的标杆 | 用户指定 (跨设备标杆 或 同设备正常 step) |
| **C** | 邻近正常坐标 | 同设备另一正常 rank/step |
| **D** | C 的标杆 | C 对应的标杆侧数据 |

**降级规则:** 数据不足时按最多可用组数，最少 2 组 (A+B)。优先级: 4 组 > 3 组 > 2 组。

- 4 组 (A/B/C/D): 完整对照，C/D 验证标杆对齐，异常度 = (A/B) / (C/D)
- 3 组 (A/B/C): 缺 D，A/B vs C/B 对比
- 2 组 (A/B): 仅异常 vs 标杆，降级为直接 A/B 对比

#### Step 3.2: 选锚点 module

选取 **至少两个** 在 A/B/C/D 中都存在且命名可对齐的 module 作为锚点 (如 `input_layernorm`、`self_attention`)。

跨设备对比时，同类算子命名可能不同，需按**功能**匹配而非精确名称匹配。不确定时向用户确认映射关系。

#### Step 3.3: 逐层对比 (强制输出)

**必须打印以下表格。每锚点分别输出前向+反向各列的 Norm 值。**

**强制规则:**

1. **必须同时看 Max、Min、Norm 三个指标**，禁止只看 Norm 就下结论
2. **必须遍历全部层**，不能抽检
3. **前向和反向分别对比**，传播方向不同: 前向 浅层→深层, 反向 深层→浅层
4. **对照组验证**: C/D 应该 ≈ 1.0x。如果 C/D 明显偏离 1.0x → 标杆对标有问题，先确认数据
5. **异常判定**: C/D ≈ 1.0x 的前提下，A/B 明显偏离 → 真异常
6. **找分叉起点**: 从传播起始端开始，第一个 A/B 持续偏离而 C/D≈1 的层

对比输出表格格式:

```
anchor: <module_name>  四组: A(异常) B(异常标杆) C(邻近正常) D(邻近标杆)  异常度=(A/B)/(C/D)
  L |     F_A     F_B     F_C     F_D | F_A/B F_C/D F异常 |     B_A     B_B     B_C     B_D | B_A/B B_C/D B异常
 L<N>|<val> <val> <val> <val> | <r> <r> <r> | <val> <val> <val> <val> | <r> <r> <r>
 ...
```

列说明:
- `F_A/B` = A 前向 Norm / B 前向 Norm，`F_C/D` = C 前向 Norm / D 前向 Norm
- `F异常 = (A/B) / (C/D)` = 消除系统偏差后的净前向异常度
- `B_A/B`、`B_C/D`、`B异常` 同理 (反向)
- 标注: `F!=Nx` = 前向异常度 > 阈值, `B!=Nx` = 反向异常度 > 阈值

#### Step 3.4: 判断方向

- 前向异常层列表 和 反向异常层列表分别汇总
- **前向分叉优先**: 计算顺序是前向→反向，前向分叉可能是反向异常的源头
  - 前向有分叉 + 反向也有分叉 → [P0] 先追溯前向分叉点，[P1] 再追溯反向放大
  - 仅反向有分叉 → 反向自身产生异常
- 前向分叉幅度 << 反向分叉幅度 → 反向放大了前向的差异
- C/D ≈ 1.0x 且 B/C/D 都正常而仅 A 异常 → A 独有异常

#### Step 3.5: 定位分叉层 → 下钻

在分叉层内展开该层所有 sub-module 算子，对比 A/B/C/D，找到差异最大的算子。
然后检查该算子在前面层是否已有分叉，直到差异不可分辨。

---

## Phase 4: 张量级精确定位

### 前置条件
- Phase 3 已定位分叉层和方向
- 优先级: 前向分叉 [P0] > 反向分叉 [P1] (前向在反向之前)
- 异常侧和标杆侧都有对应算子的 .pt 文件

### 分析步骤

1. 加载异常侧/标杆侧 .pt 文件，比对张量 shape/dtype/统计量
2. 分析元素级差异分布: 极端值位置、diff>阈值的元素占比
3. 判断异常模式: 孤立 token 位置 vs 全层系统性偏差
4. 结合 Phase 3 层结论解释: 张量证据是否支持统计差异

### 缺失数据处理

如果缺少 Phase 3 定位的根因层张量:
- 用已有张量旁证 Phase 3 结论（标注局限性）
- 明确列出所需张量数据及优先级

---

## 报告输出格式

遵循 msagent 标准: **问题 / 证据 / 影响 / 建议** + 表格。

### Phase 1 报告

**数据总览** (表格): 数据路径、格式、Step/Rank/参数范围、异常数

**模型切分分析** (表格): PP、TP、累积窗口、前反向单位

**异常列表** (表格, 按 optimizer_step 分组): Opt Step / Rank / Target / Spike ms / Delta / 偏离 / 最终 Norm

**结论**:

```
问题：<spike 总体描述，最异常前反向和偏离倍数>
证据：<top 异常坐标及跨 step 趋势>
影响：<对训练的影响>
建议：[P0] Phase 2 根因选定 → [P1] Phase 3 dump 追溯
```

### Phase 2 报告

```
根因坐标: (opt_step=<N>, rank=<N>, target=<name>, micro_step=<N>)
选定理由: <跨 step 判定 + 跨设备验证>
下一步: Phase 3 dump 追溯
```

### Phase 3 报告

**前置:** 打印四组对照逐层对比表格 (Step 3.3)。

**结论:**

```
锚点1 + 锚点2 前反向全层对比:

对照组(C/D): 全层 ~1.0x → 对齐验证通过 ✓ (或 ⚠ 有偏差，需确认)

前向异常层: [Lx-Ly] 异常度范围
反向异常层: [Lx-Ly] 异常度范围

方向判定: <前向/反向 哪个是主因，传导关系>

证据: <关键层具体 Norm 值和比值>
影响: <对梯度 spike 的解释>
建议: [P0] Phase 4 张量验证 → [P1] 算子实现对比
```

### Phase 4 报告

```
问题: <张量级异常具体位置>
证据: <张量 shape, 异常元素数量/占比, max差异>
关联 Phase 3: <张量发现与层统计差异的一致性>
影响: <如何向上传播>
建议: [P0] <补充数据> [P1] <修复建议>
```

---

## 用户交互指南

- **数据类型不明确** → 向用户确认
- **step 编号对应关系** → 监控/dump 的 step 体系可能不同，需确认
- **是否有标杆** → 不确定时向用户确认哪侧是标杆
- **锚点 module 命名不一致** → 跨设备对比时确认映射关系
- **Phase 1 候选过多** → 用户可手动指定坐标跳过 Phase 2
- **分析到头** → 输出当前结论 + 明确缺失项

**渐进降级:** 无 Phase 1 数据 → 无法开始。无标杆 → Phase 2 降级为单设备。无 dump → 以 Phase 2 坐标为终。无 .pt → 以 Phase 3 为终。

---

## 注意事项

- **DB step 可能是 micro_step**: trend.db 的 `step` 列可能实际是梯度累积的中间步，脚本通过 norm 周期性重置自动识别
- **micro_step 数据用 delta**: 梯度累加中评判标准是相邻步间的增量 (delta)，非累积绝对值
- **step 级别数据用绝对值**: 每个 step 是独立完整梯度，直接用 IQR 异常检测
- **跨设备对比从最异常开始**: 按异常侧 delta/norm 降序逐个检查标杆侧同位置，找差异最大的
- **前向优先于反向**: 计算顺序上前向在反向之前，前向分叉可能是反向异常的根因
- **不带静态阈值**: 基线从正常样本学习 (MAD/IQR)，不同数据分布自适应
- **Max/Min/Norm 三个指标必须都看**: 禁止只看 Norm 就下结论
- trend.db 的 `monitoring_metrics` 表记录 metric_id 与实际指标名的对应关系，需从 DB 读取不可硬编码
