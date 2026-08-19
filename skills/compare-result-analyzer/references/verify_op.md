# verify_op.py 单算子精度验证

> **用途**: 本文件为 `scripts/verify_op.py` 的完整参考手册。agent 在执行单算子验证（SKILL.md Step 8）时，可 Read 本文件了解所有 CLI 参数、容差体系、构造策略及结果解读。

---

## 概述

`verify_op.py` 从 msProbe compare 输出的 CSV 中读取算子信息，根据 **NPU 侧统计值**（shape / dtype / mean / l2norm / min / max）构造输入 tensor，分别在 CPU 和 NPU 上运行同一算子，比对输出是否一致。

核心思想：**同一份合成数据 → 分别创建 CPU 和 NPU 两个 leaf tensor → 确保两边输入完全相同 → 比对结果只反映硬件计算差异**。

---

## 数据来源

CSV 中以下 **NPU 侧列**被使用（取 NPU 侧而非 Bench 侧，因为 NPU 侧反映实际输入分布）：

| CSV 列 | 用途 |
|--------|------|
| `NPU Name` | 算子名 + 实例号 + 方向 + input/output 序号 |
| `NPU Tensor Shape` | tensor 形状 |
| `NPU Dtype` | 数据类型（torch.float32 等） |
| `NPU Requires_grad` | 是否需要梯度 |
| `NPU max` | 值域上界 |
| `NPU min` | 值域下界 |
| `NPU mean` | 均值 |
| `NPU l2norm` | L2 范数 |

---

## 验证流程

```
CSV 解析
  │
  ▼
按 (算子名, 实例号) 分组 → OpGroup
  │
  ├── 前向验证
  │     ├── 从 fwd_inputs 统计值构造 tensor 对 (CPU / NPU)
  │     ├── 分别在 CPU / NPU 上执行算子
  │     └── 逐元素比对输出: max_diff / l2norm_diff / max_rel_err
  │
  └── 反向验证（前向通过 + 有反向数据时）
        ├── 重建前向计算图
        ├── 从 bwd_inputs 统计值构造 grad_output
        ├── 分别在 CPU / NPU 上 backward
        └── 通过 shape 匹配找到对应梯度，逐一比对
```

---

## CLI 参数

### 主要参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `csv` | 位置参数 | — | msProbe compare CSV 文件路径 |
| `--op-list` | str | — | 逗号分隔的待验证算子实例列表，如 `"Tensor.__truediv__.3,torch.matmul.1"`，支持 `.backward` / `.forward` 方向限定 |
| `--output, -o` | str | — | 输出 JSON 报告路径 |

### 输出容差参数

验证使用 dtype 自适应容差（内置于 `verify_op.py`，无需 CLI 传参）：

| dtype | atol | rtol | max_rel_err 阈值 | 依据 |
|---|---|---|---|---|
| float64 | `1e-9` | `1e-7` | 0.00001% | 双精度 eps≈2.22e-16，CPU/NPU 差异应极小 |
| float32 | `1e-4` | `1e-3` | 0.1% | ≈840× eps(1.19e-7)，matmul K=1024 理论累加误差 ≈1.2e-4 |
| float16 | `1e-3` | `1e-3` | 0.1% | ≈1× eps(9.77e-4)，元素级操作硬件差异 |
| bfloat16 | `5e-3` | `5e-3` | 0.5% | ≈0.6× eps(7.81e-3)，元素级操作硬件差异 |

容差按 dtype 分档，取保守值；rtol 与 atol 同量级，归约操作（matmul 等）的正常累加差异由 rtol 兜底。验证的实质用途是确认分析报告标记的可疑算子——这些算子的 NRE 通常远超任何合理容差。

---

## 构造策略

### 两种策略

| 策略 | 算法 | 适用场景 |
|------|------|----------|
| **randn** | `randn → scale(l2norm) → shift(mean) → clamp(min,max)` | 区间宽松，clamp 截断少 |
| **truncnorm** | 逆 CDF 法在 `[min, max]` 内采样 → scale → shift → clamp | 区间紧，clamp 截断多 |

### 自适应选择（`auto`，默认）

```
std_est = target_l2norm / sqrt(n_elements)
span = max - min

span >= 4 × std_est → randn（区间宽松，≈5% 以下样本被 clamp）
span <  4 × std_est → truncated normal（区间紧，需有界分布）
```

**特殊处理**：
- 标量（`dim = 0`）：直接用均值 clamp 到 `[min, max]`
- `l2norm < 1e-30`：接近零向量，直接填充均值

### Truncated Normal 实现细节

逆 CDF 法（inverse transform sampling），不引入外部依赖：

```
α = Φ((min - μ) / σ)        # 标准正态 CDF 在 min 处的值
β = Φ((max - μ) / σ)        # 标准正态 CDF 在 max 处的值
u ~ Uniform(α, β)            # 在 [α, β] 上均匀采样
x = μ + σ × √2 × erfinv(2u - 1)   # 逆 CDF 变换
x = x.clamp(min, max)        # 数值精度兜底
```

其中 `μ = target_mean`，`σ = target_l2norm / sqrt(n_elements)`。

**退化处理**：当 `β - α ≤ 1e-15`（区间极窄）时，降级为均匀分布 `Uniform(min, max)`，由质量校验捕获。

---

## 构造质量校验

每次构造完成后，回算实际统计值与目标值的偏差。

### 质量指标

| 指标 | 计算方式 | 默认阈值 | CLI 覆盖 |
|------|----------|----------|----------|
| l2norm 相对偏差 | `|actual_l2norm - target_l2norm| / max(target_l2norm, 1e-12)` | 5% | `--construct-l2norm-rtol` |
| clamp 比例 | `n_clamped / n_elements` | 10% | `--construct-clamp-ratio` |

**标量不校验**（`dim = 0` 时直接跳过）。

### 降级判定

```
l2norm 偏差 ≥ 5%  OR  clamp 比例 ≥ 10%  →  construct_degraded = True  →  降级标记
l2norm 偏差 < 5%  AND clamp 比例 < 10%  →  construct_degraded = False →  正常
```

降级**不阻塞验证流程**——仍会执行算子并比对输出，但结果标记"构造质量差"，提示"验证结论置信度降低，仅供参考"。

### 质量聚合

一个算子可能有多个 input，取所有 input 的**最差值**（最大 l2norm 偏差、最大 clamp 比例），写入该算子的所有 `VerifyResult`。

---

## 输出比对容差

### 判定公式

```
passed = shape_match AND (max_diff < atol) AND (max_rel_err < rtol × 100)
```

### 比对指标

| 指标 | 计算方式 | 含义 |
|------|----------|------|
| `max_diff` | `max(|NPU - CPU|)` | 逐元素最大绝对差 |
| `l2norm_diff` | `||NPU - CPU||` | L2 范数差异 |
| `mean_diff` | `mean(|NPU - CPU|)` | 平均绝对差异 |
| `max_rel_err` | `max(|NPU - CPU| / max(|CPU|, 1e-12)) × 100%` | 逐元素最大相对误差百分比 |
| `mean_rel_err` | `mean(|NPU - CPU| / max(|CPU|, 1e-12)) × 100%` | 平均相对误差百分比 |
| `shape_match` | `npu.shape == cpu.shape == csv.shape` | 输出 shape 是否一致 |

---

## 验证结果解读

### ✅ PASS

```
shape 一致 AND max_diff < atol(dtype) AND max_rel_err < rtol(dtype) × 100%
```

CPU vs NPU 结果一致，算子实现无问题，可排除嫌疑。下一轮分析可填入 §0 "已排除算子"。

### ❌ FAIL

```
shape 不一致 OR max_diff ≥ atol(dtype) OR max_rel_err ≥ rtol(dtype) × 100%
```

CPU vs NPU 结果有差异，确认问题。下一轮分析可填入 §0 "已知根因"。

常见原因：
- 算子实现 bug（NPU 和 CPU 的计算逻辑不一致）
- 数值精度差异过大（超出正常浮点累加差异量级）
- 输入构造质量差导致两边被 clamp 到不同值（可配合"构造质量差"标注判断）

### 构造质量差（降级标记）

输入 tensor 构造时 l2norm 偏差或 clamp 比例超标。**验证结论置信度降低，仅供参考**。

- 如果同时 PASS → 算子大概率没问题，但输入不够"像"真实数据
- 如果同时 FAIL → 可能是构造偏差导致的假阳性，建议检查 CSV 中 `[min, max]` 区间是否合理

### ⚠️ 未注册

算子自动注册失败（不在注册表中且三层递进推断均未命中）。无法自动验证，需手动排查。

---

## JSON 输出结构

```json
{
  "verify_params": {
    "atol": 1e-4,
    "rtol": 1e-3,
    "construct_strategy": "auto",
    "construct_l2norm_rtol": 0.05,
    "construct_clamp_ratio": 0.10
  },
  "summary": {
    "total": 5,
    "passed": 4,
    "failed": 1,
    "construct_degraded": 1
  },
  "results": [
    {
      "op_name": "Tensor.__truediv__.3.forward",
      "instance": "3",
      "direction": "forward",
      "tensor_name": "output.0",
      "shape_match": true,
      "max_diff": 2.3e-6,
      "l2norm_diff": 1.5e-5,
      "mean_diff": 1.0e-6,
      "max_rel_err": 0.0004,
      "mean_rel_err": 0.0001,
      "passed": true,
      "error": "",
      "note": "",
      "construct_l2norm_err": 0.01,
      "construct_clamp_ratio": 0.03,
      "construct_degraded": false
    }
  ]
}
```

---

## 示例

```bash
# Skill 调用方式（唯一支持的命令格式）
python <skill_dir>/scripts/verify_op.py <compare_result.csv> \
  --op-list "<候选1>,<候选2>,..." -o verify.json
```

---

## 算子注册机制

verify_op.py 内置三层递进注册：

| 层 | 方式 | 覆盖范围 |
|----|------|----------|
| 1 | `PREFIX_MAP` 表驱动映射 | 9 种 dump 前缀 → Python 模块（Tensor→torch.Tensor, Functional→torch.nn.functional, Torch→torch, NPU→torch_npu 等） |
| 2 | 逐层 `getattr` | `torch.*` 路径，大小写不敏感 |
| 3 | `eval` 兜底 | `torch.` 白名单路径，仅含 `[a-zA-Z0-9._]` |

内置算子（无需注册）：`Tensor.__truediv__`、`Tensor.__add__`、`Tensor.__mul__`、`Tensor.__sub__`、`torch.matmul`、`torch.bmm`、`torch.nn.functional.linear`、`torch.nn.functional.softmax`、`torch.nn.functional.layer_norm`。

未覆盖的算子由自动推断机制尝试解析；若三层递进均未命中，标记为"未注册"。

---

## 与 Skill 的集成

- Skill Step 8 调用 verify_op.py 时 **不传** `--atol`、`--rtol`、`--construct-*` 参数
- 所有容差使用脚本默认值（dtype 自适应，float32 fallback atol=1e-4/rtol=1e-3；construct-strategy=auto, construct-l2norm-rtol=5%, construct-clamp-ratio=10%）
- JSON 输出中的 `verify_params` 字段包含实际使用的参数，必须在验证报告中展示
- 构造质量差的条目必须标注"构造质量差"

---

## 验证未能正常完成的情况

当单算子验证无法正常完成（或结论不可靠）时，agent SHALL 使用以下标准化说明模板。这类情况 SHALL NOT 阻塞验证流程——除「环境不可用」类型外，其余情况仍应尽量执行算子并比对输出，仅在报告中注明原因。

### 原因类型与说明模板

| 原因类型 | 触发条件 | 标准化说明模板 |
|---------|---------|---------------|
| **NPU 环境不可用** | NPU 设备未授权/不可达 | "⚠️ 验证环境不可用——NPU 设备未授权/不可达，跳过单算子验证" |
| **自动注册失败** | 三层递进注册均未命中 | "⚠️ 自动注册失败——算子 `<name>` 三层递进注册均未命中，无法自动验证" |
| **混合 dtype 跳过** | 输入/输出 dtype 不一致 | "⚠️ 跳过——算子 `<name>` 输入/输出含混合 dtype，verify_op.py 不支持" |
| **构造质量差** | `construct_degraded=True` | "⚠️ 构造质量差——l2norm 偏差 X%，clamp 比例 Y%，验证结论置信度降低，仅供参考" |
| **无法重建计算图** | 无同调用序号的前向数据 | "⚠️ 无法重建——未找到同调用序号（instance `<n>`）的前向数据，反向验证不可用" |

### 处理流程

1. agent SHALL 在验证报告中识别触发了哪种原因场景
2. 使用对应标准化说明模板，填入具体数值（算子名称、偏差值、instance 号等）
3. 相关条目 SHALL 在验证报告的「未能验证的候选及原因」子节集中呈现
4. 无此类条目时，「未能验证的候选及原因」子节 SHALL NOT 出现

### 原因类型与验证结果的关系

| 原因类型 | 是否执行验证 | 是否有 PASS/FAIL 判定 | 结论置信度 |
|---------|------------|---------------------|--------|
| NPU 环境不可用 | 否 | 否 | N/A |
| 自动注册失败 | 否 | 否 | N/A |
| 混合 dtype 跳过 | 否 | 否 | N/A |
| 构造质量差 | 是 | 是（附原因说明） | 降低 |
| 无法重建计算图 | 否（仅反向） | 否 | N/A |
