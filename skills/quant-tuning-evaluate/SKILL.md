---
name: quant-tuning-evaluate
description: 执行模型测评。调用 MCP evaluation_run 依据 Evaluation YAML 对量化模型进行评测，返回评测结果。
license: Apache-2.0
metadata:
  version: 0.3.0
  domain: quantization
  framework: msmodelslim
  protocol: mcp
  skill_class: tool
  aliases:
    - evaluator
    - evaluation-run
  trigger_intents:
    - 执行测评
    - 运行 evaluation_run
    - 评测模型
  keywords:
    - evaluation_run
    - evaluate
    - aisbench
    - service_oriented
---

# Skill: Quant Tuning Evaluate

## Overview

**解决什么**：依据 Evaluation YAML 配置，调用 MCP 对量化模型进行评测。

**不解决什么**：
- 不生成/修改 Evaluation YAML → 见 `quant-tuning-evaluation-generator` Agent
- 不执行量化 → 见 `quant-tuning-quantizer` Agent
- 不做策略决策 → 见 `quantization-accuracy-tuning-orchestrator` Skill

**执行主体**：MCP `evaluation_run`

---

## 协作关系

```
quantization-accuracy-tuning-orchestrator (workflow)
        │
        ▼ 调用
quant-tuning-evaluate (tool)
        │
        ▼ MCP Tool
  evaluation_run
        │
        ▼ 输出
  评测结果 (精度分数)
```

---

## 执行步骤

```
┌─────────────────┐
│ 输入检查        │
│ - config_path   │
│ (Evaluation YAML)│
└────────┬────────┘
         ▼
┌─────────────────┐
│ 服务启动检查    │
│ - 检查 vLLM     │
│   是否就绪      │
└────────┬────────┘
         ▼
┌─────────────────┐
│ MCP Tool:       │
│ evaluation_run  │
│ (启动推理服务   │
│  + 执行评测)    │
└────────┬────────┘
         ▼
┌─────────────────┐
│ 结果处理        │
│ - 解析精度分数   │
│ - 检查是否达标   │
│ - 错误上报       │
└─────────────────┘
```

---

## 输入参数

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `config_path` | string | ✅ | Evaluation YAML 路径，**JSON 字符串格式** |
| `device` | string | ✅ | 设备类型，如 `npu` |
| `device_indices` | list[int] | ✅ | 设备索引列表，如 `[0,1]` |

---

## MCP 调用

### 调用方式

```json
{
  "config_path": "/path/to/evaluate.yaml",
  "device": "npu",
  "device_indices": [0, 1]
}
```

**注意**：`device_indices` 与 `inference_engine.args.tensor-parallel-size` 对齐。

### 错误处理

| 错误类型 | 处理 |
|----------|------|
| MCP 未就绪 | 立即中止，报 "MCP 未就绪" |
| 推理服务启动失败 | 检查端口占用、设备可用性 |
| 评测超时 | 检查 `aisbench.timeout` 配置 |
| 精度不达标 | 正常返回结果，由 orchestrator 决策 |

---

## 输出结果

### 成功

```json
{
  "ok": true,
  "results": {
    "gsm8k": {
      "score": 83.5,
      "target": 83.0,
      "passed": true
    },
    "aime25": {
      "score": 52.0,
      "target": 50.0,
      "passed": true
    }
  },
  "overall_passed": true,
  "duration": 1800.5
}
```

### 失败

```json
{
  "ok": false,
  "error": "推理服务启动失败",
  "error_code": "INFERENCE_ERROR",
  "partial_results": {}
}
```

## 执行流程

### 1. 服务启动

调用MCP `evaluation_run`

### 2. 结果解析

返回每个数据集的分数：

| 字段 | 说明 |
|------|------|
| `score` | 实际精度（百分比）|
| `target` | 目标精度 |
| `passed` | 是否达标（score >= target - tolerance）|
| `overall_passed` | 所有数据集是否都达标 |

---

## 执行示例

### 标准调用

```python
# MCP Tool: evaluation_run
{
  "config_path": "/workspace/output/round_1/evaluate.yaml",
  "device": "npu",
  "device_indices": [0, 1]
}
```

### 结果返回给 orchestrator

```
评测完成:
- gsm8k: 83.5% (目标: 83.0%) ✅
- aime25: 52.0% (目标: 50.0%) ✅
- 总体: 达标
- 耗时: 1800.5s
```

---

## 约束

- **MCP-only**：禁止用 CLI 或脚本替代
- **路径格式**：必须是 JSON 字符串
- **设备对齐**：`device_indices` 与 `tensor-parallel-size` 对齐
- **单轮单次**：每次调用只执行一次完整评测
- **服务生命周期**：由 MCP 内部管理，本 skill 不直接操作 vLLM

---

## 常见错误

| 错误 | 原因 | 解决 |
|------|------|------|
| `port already in use` | 端口被占用 | 更换端口或等待释放 |
| `HCCL init failed` | NPU 通信失败 | 检查 `device_indices` 和设备状态 |
| `evaluate.yaml not found` | 配置文件不存在 | 检查 `config_path` |
| `out of memory` | 设备内存不足 | 换设备 |

若错误不在上述常见错误中或者多次解决后依然未解决，依据[错误上报](references/error_handling.md)，按照错误上报格式返回至`quant-tuning-evaluator` Agent

---

## 检查清单

- [ ] `config_path` 指向的 Evaluation YAML 格式正确
- [ ] `device` 与 `device_indices` 匹配
- [ ] `device_indices` 长度与 `tensor-parallel-size` 对齐
- [ ] 目标端口未被占用
- [ ] NPU/GPU 设备可用
- [ ] MCP 已就绪
