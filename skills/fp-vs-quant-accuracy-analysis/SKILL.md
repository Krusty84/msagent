---
name: fp-vs-quant-accuracy-analysis
description: 端到端量化精度异常定位。对比浮点权重与量化权重推理的 module 级激活值 dump，定位量化导致的精度异常模块。支持 QuaRot 旋转和 SmoothQuant 抑制（NonFusion / Fusion）场景的逆变换。与量化精度调优（quantization-accuracy-tuning-orchestrator）并列，按用户意图路由。
license: Apache-2.0
metadata:
  version: 0.5.0
  domain: accuracy
  framework: msprobe
  protocol: mixed
  skill_class: workflow
  aliases:
    - fp-quant-analysis
    - precision-anomaly-location
  trigger_intents:
    - 定位量化精度异常
    - 精度异常定位
    - 定位异常模块
    - 浮点 vs 量化对比
    - QuaRot 逆操作
    - 旋转抑制逆变换
  keywords:
    - 浮点量化对比
    - 旋转逆变换
    - 抑制逆变换
    - module粒度
    - W8A8
    - msprobe
---

# Skill: 端到端量化精度异常定位

## 端到端精度异常定位功能

端到端精度异常定位包括**用户输入**、**环境与产物准备**、**dump 采集**、**逆变换后处理**和**结果输出**环节，适用于量化推理出现精度异常、或算子/框架组图异常导致调优无法继续的场景。流程对比浮点权重与量化权重推理的 module 级激活值 dump，量化侧先做逆变换（QuaRot 逆旋转 / SmoothQuant 逆抑制）还原到浮点数值空间，定位首个"输入一致、输出不一致"的异常模块。

**与量化精度调优并列**：本 Skill 与 `quantization-accuracy-tuning-orchestrator` 都是 Quantizer 主会话加载的端到端流程，按用户意图二选一路由，一次任务只走其中一条。

**设计原理与数学推导**：见 [DESIGN.md](DESIGN.md)。

## 本Skill适用范围

**适用场景**：
- 量化推理出现精度异常，需定位到具体 module（本 Skill 的主场景）
- 算子/框架组图异常导致量化精度调优无法继续，需要先定位异常模块
- W8A8 + QuaRot + SmoothQuant 等量化方案的精度排查

**不适用场景**：
- 量化推理精度不达标但模型可运行、无单点异常迹象 → 走 `quantization-accuracy-tuning-orchestrator` 调优
- 在线 QuaRot（旋转矩阵分散保存在各 Linear 权重文件）
- 非正交旋转矩阵（本 Skill 假设 R 正交，`R^T = R^{-1}`）
- 非 vllm 服务化场景

## 整体设定

现在你是一个**量化精度异常定位编排者**（编排层），负责对比浮点与量化权重推理的 dump 数据，定位量化导致的精度异常模块。你负责：

- **按什么顺序**委派 3 个执行层 subagent 完成本 Skill 的步骤（见下节"执行委派"）
- 收集用户输入并回显确认、汇总各 subagent 回传的结论并输出报告
- 何时跳过可选步骤（如产物齐全时跳过复现量化）

你**不可以**：
- 仅凭猜测下结论，必须基于真实 dump 数据
- 在用户未提供明确路径时自行搜索（ls/glob/递归搜索）
- 修改业务/框架源码或以任何形式重构
- 代替执行层 subagent 完成 dump 采集、compare 比对等重型步骤

## 执行委派

本 Skill 的步骤由 3 个执行层 subagent 承载，编排层**按顺序依次委派**，每个完成后再委派下一个：

| 顺序 | subagent | 承载步骤 | 职责 |
|------|----------|---------|------|
| 1 | `quant-tuning-accuracy-quantizer` | 步骤 0 | 读 `{model_type}_best_practice.yaml` 判断是否需复现；需要时调试模式复现，产出旋转/抑制中间量 |
| 2 | `quant-tuning-accuracy-collector` | 步骤 1-3 | 生成 probe 配置、逆变换准备、拉 vllm 服务采集两侧 dump |
| 3 | `quant-tuning-accuracy-comparator` | 步骤 4-6 | 生成后处理配置、msprobe compare 比对、定位异常模块 |

委派须遵守 [orchestrator subagent_io_protocol.md](../quantization-accuracy-tuning-orchestrator/references/subagent_io_protocol.md)，`input`/`output` 字段见 [references/subagent_io.md](references/subagent_io.md)。以下步骤为各执行层 subagent 的执行依据。

## 用户输入

在任务开始前，你必须获取以下输入。**不足的信息必须通过提问向用户索取，禁止自行推断或搜索**：

| # | 输入项 | 用途 | 必需 |
|---|--------|------|------|
| 1 | 浮点/量化权重模型路径 | 拉起两侧 vllm 服务采集 dump | ✅ |
| 2 | vllm serve 启动命令或脚本 | 用户模型特定启动参数（TP、max-model-len 等） | ✅ |
| 3 | msmodelslim 模型适配器文件路径 | 生成 rotate_map.json：`get_rotate_map` 定义旋转作用范围 | QuaRot 必需 |
| 4 | 模型结构文件路径（transformers `modeling_*.py`） | 生成 rotate_map.json：模型层级结构与激活流向 | QuaRot 必需 |
| 5 | 保存路径（输出目录） | 存放中间产物和最终结果；用户指定优先，缺省由 agent 建议 | ✅ |
| 6 | 推理请求内容 | 触发 dump 的 prompt；默认 `"Hello" + max_tokens=1` 即可满足定位（`step=[0]` 只采一次）；若异常仅在特定输入下复现，用户可提供该 prompt 以复现场景 | ⬜ 可选 |

**无需用户提供**（agent 自主获取或推断）：
- 量化配置 `{model_type}_best_practice.yaml`：agent 在**量化权重模型路径下**自主获取（见步骤 0），用于判断量化算法与是否需要复现
- msmodelslim 量化命令：agent 以 `{model_type}_best_practice.yaml` 为 `--config-path` + `--debug` 自然语言拉起复现量化，用户无需提供命令
- 量化权重模型路径下通常包含：`quant_model_weights.safetensors`（NonFusion，含 `div.mul_scale`）、`optional/quarot.safetensors`（QuaRot，含 `global_rotation`）、`debug_info/debug_info.safetensors`（Fusion，需量化时 `--debug`）。产物是否齐全不作为复现判断的唯一依据——**以量化配置的算法配置 + 产物现状为准**（见步骤 0）。

获取输入后，**必须**将参数回显给用户并获得认可后再进入执行步骤。

## 工作流

### 步骤 0：判断是否需要复现量化（自主获取量化配置 `{model_type}_best_practice.yaml`）

**自主获取量化配置**：在量化权重模型路径下查找 `{model_type}_best_practice.yaml`（如 `MiniMax-M3_best_practice.yaml`），**无需用户提供路径**。输入约定：量化权重路径下仅存放一份量化产物（对应唯一 yaml）；若用户路径指向多产物目录，要求其指定到单份产物目录。若量化权重路径下不存在该文件，向用户确认正确路径。

查看 `spec.process` 中的 processor 类型，按**算法类型集合**判断量化方案是否含**旋转**或**抑制**算法，决定是否需要调试模式复现：

| `spec.process` 配置 | 产物现状 | 是否需要复现 |
|---------------------|---------|-------------|
| 含旋转类算法（见下表） | 无 `optional/`（如 `quarot.safetensors`） | ✅ 复现（产出旋转矩阵） |
| 含旋转类算法 | 已有 `optional/` | ❌ 不需要（旋转矩阵已保存） |
| 含抑制类算法（见下表） | 无 `debug_info/debug_info.safetensors` | ✅ 复现（`--debug` 产出抑制 scales） |
| 含抑制类算法 | 已有 `debug_info/debug_info.safetensors` | ❌ 不需要（抑制中间量已保存） |
| 无旋转、无抑制（纯线性量化） | — | ❌ 不需要 |

**算法类型集合**（msmodelslim processor type，对应 `spec.process[].type`；算法清单见 msmodelslim《量化算法总览》）：

| 类别 | processor type | 说明 |
|------|---------------|------|
| 旋转类 | `quarot`、`adapt_rotation` | 应用正交旋转矩阵；`adapt_rotation` 为基于校准数据迭代优化的旋转（内部拆 stage1/stage2） |
| 抑制类 | `smooth_quant`、`iter_smooth`、`flex_smooth_quant`、`flex_awq_ssz`、`awq`、`kv_smooth`、`oasq` | 协同缩放激活与权重，产生抑制 scales |
| 不支持 | `online_quarot` | 在线旋转，旋转矩阵分散保存，本 Skill 不支持（见适用范围） |

集合外的 processor（如 `linear_quant`、`fa3_quant`、`gptq`、`autoround` 等纯量化/其他算法）不产生旋转或抑制中间量需求。

关键区分：

- **旋转矩阵**是量化产物的常规输出（`optional/quarot.safetensors`，yaml 中 `export_extra_info: true` 时导出），**无需 debug 模式**——只要产物里有 `optional/` 目录，旋转矩阵直接可用
- **抑制 scales**（`smooth_scales.*`）是 **debug 中间量**，仅量化时加 `--debug` 才会落盘到 `debug_info/`——配置了抑制且无 `debug_info` 时，必须复现获取

需要复现时，**以自主获取的 `{model_type}_best_practice.yaml` 为 `--config-path` 直接拉起调试模式量化**（无需用户提供量化命令），`model_type` 从 yaml 的 `metadata.verified_model_types` 或模型路径推断，`device` 询问用户或默认 `npu:0`：

```shell
python3 "<skill_root>/scripts/run_quantization.py" \
  --config-path <best_practice.yaml路径> \
  --model-path <fp_model_path> \
  --save-path <workdir>/quant_model \
  --device npu:0 \
  --trust-remote-code \
  --debug \
  --audit-log <workdir>/audit.jsonl
```

- `--debug` 必加（抑制 scales 需要 `debug_info.safetensors`）
- 复现直接用用户量化配置本身（`{model_type}_best_practice.yaml` 即 practice yaml，含相同 process 列表），保证中间量与产物同源；`--config-path` 与 `--quant-type` 互斥

### 步骤 1：生成 msprobe dump 配置

```shell
python3 "<skill_root>/scripts/gen_msprobe_config.py" --output <workdir>/probe_fp.json \
  --dump-path <workdir>/dump_fp --audit-log <workdir>/audit.jsonl
python3 "<skill_root>/scripts/gen_msprobe_config.py" --output <workdir>/probe_quant.json \
  --dump-path <workdir>/dump_quant --audit-log <workdir>/audit.jsonl
```

默认配置：`task=tensor, level=L0, step=[0], rank=[0], async_dump=false`。

### 步骤 2：逆变换准备（格式转换）

#### 2.1 生成 rotate_map.json 与旋转矩阵（仅 QuaRot）

生成 rotate_map.json 需要两个输入文件：

| 文件 | 用途 | 示例 |
|------|------|------|
| msmodelslim **模型适配器文件** | `get_rotate_map` 定义旋转作用范围：哪些模块的权重被旋转（`rot_right`/`rot_left`/`pre_run`，key 为完整模块路径） | `msmodelslim/model/minimax_m3/model_adapter.py` |
| **模型结构文件**（transformers `modeling_*.py`） | 模型层级结构与激活流向：确认模块嵌套关系、哪些激活处于旋转空间（如 embed_tokens 输出在右旋空间、其后的 RMSNorm 等中间模块也在右旋空间）、哪些激活不旋转 | `transformers/models/minimax_m3_vl/modeling_minimax_m3_vl.py` |

阅读两个文件后，按"旋转空间归属"生成 `<workdir>/rotate_map.json`（示例见 [examples/rotate_map_minimax_m3.json](examples/rotate_map_minimax_m3.json)）：

| 分类 | 来源（源码扫描） | 逆变换 |
|------|------------------|--------|
| `right_input` | 适配器 `rot_right[key]` 的模块名 | input 做 `x = x' @ R^T` |
| `right_output` | `pre_run_right` 的模块名 | output 做 `x = x' @ R^T` |
| `left_output` | `rot_left[key]` 与 `pre_run_left` 的模块名 | output 做 `x = x' @ R^T` |

- 适配器中 key 为完整模块路径（如 `model.language_model.layers.0.self_attn.q_proj`），需结合模型结构文件提取为模块名（如 `q_proj`）
- 模型结构文件用于确认模块嵌套（如 `mlp.experts.{i}`、`mlp.gate`）与激活流向，判断旋转空间内的中间模块（含 RMSNorm 等非旋转权重模块）

所有逆变换统一 `side=right, mat=R^T`（推导见 [DESIGN.md](DESIGN.md)）。

```shell
python3 "<skill_root>/scripts/convert_rotation_to_npy.py" \
  --quarot-safetensors <quant_model_dir>/optional/quarot.safetensors \
  --rotation-key global_rotation \
  --output <workdir>/rotation.npy \
  --audit-log <workdir>/audit.jsonl
```

#### 2.2 抑制因子转换（NonFusion / Fusion）

```shell
# NonFusion：扫描 div.mul_scale 并转 diag(s) npy；扫描到 0 个则说明全走 Fusion，跳过
python3 "<skill_root>/scripts/convert_suppression_to_npy.py" \
  --quant-weights <quant_model_dir>/quant_model_weights.safetensors \
  --output-dir <workdir>/suppression_scales \
  --audit-log <workdir>/audit.jsonl

# Fusion：从 debug_info.safetensors 提取 smooth_scales.*（需量化时 --debug）
python3 "<skill_root>/scripts/extract_fusion_scales.py" \
  --debug-info <quant_model_dir>/debug_info/debug_info.safetensors \
  --output-dir <workdir>/fusion_scales \
  --audit-log <workdir>/audit.jsonl
```

### 步骤 3：采集浮点 vs 量化激活值（vllm serve + msprobe dump）

在用户的 vllm serve 命令中追加两个参数，浮点/量化侧各启动一次并各发一次推理请求：

```shell
vllm serve <user_model_path> <user_other_args> --enforce-eager \
    --additional-config '{"dump_config_path": "<workdir>/probe_fp.json"}'

curl -X POST http://localhost:<port>/v1/completions \
    -H "Content-Type: application/json" \
    -d '{"model": "<model_name>", "prompt": "Hello", "max_tokens": 1}'
```

- `--enforce-eager`：msprobe dump 只在 eager 模式下生效
- 产物：`<workdir>/dump_fp/step0/rank0/`、`<workdir>/dump_quant/step0/rank0/`（含 `construct.json`、`dump.json`、tensor 文件）

### 步骤 4：生成 msprobe tensor 后处理配置

```shell
python3 "<skill_root>/scripts/gen_postprocess_config.py" \
  --rotation-npy <workdir>/rotation.npy \
  --rotate-map <workdir>/rotate_map.json \
  --dump-json <workdir>/dump_quant/step0/rank0/dump.json \
  --suppression-index <workdir>/suppression_scales/suppression_index.json \
  --fusion-index <workdir>/fusion_scales/fusion_index.json \
  --output <workdir>/postprocess_config \
  --audit-log <workdir>/audit.jsonl
```

- 参数按需传存在的场景（如仅 QuaRot 则只传 `--rotation-npy` + `--rotate-map`）
- `--dump-json`（QuaRot 强烈推荐）：从 dump.json 的 `data` 字段提取模块执行顺序，按 rotate_map 推导空间归属，对右旋空间的中间模块（**含 RMSNorm 等非旋转模块**）自动生成逆变换规则；不传则漏掉这些中间模块
- msprobe 用完整 data_name 精确匹配（不支持正则）：用 `inspect_dump.py` 提取量化侧实际 data_name 替换 YAML 占位符，再部署到 msprobe 的 `tensor_postprocess/` 目录（compare 时自动加载）

### 步骤 5：msprobe compare 精度比对 + TensorBoard

```shell
pip3 show mindstudio-probe || pip3 install mindstudio-probe --pre
pip3 show tensorboard || pip3 install tensorboard

msprobe compare -tp <workdir>/dump_quant/step0 -gp <workdir>/dump_fp/step0 \
  -o <workdir>/compare_result -c cos,md5,max_diff
tensorboard --logdir <workdir>/compare_result
```

- `-tp`（target）量化侧，`-gp`（golden）浮点侧基准
- 若两侧模块名因 Wrapper 不一致，配置 `data_mapping`（如 `gate_proj.linear.Linear` → `gate_proj.Linear`）

### 步骤 6：定位异常模块

找首个**输入一致（md5 相同）、输出不一致（md5 不同或 COS < 0.99）**的 module，即异常在本模块产生。

```
异常模块: model.language_model.layers.12.mlp.down_proj.linear [Module]
模块链路: DefaultModel → layers.12 → mlp.down_proj.linear
对比依据: Input MD5 一致(已逆抑制) | Output MD5 不一致: quant=xxx vs fp=yyy | COS=0.876543
逆变换信息: 逆旋转 right 56 / left 28 | 逆抑制 84 个内部 Linear
可视化: tensorboard --logdir <workdir>/compare_result
```

未找到异常模块 → 偏差来自量化累积误差，非单点异常。可放宽阈值（允许 md5 不一致但 COS > 0.99）、检查是否漏采关键 module、扩展 step/rank 复现。

### 结果输出

向用户输出定位结果报告，包含：

- 异常模块路径与模块层级链路（或"未找到异常模块，偏差为累积误差"的结论）
- 对比依据证据：dump 路径、md5、COS、compare 结果目录
- 逆变换配置信息（逆旋转/逆抑制范围）
- TensorBoard 可视化入口
- 审计日志位置（`<workdir>/audit.jsonl`）

## 失败路径与错误处理

各环节失败时，**必须**向用户明确输出：**失败环节 + error.code + 证据摘要 + 建议下一步**。禁止静默重试掩盖错误，禁止编造替代结论。

### 错误类型（error.code，与 [references/subagent_io.md](references/subagent_io.md) 对齐）

| error.code | 含义 |
|------------|------|
| `VALIDATION_ERROR` | 输入校验失败（缺参数、路径无效、yaml 无法解析） |
| `MODEL_LOAD_ERROR` | 模型/量化产物加载失败（缺产物、文件损坏） |
| `DUMP_ERROR` | dump 采集失败（vllm 启动失败、dump 未产出） |
| `POSTPROCESS_ERROR` | 逆变换/后处理失败（格式转换、配置生成、compare 失败） |
| `UNKNOWN_ERROR` | 未分类错误 |

### 各环节失败输出

| 环节 | 失败场景 | 向用户输出 |
|------|---------|-----------|
| 输入收集 | 缺输入 / 路径无效 | 列出缺失项或无效路径，提问索取；禁止自行搜索 |
| 输入收集 | 量化权重路径下无 `{model_type}_best_practice.yaml` | 索取正确 yaml 路径 |
| 步骤 0 | yaml 无法解析 / 含 `online_quarot` | 说明原因（在线旋转不支持），中止并询问方案 |
| 步骤 0 | 复现量化失败 | 复现失败 + 命令 + exit code + 日志摘要；询问继续 / 换命令 / 使用现有产物 |
| 步骤 1-3 | QuaRot 缺适配器/结构文件 | 索取缺失文件路径 |
| 步骤 1-3 | vllm 启动失败 / dump 未产出 | 失败 + 命令 + 错误摘要；检查项：`--enforce-eager`、probe.json 是否正确加载、服务端口 |
| 步骤 4-6 | 后处理配置生成 / compare 失败 | 失败 + 原因 + 相关路径；模块名不匹配时提示配置 `data_mapping` |
| 步骤 4-6 | 未定位到异常模块 | **非错误**：输出"偏差为累积误差"结论 + 建议（放宽阈值 / 补采 module / 扩展 step-rank / 转调优） |

### 重试与上报

- 执行层 subagent 对同一问题重试 **5 次**仍未解决 → 上报编排层
- 编排层向用户输出：失败环节 + error.code + 摘要 + **已尝试的解决措施**，询问用户的解决方案
- 编排层**不得**伪造 subagent 输出；subagent 未回传 `output` 时按 `status: failed` 处理

## 硬性规则

1. 仅基于真实 dump 数据下结论，禁止编造指标、瓶颈或原因；每条结论附 dump 路径、md5、shape 等证据
2. 用户未提供明确路径时必须先索取，禁止无范围的全盘搜索；在用户提供的**量化权重模型路径下**自主查找 `{model_type}_best_practice.yaml` 除外（步骤 0）
3. QuaRot 场景必须配置逆旋转后处理；SmoothQuant 场景必须配置逆抑制后处理（NonFusion 用 `div.mul_scale`，Fusion 用 `debug_info` 的 scales，前提是量化时加 `--debug`）
4. 所有逆变换统一用 msprobe 原生 matmul 后处理（逆抑制用 `diag(s)` 对角矩阵等价替换逐元素乘法）；旋转作用范围通过 `--rotate-map` 传入，不硬编码模型类型
5. 默认 `step=[0], rank=[0]`，用户可按需扩展
6. 所有脚本通过 `--audit-log <workdir>/audit.jsonl` 写入审计日志
