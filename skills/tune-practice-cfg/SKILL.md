---
name: tune-practice-cfg
description: Use when 量化调优闭环中需要生成或修改一轮调优所需的 Practice YAML，包括敏感层分析、策略决策、写出 YAML 文件和校验。
license: Apache-2.0
metadata:
  version: 0.1.0
  domain: quantization
  framework: msmodelslim
  protocol: cli
  skill_class: tool
  aliases:
    - practice-cfg
    - tune-practice
  trigger_intents:
    - 生成量化配置
    - 修改 practice
    - 敏感层分析并生成配置
  keywords:
    - msmodelslim analyze
    - yaml_validation_validate
    - exclude
    - 敏感层
    - practice yaml
---

# 量化配置生成

## Overview

在量化调优闭环中，根据敏感层分析和上轮评测结果，**生成或修改**一轮调优所需的 Practice YAML，确保其通过校验后交付给后续 model-quantize 执行。

| 负责 | 不负责（编排层） |
|------|------------------|
| 敏感层分析（`msmodelslim analyze`） | 缓存查询（`accuracy_lookup`） |
| 策略生成/修改 Practice YAML | 量化执行（`msmodelslim quant`） |
| YAML 校验（`validate_practice_yaml.py`） | 评测执行（`run_evaluation.py`） |
| | 缓存写入（`accuracy_append`） |
| | 历史记录（`history_append`） |
| | 策略终止决策 |

## 接口

**输入**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `model_type` | `str` | 模型类型名 |
| `model_path` | `str` | 模型路径 |
| `save_path` | `str` | 工作目录，Practice YAML 写入此目录下 |
| `device` | `str` | 分析设备，如 `"npu"`、`"npu:0"`、`"gpu:0,1"` |
| `strategy` | `str` | 调优策略：`"standing_high"` 或 `"standing_high_with_experience"` |
| `calib_dataset` | `str \| None` | 可选的校准数据集覆盖值；默认值见 [敏感层分析](references/sensitive_layer_analysis.md) |
| `max_iterations` | `int` | 最大迭代轮次，由用户指定 |
| `round` | `int` | 当前调优轮次，用于生成本轮 Practice 文件名 |
| `prev_result` | `dict \| None` | 上轮评测结果（EvaluateResult 结构），首轮为 `None` |
| `anchor_practice` | `str \| None` | 当前已知最优且达标的 Practice YAML 路径（锚点） |

**产出**：`practice_path`（合法的 Practice YAML 文件路径）

**工具**：`msmodelslim analyze`（敏感层分析）、`scripts/validate_practice_yaml.py`（校验）

## 执行步骤

### 步骤总览

```
        ┌─────────────────────┐
        │ ① 读取/生成基准      │  ← 确定 schema 与静态量化边界
        │    Practice         │
        └──────────┬──────────┘
                   ▼
        ┌─────────────────────┐
        │   ② 敏感层分析       │  ← 敏感层分析只执行一次
        │(msmodelslim analyze)│
        └──────────┬──────────┘
                   │ 敏感度得分文件（各轮复用）
                   ▼
     (>>> 每轮循环 <<<)  ◄──────────────────┐
                   ▼                        │
        ┌─────────────────────────────────┐  │
        │ ③ 根据策略选择回退层              │  │
        │   + 生成/修改 Practice YAML      │  │
        └──────────┬──────────────────────┘  │
                   │                          │
                   ▼                          │
        ┌─────────────────────┐              │
        │ ④ 校验 Practice YAML │              │
        │ (validate_practice_  │              │
        │  yaml)               │              │
        └──────────┬──────────┘              │
                   │ practice_path            │
                   ▼                          │
         后续：model-quantize ──→ 下一轮 ─────┘
```

**调度优化**

- 如果你在进行敏感层分析的时候，还有其他卡闲置可用，如果敏感层分析时长较长，则你可以同步地使用其他卡拉起第一轮的量化（注意指定不同的卡，如使用ASCEND_RT_VISIBLE_DEVICES环境变量等方式）以减少串行等待时间。在量化结束后，如果测评需要使用的卡中包含正在进行敏感层分析的卡，则你**必须**等待敏感层分析任务结束后再进行测评任务。

### ① 读取或生成基准 Practice

优先从 Practice 仓库中查找与当前 `model_type` 匹配的已验证 Practice；存在多个候选时，返回候选项，由主 Agent 确认后继续。未找到时，按照 [量化配置格式](references/practice_yaml_format.md) 生成保守基准 Practice，保存为 `{save_path}/practice_base.yaml`。基准 Practice 必须在敏感层分析前确定并通过校验。

从基准 Practice 中继承：

- `apiversion`
- 目标量化处理器的 `include`
- 因模型能力或已验证经验而存在的静态 `exclude`
- 当前 schema 要求的其他静态字段，如 VLM 的 `spec.default_text`。

将静态排除项记录为 `protected_exclude`，在全部调优轮次中保持不变。每轮最终写入的 `exclude` 为 `protected_exclude ∪ tuning_exclude`；调优只能增减 `tuning_exclude`，不得删除静态排除项。

### ② 敏感层分析

按照[敏感层分析](references/sensitive_layer_analysis.md) 调用 `msmodelslim analyze layer`，获取各 Decoder Block 的量化敏感度得分。每个调优任务只执行一次，结果写入 `{save_path}/analysis_result.yaml`，供后续各轮复用。

复用已有 `analysis_result.yaml` 前，必须按敏感层分析文档验证其结构；校验通过时跳过分析，校验失败时重新执行并覆盖旧结果。

敏感层分析必须遵守基准 Practice 确定的量化边界：

- 使用与后续 Practice `spec.dataset` 一致的校准数据；
- 根据目标量化处理器的 `include` 确定分析范围；
- 不得将静态 `exclude` 中的模块作为可调回退项。

分析命令、设备绑定、指标选择、日志保存、成功判定及结果转换均以该文档为准，不在此重复定义。

仅当分析能力不可用或分析超时，且已确认模型、数据集和参数本身合法时，才可用经验规则占位。数据集、模型加载、schema 或参数错误必须立即失败返回，不得用经验规则掩盖。

---

### ③ 策略生成/修改 Practice 并写出 YAML 文件

**目的**：根据预计算的敏感度得分和当前轮次的策略需要，选择本轮回退层并确定离群值抑制策略，构造完整的 Practice YAML 内容，并**写入磁盘文件**。

**输入**：
- 敏感度得分文件 `{save_path}/analysis_result.yaml`（步骤 ② 产出，各轮复用）
- 上轮评测结果 `prev_result`（首轮为 `None`）
- 当前已知最优且达标的配置（锚点）

**具体动作**：

1. **确定本轮改动**（一次只改一两处字段，从预计算的敏感度得分中选择回退层，遵守同分同退约束）
2. **构造完整的 Practice YAML 内容**：继承基准 Practice 的 `apiversion` 和静态字段，仅修改当前策略允许的调优字段，详见 [量化配置格式](references/practice_yaml_format.md)
3. **写出文件**：将 YAML 内容写入 `{save_path}/practice_round_{N}.yaml`（N 为当前轮次），得到 `practice_path`

| 改动项 | 说明 | 对应 YAML 位置 |
|--------|------|----------------|
| 调整 `tuning_exclude` | 增减敏感层回退；最终与 `protected_exclude` 取并集 | `spec.process[].exclude` |
| 替换离群值抑制 | `iter_smooth` ↔ `flex_smooth_quant` ↔ `flex_awq_ssz` | `spec.process[].type` |
| 调整抑制强度 | 如 `flex_awq_ssz` 的 `step`、`enable_subgraph_type` | `spec.process[].qconfig.ext` |

**修改粒度**：
- **一次只改一两处字段**，避免多因素同时变化导致无法归因

**exclude 设计原则**：
- 优先覆盖敏感层排序中 **score 最高的层**
- **同分同退**：敏感度分数相同的层必须作为一个整体同时回退或同时保留
- 回退位置经验优先级：靠近输入的前若干层 > 靠近输出的后若干层 > 语义敏感子模块（部分 MLP / attention 层）
- 回退级别按层组离散化（如前 2 层、前 4 层、前 4 + 后 4 层……），便于二分搜索

**离群值抑制叠加原则**：
- 先上单一、简单的抑制（如仅 `iter_smooth`）
- 确认瓶颈后再考虑更强或组合策略
- **二分阶段抑制组合固定，只动回退刻度**；摸高阶段才允许切换抑制

> 调优策略由入参 `strategy` 决定。`"standing_high"` 详见 [standing_high 策略](references/strategy_standing_high.md)；`"standing_high_with_experience"` 详见 [standing_high_with_experience 策略](references/strategy_standing_high_with_experience.md)。

**始终保留锚点**：掉精度时可回滚到上一已知达标配置。

---

### ④ 校验 Practice YAML

**脚本调用**：

```bash
python skills/tune-practice-cfg/scripts/validate_practice_yaml.py --practice-path /path/to/practice.yaml
```

**返回**：

```json
{
    "ok": true,
    "valid": true,
    "errors": []
}
```

**校验内容**：
1. **YAML 语法**：能否正常解析
2. **Schema 校验**：是否可被 `PracticeConfig.model_validate` 通过（字段名、类型、必填项）
3. **业务规则**：如 `label` 必须是 dict 而非字符串、`type` 与字段是否匹配

**错误处理**：

| 错误类型 | 说明 | 动作 |
|----------|------|------|
| `parse_error` | YAML 语法错误 | 修正语法后重试 |
| `schema_error` | 字段缺失/类型不对 | 修正字段后重试 |
| `business_rule_error` | 业务逻辑违规 | 按提示修正后重试 |

`valid=false` 时**不可继续后续步骤**，必须修正后重新校验。

> YAML 字段名、类型、必填项等 schema 细节见 [量化配置格式](references/practice_yaml_format.md)。

---

## 产出

`practice_path`（合法的 Practice YAML 文件路径），交付给编排层进行缓存查询后，传递给 model-quantize 执行量化。

## 约束汇总

| 约束 | 说明 |
|------|------|
| ② 在首轮前调用一次 | 敏感度得分每个调优任务计算一次，各轮复用 |
| 一次只改一两处 | exclude 或离群值抑制，避免多因素同时变化 |
| 保留锚点 | 始终保留一份当前已知最优且达标的配置，掉精度可回滚 |
| 校验必过 | `valid=false` 时不可继续，必须修正后重新校验 |
| save固定 | `spec.save` 字段除非用户指定，默认情况下必须为 `ascendv1_saver` |

## 常见错误

- 回退层选择时拆分同分同退组（应整体回退或整体保留）
- 一次同时改 exclude + 抑制策略 + 校准集，无法归因
- `metadata.label` 写成字符串而非 dict
- `type` 与字段不匹配（如 `flex_awq_ssz` 缺少 `qconfig`），参见 [量化配置格式](references/practice_yaml_format.md)
- `valid=false` 仍继续后续步骤
- 命令行参数 `--device` 未使用 `npu:0` 这种格式，错误地使用了 `DeviceType.NPU`
