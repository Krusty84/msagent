---
name: msmodelslim-ep-parallel-adaptation
description: |
  为 MoE 模型提供 EP（Expert Parallel）多卡并行适配：确认 MoE 架构 → 检查 EP 是否就绪 →
  未就绪时完成 EP 代码改造（专家按 rank 分片构造、权重按 rank 加载、Smooth/QuaRot/LN fuse 等量化映射本地化）→
  用真实多卡日志 `[EP_CHECK]` 验证结构分片，再用 `[EP_ACT_GATE]`（单卡 vs 多卡激活值余弦相似度 + 幅度比）验证数值一致，
  最终回传 `EP_ADAPT_RESULT=PASS`。
  本 Skill 只保证「后续调优全程以 EP 并行进行」，不承担量化、测评迭代、结构化回退与最终交付；
  调优主流程由 quantization-accuracy-tuning-orchestrator 承接。
license: Apache-2.0
metadata:
  version: 0.1.0
  domain: quantization
  framework: msmodelslim
  protocol: cli
  skill_class: tool
  aliases:
    - ep-parallel-adaptation
    - moe-ep-adaptation
    - ep-check
  trigger_intents:
    - EP 并行适配
    - 多卡 EP 量化适配
    - MoE 专家分片适配
    - EP 就绪检查
  keywords:
    - EP 适配
    - Expert Parallel
    - MoE 专家分片
    - EP_CHECK
    - EP_ACT_GATE
    - 多卡并行
    - 激活值余弦相似度门禁
---

# EP 并行适配 Skill（适配层）

## 概述

本 Skill 只做一件事：**把一个 MoE 模型改造成可被多卡 EP（Expert Parallel）并行运行的状态**，
保证后续量化调优的每一步都真实地跑在 EP 并行上，而不是退化为单卡或全量专家。

| 维度 | 本 Skill 负责 | 不属于本 Skill |
|------|--------------|--------------|
| 范围 | MoE 检查、EP 就绪检查、EP 代码改造、`[EP_CHECK]` 验证 | 量化、测评迭代、结构化回退、最终交付 |
| 产物 | EP 适配完成证据（`EP_ADAPT_RESULT=PASS`） | Practice YAML、量化权重、测评结果 |

调优主流程（环境准备 / 模型准备 / 量化配置调优 / 结果输出）始终在
`quantization-accuracy-tuning-orchestrator` 内，本 Skill 只是其中量化前的适配环节。

## 触发条件

- 用户要求对 MoE 模型做量化调优，且给出了多卡（≥2 卡）设备；
- 目标模型为带 routed experts 的 MoE 架构；
- 运行环境支持多卡分布式（Ascend NPU / HCCL，至少 2 卡）。

## 决策树

```
被 orchestrator 委派 EP 适配
  │
  ├── 第 1 步：MoE 模型检查
  │     ├── 无 routed experts → 回传 requires_ep=false，无需 EP，返回单卡/DP 流程
  │     └── 是 MoE → 继续
  │
  ├── 第 2 步：EP 就绪检查
  │     ├── 已就绪 → 直接进入第 3 步验证
  │     └── 未就绪 → 完成 EP 代码改造（见 references/ep_implementation_guide.md）
  │
  └── 第 3 步：EP 验证
        ├── 3a 结构门禁：多卡日志含 `[EP_CHECK]` 且 Check 1~6 全部通过
        ├── 3b 数值门禁：单卡 vs 多卡激活值余弦相似度 + 幅度比（`[EP_ACT_GATE]`）
        │     ├── 结构 FAIL → 直接 EP_ADAPT_RESULT=FAIL（不跑数值门禁）
        │     └── 结构 PASS + 数值 PASS → EP_ADAPT_RESULT=PASS
        └── 数值 FAIL → EP_ADAPT_RESULT=FAIL，回传 first_diverged_layer
```

## 职责边界

- **做**：MoE 检查、EP 就绪检查、EP 代码改造、`[EP_CHECK]` 验证，回传 `EP_ADAPT_RESULT`（与 `requires_ep`）。
- **不做**：不生成/修改 Practice YAML、不执行 `msmodelslim quant`、不测评、不做结构化回退、不做合规交付与磁盘管理。
- 适配完成后把结果回传给 orchestrator，由其继续后续量化调优；本 Skill 不接管调优主流程。

## 工作流参考

| 步骤 | 参考文件 | 说明 |
|------|---------|------|
| 第 1 步：MoE 模型检查 | `references/ep_checklist.md` | 确认 MoE 架构与专家参数 |
| 第 2 步：EP 就绪检查与改造 | `references/ep_implementation_guide.md` | 专家分片、权重按 rank 加载、mapping 本地化 |
| 第 3 步：EP 验证 | `references/ep_checklist.md` | `[EP_CHECK]` 结构硬检查 + `[EP_ACT_GATE]` 数值门禁 |

## 参考资料

| 文件 | 说明 |
|------|------|
| [EP 验收检查清单](references/ep_checklist.md) | EP 适配硬检查（EP Check 1~6）+ 数值门禁（EP Check 7）与交付验收 |
| [EP 实施指南](references/ep_implementation_guide.md) | 真实 EP 代码改造参考（专家分片、权重加载、mapping 本地化） |
| [量化映射适配指南](references/ep_quant_mapping_guide.md) | Smooth/QuaRot/LN fuse 等量化映射的 EP 本地化 |
| [EP 激活值数值门禁](references/ep_activation_gate.md) | 单卡 vs 多卡激活值余弦相似度 + 幅度比（EP Check 7） |