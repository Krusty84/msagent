---
name: quant-tuning-evaluate
description: 执行模型测评。通过 scripts/run_evaluation.py 依据 Evaluation YAML 对量化模型进行评测。
license: Apache-2.0
metadata:
  version: 0.3.1
  domain: quantization
  framework: msmodelslim
  protocol: script
  skill_class: tool
  aliases:
    - evaluator
    - evaluation-run
  trigger_intents:
    - 执行测评
    - 运行 run_evaluation
    - 评测模型
  keywords:
    - run_evaluation
    - evaluate
    - aisbench
    - service_oriented
---

# Skill: Quant Tuning Evaluate

## Overview

**解决什么**：依据 Evaluation YAML 配置，通过 `scripts/run_evaluation.py` 对量化模型进行评测。

**不解决什么**：
- 不生成/修改 Evaluation YAML → 见 `quant-tuning-evaluation-generator` Agent
- 不执行量化 → 见 `quant-tuning-quantizer` Agent
- 不做策略决策 → 见 `quantization-accuracy-tuning-orchestrator` Skill

**执行主体**：`scripts/run_evaluation.py`

---

## 协作关系

```
quantization-accuracy-tuning-orchestrator (workflow)
        │
        ▼ 调用
quant-tuning-evaluate (tool)
        │
        ▼ Script
  run_evaluation.py
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
│ execute:        │
│ run_evaluation  │
│ .py             │
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
| `quant_model_path` | string | ✅ | 量化后模型路径 |
| `evaluate_id` | string | ✅ | 本轮评测 ID |
| `evaluate_config_path` | string | ✅ | Evaluation YAML 路径（编排层常称 `config_path`） |
| `save_path` | string | ✅ | 评测工作目录 |
| `device` | string | ❌ | 设备类型，默认 `npu` |
| `device_indices` | list[int] | ❌ | 设备索引列表，如 `[0,1]` |

---

## 脚本调用

```bash
python skills/quant-tuning-evaluate/scripts/run_evaluation.py \
  --quant-model-path "${quant_model_path}" \
  --evaluate-id "${evaluate_id}" \
  --evaluate-config-path "${evaluate_config_path}" \
  --save-path "${save_path}" \
  --device "${device}" \
  --device-indices "${device_indices}"
```

执行前将 `device_indices` 列表转换为逗号分隔字符串，例如 `[0, 1]` 转换为 `0,1`。

### 错误处理

| 错误类型 | 处理 |
|----------|------|
| msmodelslim 未安装 | 按 prepare_environment.md 安装 |
| 推理服务启动失败 | 检查端口占用、设备可用性 |
| 评测超时 | 检查 `aisbench.timeout` 配置 |
| 精度不达标 | 正常返回结果，由 orchestrator 决策 |

---

## 脚本原始输出

### 成功

```json
{
  "ok": true,
  "evaluate_result": {
    "accuracies": [
      {
        "dataset": "gsm8k",
        "accuracy": "83.5"
      }
    ],
    "expectations": [
      {
        "dataset": "gsm8k",
        "target": "83.0",
        "tolerance": "1.0"
      }
    ],
    "is_satisfied": true
  }
}
```

### 失败

```json
{
  "ok": false,
  "error": "推理服务启动失败"
}
```

## 执行流程

### 1. 服务启动

调用 `scripts/run_evaluation.py`（`execute`）

### 2. 结果解析

脚本返回的 `evaluate_result` 字段：

| 字段 | 说明 |
|------|------|
| `accuracies[].dataset` | 数据集名称 |
| `accuracies[].accuracy` | 实际精度（百分比）|
| `expectations[].dataset` | 数据集名称 |
| `expectations[].target` | 目标精度 |
| `expectations[].tolerance` | 允许的精度偏差 |
| `is_satisfied` | 所有已要求的数据集是否达标 |

### 3. Agent 回传

脚本返回 `ok: true` 时，`quant-tuning-evaluator` Agent 将完整的 `evaluate_result` 原样放入 `output.evaluate_result`，并按 Agent 输出协议补充 `commands` 和 `msagent-io` 包装，不得改名、删减或重新计算其中字段：

```json
{
  "output": {
    "evaluate_result": {
      "accuracies": [
        {
          "dataset": "gsm8k",
          "accuracy": "83.5"
        }
      ],
      "expectations": [
        {
          "dataset": "gsm8k",
          "target": "83.0",
          "tolerance": "1.0"
        }
      ],
      "is_satisfied": true
    },
    "commands": [
      {
        "name": "inference_service",
        "command": "<本次实际执行的完整推理服务命令>"
      },
      {
        "name": "evaluation",
        "command": "<本次实际执行的完整评测命令>"
      }
    ]
  }
}
```

脚本返回 `ok: false` 时，转换为 Agent 的失败协议，不填 `output`。

---

## 约束

- **Script-only**：禁止用裸 CLI 替代 `run_evaluation.py`
- **路径格式**：必须是 JSON 字符串
- **单轮单次**：每次调用只执行一次完整评测
- **服务生命周期**：由脚本内部评测服务管理。如果你需要测多个数据集，请你在测完所有数据集后再关闭服务化，**避免**重复多次拉起。服务化测评运行时长可能较长，超过 timeout 3600s，**务必避免**在测评的中途关闭服务化和测评，你应该等待测评完成，必要时可以通过看日志（如vllm_server.log）最新的消息时间来确认测评任务是否还活跃。

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
- [ ] YAML 中的 `ASCEND_RT_VISIBLE_DEVICES` 与 `device_indices` 一致
- [ ] `device_indices` 长度与 `tensor-parallel-size` 对齐
- [ ] 目标端口未被占用
- [ ] NPU/GPU 设备可用
- [ ] msmodelslim 已安装
