# 精度异常定位执行 subagent 字段定义

协议总则见 [orchestrator subagent_io_protocol.md](../../quantization-accuracy-tuning-orchestrator/references/subagent_io_protocol.md)。本文档面向**编排层（Quantizer 主会话）**，定义依次委派 3 个执行 subagent 时的 `input` 与回传 `output`。

> 本 Skill 为与调优并列的端到端流程。编排层加载 `fp-vs-quant-accuracy-analysis` 后，收集并回显确认用户输入，然后**按顺序依次委派**以下 3 个 subagent，每个完成后再委派下一个；全部完成后由编排层汇总结论并输出报告。

| subagent | 承载步骤 | 职责 |
|----------|---------|------|
| `quant-tuning-accuracy-quantizer` | 步骤 0 | 调试模式复现量化，产出旋转/抑制中间量 |
| `quant-tuning-accuracy-collector` | 步骤 1-3 | 生成 probe 配置、逆变换准备、拉 vllm 服务采集两侧 dump |
| `quant-tuning-accuracy-comparator` | 步骤 4-6 | 生成后处理配置、msprobe compare 比对、定位异常模块 |

---

## ① quant-tuning-accuracy-quantizer（步骤 0：复现量化）

### 委派 input

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `fp_model_path` | string | ✓ | 原始浮点模型路径 |
| `quant_model_path` | string | ✓ | 现有量化权重路径（自主获取 `{model_type}_best_practice.yaml` 与判断 `optional/`、`debug_info/` 产物现状） |
| `device` | string | ⬜ | 复现量化设备，默认 `npu:0` |
| `save_path` | string | ✓ | 量化产物输出目录（通常为 `<workdir>/quant_model`） |
| `workdir` | string | ✓ | 中间产物目录 |

复现命令由 subagent 自主构造：以 `quant_model_path` 下自主获取的 `{model_type}_best_practice.yaml` 为 `--config-path` + `--debug`（`model_type` 从 yaml 或模型路径推断），用户无需提供量化命令。

### 回传 output

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `quant_model_path` | string | ✓ | 量化产物目录（含 `quant_model_weights.safetensors` 等） |
| `reproduced` | bool | ✓ | 本次是否实际执行了复现（false 表示跳过） |
| `commands` | object[] | ✓ | 含 `name: quantize` 的完整量化命令 |

### 示例

```msagent-io v1
{
  "protocol": "msagent.subagent_io",
  "subagent_type": "quant-tuning-accuracy-quantizer",
  "status": "ok",
  "output": {
    "quant_model_path": "/path/to/accuracy_analysis/quant_model",
    "reproduced": true,
    "commands": [
      { "name": "quantize", "command": "python3 .../run_quantization.py --model-type Qwen3-8B ..." }
    ]
  }
}
```

---

## ② quant-tuning-accuracy-collector（步骤 1-3：probe 配置 + 逆变换准备 + dump 采集）

### 委派 input

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `fp_model_path` | string | ✓ | 浮点权重模型路径 |
| `quant_model_path` | string | ✓ | 量化权重模型路径（步骤 0 产物或用户已有产物） |
| `vllm_serve_command` | string | ✓ | 用户 vllm serve 启动命令（TP、max-model-len 等），采集时追加 `--enforce-eager` 与 `dump_config_path` |
| `model_adapter_path` | string | QuaRot 必需 | msmodelslim 模型适配器文件路径（含 `get_rotate_map`），定义旋转作用范围 |
| `model_structure_path` | string | QuaRot 必需 | 模型结构文件路径（transformers `modeling_*.py`），确认模块层级与激活流向 |
| `workdir` | string | ✓ | 中间产物目录 |
| `request` | object | ⬜ | 触发 dump 的推理请求 `{ "prompt": "...", "max_tokens": N }`，默认 `"Hello"` / 1 |

### 回传 output

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `dump_fp_path` | string | ✓ | 浮点侧 dump 目录（`<workdir>/dump_fp/step0/rank0/`） |
| `dump_quant_path` | string | ✓ | 量化侧 dump 目录（`<workdir>/dump_quant/step0/rank0/`） |
| `rotation_npy_path` | string | QuaRot 时必填 | 旋转矩阵 `rotation.npy`（含 rotate_map.json 路径） |
| `suppression_scales_dir` | string | NonFusion 时必填 | `diag(s)` npy 目录 + `suppression_index.json` |
| `fusion_scales_dir` | string | Fusion 时必填 | `diag(s)` npy 目录 + `fusion_index.json` |
| `commands` | object[] | ✓ | 含 `name: collect_dump` 的 vllm serve + 请求命令 |

### 示例

```msagent-io v1
{
  "protocol": "msagent.subagent_io",
  "subagent_type": "quant-tuning-accuracy-collector",
  "status": "ok",
  "output": {
    "dump_fp_path": "/path/to/accuracy_analysis/dump_fp/step0/rank0",
    "dump_quant_path": "/path/to/accuracy_analysis/dump_quant/step0/rank0",
    "rotation_npy_path": "/path/to/accuracy_analysis/rotation.npy",
    "suppression_scales_dir": "/path/to/accuracy_analysis/suppression_scales",
    "commands": [
      { "name": "collect_dump", "command": "vllm serve ... --enforce-eager --additional-config '{\"dump_config_path\": ...}'" }
    ]
  }
}
```

---

## ③ quant-tuning-accuracy-comparator（步骤 4-6：后处理配置 + compare + 定位）

### 委派 input

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `dump_fp_path` | string | ✓ | 浮点侧 dump 目录 |
| `dump_quant_path` | string | ✓ | 量化侧 dump 目录 |
| `rotation_npy_path` | string | QuaRot 时必填 | 旋转矩阵 npy 与 rotate_map.json 路径 |
| `suppression_scales_dir` | string | NonFusion 时必填 | `diag(s)` npy 目录 |
| `fusion_scales_dir` | string | Fusion 时必填 | `diag(s)` npy 目录 |
| `workdir` | string | ✓ | 中间产物目录（后处理配置、compare 结果输出于此） |

### 回传 output

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `anomaly_modules` | object[] | ✓ | **所有** COS < 阈值（默认 0.95）的异常激活，按执行顺序：`[{ "module": str, "target": "input"\|"output", "cos_similarity": number, "input_md5_match": bool, "output_md5_match": bool, "output_md5_quant": str, "output_md5_fp": str }]`；无异常激活为 `[]` |
| `threshold` | number | ✓ | 本次使用的 COS 阈值（默认 0.95） |
| `evidence` | object | ✓ | `{ "compare_result_dir": str, "dump_fp_path": str, "dump_quant_path": str }` |
| `inverse_transforms` | object | ✓ | `{ "rotation": { "right": int, "left": int }, "suppression": int }`；未配置逆变换用空对象 |
| `commands` | object[] | ✓ | 含 `name: compare` 的 msprobe compare 命令 |

### 示例

```msagent-io v1
{
  "protocol": "msagent.subagent_io",
  "subagent_type": "quant-tuning-accuracy-comparator",
  "status": "ok",
  "output": {
    "anomaly_modules": [
      {
        "module": "model.language_model.layers.12.mlp.down_proj.linear",
        "target": "output",
        "cos_similarity": 0.876543,
        "input_md5_match": true,
        "output_md5_match": false,
        "output_md5_quant": "a1b2c3...",
        "output_md5_fp": "d4e5f6..."
      },
      {
        "module": "model.language_model.layers.13.mlp.gate_proj",
        "target": "output",
        "cos_similarity": 0.921,
        "input_md5_match": false,
        "output_md5_match": false,
        "output_md5_quant": "e7f8a1...",
        "output_md5_fp": "b2c3d4..."
      }
    ],
    "threshold": 0.95,
    "evidence": {
      "compare_result_dir": "/path/to/accuracy_analysis/compare_result",
      "dump_fp_path": "/path/to/accuracy_analysis/dump_fp",
      "dump_quant_path": "/path/to/accuracy_analysis/dump_quant"
    },
    "inverse_transforms": {
      "rotation": { "right": 56, "left": 28 },
      "suppression": 84
    },
    "commands": [
      { "name": "compare", "command": "msprobe compare -tp ... -gp ... -o ... -c cos,md5,max_diff" }
    ]
  }
}
```

---

## 通用约束

- 失败：`status: "failed"` + `error: { "code", "message" }`，不填 `output`。`error.code` 优先：`VALIDATION_ERROR`、`MODEL_LOAD_ERROR`、`DUMP_ERROR`、`POSTPROCESS_ERROR`、`UNKNOWN_ERROR`
- 编排层**不得**伪造 subagent 的 `output`；结论须来自回传的 msagent-io 块
- 每个 subagent 尝试同一问题 5 次未解决时，上报编排层向用户确认
