---
name: quant-tuning-quantize
description: 执行模型量化。调用 MCP quantization_run 依据 Practice YAML 对模型进行量化，返回量化结果与产物路径。
license: Apache-2.0
metadata:
  version: 0.3.0
  domain: quantization
  framework: msmodelslim
  protocol: mcp
  skill_class: tool
  aliases:
    - quantizer
    - quantization-run
  trigger_intents:
    - 执行量化
    - 运行 quantization_run
    - 量化模型
  keywords:
    - quantization_run
    - quantize
    - practice.yaml
    - mcp
---

# Skill: Quant Tuning Quantize

## Overview

**解决什么**：依据 Practice YAML 配置，调用 MCP 执行模型量化。

**不解决什么**：
- 不生成/修改 Practice YAML → 见 `quant-tuning-practice-generator` Agent
- 不执行评测 → 见 `quant-tuning-quantizer` Agent
- 不做策略决策 → 见 `quantization-accuracy-tuning-orchestrator` Skill

**执行主体**：MCP `quantization_run`

---

## 协作关系

```
quantization-accuracy-tuning-orchestrator (workflow)
        │
        ▼ 调用
quant-tuning-quantize (tool)
        │
        ▼ MCP Tool
  quantization_run
        │
        ▼ 输出
  量化后的模型权重
```

---

## 执行步骤

```
┌─────────────────┐
│ 输入检查        │
│ - practice_path │
│ - model_path    │
│ - save_path     │
└────────┬────────┘
         ▼
┌─────────────────┐
│ 参数校验        │
│ 路径为 JSON 字符串│
│ (加引号)        │
└────────┬────────┘
         ▼
┌─────────────────┐
│ MCP Tool:       │
│ quantization_   │
│ run             │
└────────┬────────┘
         ▼
┌─────────────────┐
│ 结果处理        │
│ - 检查返回状态   │
│ - 记录产物路径   │
│ - 错误上报       │
└─────────────────┘
```

---

## 输入参数

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `config_path` | string | ✅ | Practice YAML 路径，**JSON 字符串格式** |
| `model_path` | string | ✅ | 原始模型路径 |
| `save_path` | string | ✅ | 量化产物保存路径 |
| `device` | string | ✅ | 设备类型，如 `npu:0` |
| `trust_remote_code` | bool | ❌ | 是否信任远程代码 |

---

## MCP 调用

### 调用方式

```json
{
  "config_path": "/path/to/practice.yaml",
  "model_path": "/path/to/model",
  "save_path": "/path/to/output",
  "device": "npu:0",
  "trust_remote_code": false
}
```

**注意**：所有路径必须是 **JSON 字符串**（加引号），禁止裸路径。

### 错误处理

| 错误类型 | 处理 |
|----------|------|
| MCP 未就绪 | 立即中止，报 "MCP 未就绪" |
| 路径不存在 | 检查路径后重试或中止 |
| 量化失败 | 报错误摘要，等待 orchestrator 决策 |
| 超时 | 按 MCP 超时处理，不上层续跑 |

---

## 输出结果

### 成功

```json
{
  "ok": true,
  "output_path": "/path/to/output/quantized_model",
  "config_id": "Qwen2-7B_W8A8_xxx",
  "duration": 123.45
}
```

### 失败

```json
{
  "ok": false,
  "error": "量化失败原因",
  "error_code": "QUANTIZATION_ERROR"
}
```

---

## 磁盘管理

- 量化产物写入 `save_path`
- 由 orchestrator 管理磁盘空间（最多保留 2 份权重）
- 本 skill 不主动清理历史产物

---

## 执行示例

### 标准调用

```python
# MCP Tool: quantization_run
{
  "config_path": "/workspace/output/round_1/practice.yaml",
  "model_path": "/models/Qwen2-7B-Instruct",
  "save_path": "/workspace/output/round_1/quantized",
  "device": "npu:0",
  "trust_remote_code": false
}
```

### 结果返回给 orchestrator

```
量化完成:
- 产物路径: /workspace/output/round_1/quantized
- 配置ID: Qwen2-7B_W8A8_Baseline_v1
- 耗时: 125.3s
```

---

## 约束

- **MCP-only**：禁止用 CLI 或脚本替代
- **路径格式**：必须是 JSON 字符串（`"/path/to"`）
- **错误即停**：MCP 报错后立即中止，不兜底续跑
- **单轮单次**：每次调用只执行一次量化

---

## 常见错误

| 错误 | 原因 | 解决 |
|------|------|------|
| `practice.yaml not found` | 配置文件不存在 | 检查 `config_path` |
| `out of memory` | 设备内存不足 | 换设备 |

若错误不在上述常见错误中或者多次解决后依然未解决，依据[错误上报](references/error_handling.md)，按照错误上报格式返回至`quant-tuning-quantizer` Agent

---

## 检查清单

- [ ] `config_path` 指向的 Practice YAML 已通过 `yaml_validation_validate`
- [ ] 所有路径是 JSON 字符串格式（加引号）
- [ ] `device` 格式正确（如 `npu:0`, `cuda:0`）
- [ ] `save_path` 磁盘空间充足
- [ ] MCP 已就绪
