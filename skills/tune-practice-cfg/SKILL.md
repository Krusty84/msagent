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
| `base_practice_path` | `str \| None` | 与当前模型匹配的基准 Practice；存在时继承其 schema、处理器范围与静态排除项 |
| `device` | `str` | 分析设备类型：`"npu"` 或 `"cpu"`；分析命令不接受设备索引 |
| `selected_npu_ids` | `list[int]` | NPU 场景的物理卡白名单；所有相关命令必须据此设置 `ASCEND_RT_VISIBLE_DEVICES` |
| `strategy` | `str` | 搜索算法：`"standing_high"` 或 `"standing_high_with_experience"`；LLM 与 VLM 共用 |
| `calib_dataset` | `str \| None` | 可选覆盖值；为 `None` 时，`modelslim_v1` 默认使用 `mix_calib.jsonl`，`multimodal_vlm_modelslim_v1` 默认使用 `calibImages` |
| `max_iterations` | `int` | 最大迭代轮次，由用户指定 |
| `prev_result` | `dict \| None` | 上轮评测结果（EvaluateResult 结构），首轮为 `None` |
| `anchor_practice` | `str \| None` | 当前已知最优且达标的 Practice YAML 路径（锚点） |

**产出**：`practice_path`（合法的 Practice YAML 文件路径）

**工具**：`msmodelslim analyze`（敏感层分析）、`scripts/validate_practice_yaml.py`（校验）

**统一流程不变量**：

- LLM 与 VLM 使用同一套敏感层分析、二分搜索、量化和评测闭环。
- schema 与少量专属字段由基准 Practice 的 `apiversion` 决定；生成结果必须继承该值。
- 基准 Practice 中已有的 `include` 和静态 `exclude` 是量化能力边界。调优只能在该边界内增加或减少“调优排除项”，不得删除静态排除项。
- 没有匹配的基准 Practice 时，先生成并保存保守基准 Practice，再进行敏感层分析；不得在分析完成后才决定量化范围。

## 执行步骤

### 步骤总览

```
        ┌─────────────────────┐
        │ ① 读取/生成基准      │  ← 确定 schema 与静态量化边界
        │    Practice          │
        └──────────┬──────────┘
                   ▼
        ┌─────────────────────┐
        │   ② 敏感层分析       │  ← 调优任务开始前执行一次
        │ (msmodelslim analyze)│
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

- 如果你在进行敏感层分析的时候，还有其他已确认的 `selected_npu_ids` 范围内的设备，如果敏感层分析时长较长，则你可以同步地使用其他卡拉起第一轮的量化（注意指定不同的卡，如使用ASCEND_RT_VISIBLE_DEVICES环境变量等方式）以减少串行等待时间。在量化结束后，如果测评需要使用的卡中包含正在进行敏感层分析的卡，则你**必须**等待敏感层分析任务结束后再进行测评任务。

### ① 读取或生成基准 Practice

优先读取与当前 `model_type` 和量化方案匹配的 `base_practice_path`。若未提供，则先生成 `{save_path}/practice_base.yaml`。从基准 Practice 提取：

- `apiversion`
- 目标量化处理器的 `include`
- 因模型能力、视觉/投影结构或已验证经验而存在的静态 `exclude`
- VLM 的 `spec.default_text`

将静态排除项记录为 `protected_exclude`。它在全部调优轮次中保持不变；每轮最终 `exclude = protected_exclude ∪ tuning_exclude`。

### ② 敏感层分析

通过 `execute` 调用 **msmodelslim CLI** 获取当前模型各 Decoder Block 的量化敏感度得分（score 越高越敏感）。**每个调优任务调用一次**，后续各轮复用该得分结果。当前服务支持 `mse_layer_wise` 和 `mse_model_wise`，默认使用 `mse_layer_wise`。

若当前调优任务的 `{save_path}/analysis_result.yaml` 已存在，先按 [敏感层分析](references/sensitive_layer_analysis.md) 校验结果结构；校验通过后跳过本步骤并复用，校验失败则重新分析并覆盖旧结果。

分析命令、参数构造、日志保存、成功判定和结果转换统一按 [敏感层分析](references/sensitive_layer_analysis.md) 执行；该 reference 是这些细节的唯一来源。分析数据集必须与当前生成 Practice 的 `spec.dataset` 一致，分析候选范围不得越过基准 Practice 定义的量化能力边界。

仅当分析能力不可用或分析超时，且已确认模型、数据集和参数本身合法时，才可用经验规则占位，并在结果中记录 `source: heuristic` 与具体原因。数据集、模型加载、schema 或参数错误必须立即失败返回，不得用经验规则掩盖。

> 完整命令、参数说明、metrics 选项与分析结果结构见 [敏感层分析](references/sensitive_layer_analysis.md)。

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

> LLM 与 VLM 共用由 `strategy` 决定的搜索算法，schema 差异由基准 Practice 的 `apiversion` 承载。`"standing_high"` 详见 [standing_high 策略](references/strategy_standing_high.md)；`"standing_high_with_experience"` 详见 [standing_high_with_experience 策略](references/strategy_standing_high_with_experience.md)。

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
- 命令行参数 `--device` 使用了 Python 枚举表达 `DeviceType.NPU`，而不是 CLI 字符串 `npu`
