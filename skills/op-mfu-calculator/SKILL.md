---
name: op-mfu-calculator
description: 基于 msprof-analyze 工具的昇腾 NPU 算子 MFU 分析技能。支持三种场景：(1) 用户已有 Ascend PyTorch Profiler 采集脚本，直接修改脚本补齐采集配置并用 msprof-analyze 解析 MFU；(2) 用户想手动计算某个算子的 MFU，无需 Profiling 数据，通过算子公式表或 op-plugin 检索确定 FLOPs 后直接计算；(3) 用户需要为未注册的新算子扩展 FLOPs 公式。触发场景：算子性能分析、MFU 瓶颈定位、模型计算效率评估。
---

# 算子 MFU 分析

> **本 skill 包含三种模式：模式 A 会直接修改用户的 Profiler 脚本；模式 B 仅做计算，不修改代码；模式 C 会修改 `_flops_formulas.py` 注册新算子。**

***

## 适用范围

- **硬件平台**：昇腾 Atlas A2 / A3 系列（Ascend 910B 系列）
- **分析对象**：已注册 FLOPs 公式的 PyTorch 算子（matmul、attention、norm 等）
- **分析模式**：
  - **模式 A**：用户已有 Ascend PyTorch Profiler 采集脚本 → 直接修改脚本补齐采集配置，使用 msprof-analyze 解析
  - **模式 B**：用户想手动计算某个算子的 MFU → 通过算子公式表或 [op-plugin](https://gitcode.com/Ascend/op-plugin) 确定 FLOPs 后直接计算
  - **模式 C**：用户需要为未注册的新算子扩展 FLOPs 公式 → 查 [op-plugin](https://gitcode.com/Ascend/op-plugin)、注册到 `_flops_formulas.py`、采集验证

***

## 前置判断：选择分析模式

在看到用户的具体需求后，首先判断用户属于哪种场景：

```
用户需求？
├─ 用户已有包含 Ascend PyTorch Profiler 采集的脚本
│  → 模式 A：采集并解析 MFU
│
├─ 用户询问某个具体算子的 MFU
│  → 模式 B：直接计算该算子的 MFU
│
├─ 用户明确要扩展新算子、注册 FLOPs 公式
│  → 模式 C：扩展新算子（注册 FLOPs 公式 + 采集验证）
│
└─ 用户只问了算子名和维度（无法判断意图）
   → 询问用户：是要快速手动估算 MFU（模式 B），还是注册 FLOPs 公式并采集验证（模式 C）？
```

> **关键**：不要混淆三种模式。如果根据用户提问无法确定是模式 B 还是模式 C，**直接询问用户**，不要自行假设。
>
> **重要：无论选择哪种模式，都先按以下步骤与用户确认**：
> 1. **先简要列出三种模式**，说明各自适用场景。
> 2. **再说明你的判断**：根据用户需求，你认为适合走哪个模式及原因。
> 3. **最后请用户确认**：得到确认后再按对应模式执行。

***

## 模式 A：采集并解析 MFU

> 当用户已有包含 Ascend PyTorch Profiler 采集的脚本时走此模式。

**详细步骤请阅读：[references/mode-a-profiling.md](references/mode-a-profiling.md)**

**流程概要**：修改脚本补齐采集配置 → 运行采集 → `msprof-analyze` 解析 MFU → 分析瓶颈。

## 模式 A 完成标志

- [ ] 确认用户已有 Ascend PyTorch Profiler 采集脚本
- [ ] 已直接修改脚本补齐采集配置，并已向用户列出每一项改动
- [ ] 运行前已检查输出目录，如有旧数据已先询问用户是否清空
- [ ] 已运行程序，`on_trace_ready` 输出目录已生成 Profiling 数据
- [ ] 已运行 `msprof-analyze --agent -m operator_mfu -d <on_trace_ready输出目录>` 命令
- [ ] 已读取并解读输出结果（kernel 级 / module 级 MFU）
- [ ] 已给出 MFU 瓶颈分析和优化建议

***

## 模式 B：单算子 FLOPs / MFU 计算

> 当用户想手动计算某个算子的 MFU、无需 Profiling 数据时走此模式。

**详细步骤请阅读：[references/mode-b-cal.md](references/mode-b-cal.md)**

**流程概要**：确定 FLOPs → 计算 MFU → 按标准格式回答。

## 模式 B 完成标志

**无耗时场景（仅 FLOPs）**：

- [ ] 已确认算子类型和维度信息
- [ ] 已通过算子公式表或 [op-plugin](https://gitcode.com/Ascend/op-plugin) 确定 FLOPs 公式
- [ ] 已给出 FLOPs 计算公式和最终数值

**有耗时场景（MFU）**：

- [ ] 已确认算子类型、张量维度、执行耗时、硬件峰值算力
- [ ] 已计算 FLOPs、Achieved TFLOPs/s、MFU
- [ ] 已给出结果分析和优化建议

***

## 模式 C：扩展新的算子（注册 FLOPs 公式）

> 当用户明确需要为新算子注册 FLOPs 公式并采集验证时走此模式。

**详细步骤请阅读：[references/mode-c-extend-operator.md](references/mode-c-extend-operator.md)**

**流程概要**：确定 FLOPs 公式并注册 → 采集验证（打点落盘 → `msprof-analyze` 比对）。若已注册则跳过注册步骤。

## 模式 C 完成标志

- [ ] 已确认该算子未注册到 `_flops_formulas.py`，需要扩展
- [ ] 已通过 `_flops_formulas.py` 文件或 [op-plugin](https://gitcode.com/Ascend/op-plugin) 确定 FLOPs 公式并注册
- [ ] 已展示修改的文件路径和修改内容
- [ ] 已运行程序采集 Profiling 数据
- [ ] 已通过 SQL 查询确认新算子的 FLOPs 打点落盘
- [ ] 已运行 msprof-analyze 并比对 `flops` 字段与手动计算结果一致