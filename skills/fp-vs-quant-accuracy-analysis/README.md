# fp-vs-quant-accuracy-analysis

浮点权重推理 vs 量化权重推理的 dump 对比，定位量化导致的精度异常模块。支持 QuaRot 旋转和 SmoothQuant 抑制变换的逆操作。

## 适用场景

- 量化推理出现精度异常，需定位到具体 module
- 算子/框架组图异常导致 LLM 调优无法继续
- W8A8 + QuaRot + SmoothQuant 量化方案
- 硬件：A2 / A3 / A5
- 服务化部署：vllm serve

## 核心能力

| 能力 | 说明 |
|------|------|
| 浮点 dump 采集 | 通过 vllm serve + `dump_config_path` 采集浮点权重推理的 module 级 tensor（默认 1 step + 1 rank） |
| 量化 dump 采集 | 同上，采集量化权重推理的 module 级 tensor |
| 旋转矩阵格式转换 | 从 `quarot.safetensors` 加载 `global_rotation`，转换为 `rotation.npy` |
| NonFusion 抑制因子转换 | 扫描 `quant_model_weights.safetensors` 中的 `div.mul_scale`，转换为 `diag(s)` npy |
| Fusion 抑制因子提取 | 从 `debug_info.safetensors` 提取 `smooth_scales.*`，转换为 `diag(s)` npy（需量化时加 `--debug`） |
| msprobe tensor 后处理配置 | 生成逆旋转 + NonFusion 逆抑制 + Fusion 逆抑制规则配置（统一用 matmul） |
| msprobe compare + TensorBoard | 用 msprobe compare 比对浮点 vs 量化（compare 时自动加载后处理配置做逆变换），TensorBoard 可视化 |

> **注**：所有逆变换统一用 msprobe 原生 matmul 操作（逆抑制用 `diag(s)` 对角矩阵等价替换逐元素乘法），无需扩展 msprobe。详见 [DESIGN.md](DESIGN.md) 决策 3.7。

## 文件结构

```
skills/fp-vs-quant-accuracy-analysis/
├── SKILL.md                              # 主流程定义
├── README.md                             # 本文件
├── DESIGN.md                             # 设计文档
├── scripts/
│   ├── gen_msprobe_config.py             # 生成 probe.json（vllm serve 集成用）
│   ├── convert_rotation_to_npy.py        # 旋转矩阵格式转换（safetensors → npy）
│   ├── convert_suppression_to_npy.py     # NonFusion 抑制因子转换（div.mul_scale → diag(s) npy）
│   ├── extract_fusion_scales.py          # Fusion 抑制因子提取（debug_info → diag(s) npy）
│   ├── gen_postprocess_config.py         # 生成 msprobe tensor 后处理配置（统一 matmul）
│   └── inspect_dump.py                   # dump 数据结构检查工具
├── examples/
│   └── rotate_map_minimax_m3.json        # MiniMax-M3 旋转作用范围参考（验收用例示例）
└── references/
    └── rotate_map_minimax_m3.md          # MiniMax-M3 旋转作用范围说明
```

## 快速开始

### 前置条件

- 浮点权重模型：`/path/to/float_model`
- 量化权重模型：`/path/to/quant_model`（含 `optional/quarot.safetensors`、`quant_model_weights.safetensors`）
- **量化时加 `--debug`**（Fusion 路径必需，否则无法提取 Fusion scales）
- msprobe 已安装：`pip install mindstudio-probe --pre`
- tensorboard 已安装：`pip install tensorboard`
- vllm-ascend 已安装（提供 `dump_config_path` 集成）

### 执行流程

```bash
WORKDIR=/workdir/precision_analysis
SKILL_ROOT=/home/txy/project/msagent/skills/fp-vs-quant-accuracy-analysis
FP_MODEL=/path/to/float_model
QUANT_MODEL=/path/to/quant_model

# 1. 生成 msprobe dump 配置（默认 1 step + 1 rank，level=L0，tensor 模式）
python3 $SKILL_ROOT/scripts/gen_msprobe_config.py \
    --output $WORKDIR/probe_fp.json \
    --dump-path $WORKDIR/dump_fp

python3 $SKILL_ROOT/scripts/gen_msprobe_config.py \
    --output $WORKDIR/probe_quant.json \
    --dump-path $WORKDIR/dump_quant

# 2. 旋转矩阵格式转换（仅 QuaRot 场景）
python3 $SKILL_ROOT/scripts/convert_rotation_to_npy.py \
    --quarot-safetensors $QUANT_MODEL/optional/quarot.safetensors \
    --rotation-key global_rotation \
    --output $WORKDIR/rotation.npy

# 3. NonFusion 抑制因子转换（从 quant_model_weights.safetensors 提取 div.mul_scale）
python3 $SKILL_ROOT/scripts/convert_suppression_to_npy.py \
    --quant-weights $QUANT_MODEL/quant_model_weights.safetensors \
    --output-dir $WORKDIR/suppression_scales

# 4. Fusion 抑制因子提取（从 debug_info.safetensors 提取 smooth_scales）
#    前提：量化时必须加 --debug，否则 debug_info.safetensors 为空
python3 $SKILL_ROOT/scripts/extract_fusion_scales.py \
    --debug-info $QUANT_MODEL/debug_info/debug_info.safetensors \
    --output-dir $WORKDIR/fusion_scales

# 5. 准备 rotate_map.json（根据实际模型的 msmodelslim.get_rotate_map 输出填写）
#    参考示例：examples/rotate_map_minimax_m3.json
cp $SKILL_ROOT/examples/rotate_map_minimax_m3.json $WORKDIR/rotate_map.json

# 6. 生成 msprobe tensor 后处理配置（统一用 matmul，含逆旋转 + NonFusion 逆抑制 + Fusion 逆抑制）
python3 $SKILL_ROOT/scripts/gen_postprocess_config.py \
    --rotation-npy $WORKDIR/rotation.npy \
    --rotate-map $WORKDIR/rotate_map.json \
    --suppression-index $WORKDIR/suppression_scales/suppression_index.json \
    --fusion-index $WORKDIR/fusion_scales/fusion_index.json \
    --output $WORKDIR/postprocess_config

# 7. 用 inspect_dump.py 提取实际 data_name（msprobe 用完整字符串匹配，不支持正则）
python3 $SKILL_ROOT/scripts/inspect_dump.py \
    --dump-path $WORKDIR/dump_quant/step0
# 然后手动替换 postprocess_config.yaml 中的 data_name 占位符

# 8. 把 YAML 配置放到 msprobe 的 tensor_postprocess/ 目录下
cp $WORKDIR/postprocess_config.yaml /path/to/msprobe/python/msprobe/core/compare/tensor_postprocess/

# 9. 通过 vllm serve 拉起浮点服务并触发 dump（--enforce-eager 必须）
vllm serve $FP_MODEL \
    --host 0.0.0.0 \
    --port 8900 \
    --trust-remote-code \
    --enforce-eager \
    --additional-config "{\"dump_config_path\": \"$WORKDIR/probe_fp.json\"}"

# 10. 发送推理请求触发 dump（一次请求即可，step=[0] 只采集这一次）
curl -X POST http://localhost:8900/v1/completions \
    -H "Content-Type: application/json" \
    -d '{"model": "float_model", "prompt": "Hello", "max_tokens": 1}'

# 11. 通过 vllm serve 拉起量化服务并触发 dump
vllm serve $QUANT_MODEL \
    --host 0.0.0.0 \
    --port 8901 \
    --trust-remote-code \
    --enforce-eager \
    --additional-config "{\"dump_config_path\": \"$WORKDIR/probe_quant.json\"}"

# 12. 发送推理请求触发量化侧 dump
curl -X POST http://localhost:8901/v1/completions \
    -H "Content-Type: application/json" \
    -d '{"model": "quant_model", "prompt": "Hello", "max_tokens": 1}'

# 13. msprobe compare 精度比对（compare 时自动加载 tensor_postprocess 配置做逆变换）
msprobe compare \
    -tp $WORKDIR/dump_quant/step0 \
    -gp $WORKDIR/dump_fp/step0 \
    -o $WORKDIR/compare_result \
    -c cos,md5,max_diff

# 14. TensorBoard 可视化
tensorboard --logdir $WORKDIR/compare_result
```

## 关键时机说明

**msprobe tensor 后处理在 compare 阶段实时处理**，不是在 dump 阶段：

```
dump 阶段：vllm serve → msprobe 采集原始 tensor（含旋转/抑制变换）→ 落盘
                                    ↓
compare 阶段：msprobe compare → 读入 tensor → 【应用 tensor_postprocess 配置做逆变换】→ 校验/指标计算
                                    ↑
                        tensor_postprocess/ 目录下的 YAML 配置
```

- **dump 阶段**：只采集原始 tensor，不做逆变换
- **compare 阶段**：读入 tensor 后、校验前，自动应用 tensor_postprocess 配置做逆变换
- **不修改 dump 文件**：逆变换只在内存中进行

## 逆变换原理

**核心设计**：所有逆变换统一用 **msprobe 原生的 matmul 操作**（左乘/右乘），无需扩展 msprobe。

### msprobe 后处理机制

msprobe 的 tensor 后处理（`msprobe.core.compare.tensor_postprocess`）原生**只支持 matmul**，不支持 mul/div/add。执行时机为**比对时实时处理**（读入 tensor 后、校验前），不修改 dump 文件。

### 统一 matmul 形式

| 逆变换类型 | 逐元素形式 | matmul 形式（msprobe 后处理用） | side |
|----------|----------|------------------------------|------|
| 逆旋转 right | `x = x' @ R^T` | `x = x' @ R^T`（原生 matmul） | right |
| 逆旋转 left | `x = R @ x'` | `x = R @ x'`（原生 matmul） | left |
| NonFusion 逆抑制 | `x = x' * s` | `x = x' @ diag(s)`（对角矩阵替换） | right |
| Fusion 逆抑制 | `x = x' * s` | `x = x' @ diag(s)`（对角矩阵替换） | right |

**关键**：msprobe 不支持逐元素运算，所以把抑制因子 s 转换为 `diag(s)` 对角矩阵，用 `x @ diag(s)` 等价替换 `x * s`。

### 数学等价性证明

`x * s = x @ diag(s)`（当 x 最后一维是 hidden 时，numpy matmul 自动广播）：

```
x = [x1, x2, x3]        shape [3]
s = [s1, s2, s3]        shape [3]

diag(s) = [[s1,  0,  0],
           [ 0, s2,  0],
           [ 0,  0, s3]]       shape [3, 3]

x @ diag(s) = [x1*s1, x2*s2, x3*s3] = x * s   ✅
```

高维情况（`[batch, seq, hidden]`）：numpy/torch 的 matmul 对最后两维做矩阵乘法，前面的维度自动广播。

### QuaRot 旋转（需要逆变换）

msmodelslim 的 QuaRot 生成正交 Hadamard 矩阵 `R`（shape `[hidden, hidden]`），保存到 `optional/quarot.safetensors` 的 `global_rotation` key。

| 方向 | 正向变换 | 逆变换（msprobe matmul 后处理） |
|------|---------|------------------------------|
| right（q_proj/gate_proj 等） | `W' = W @ R`, `x' = x @ R` | `x = x' @ R^T`（side=right，对 Wrapper 外层输入） |
| left（o_proj/down_proj 等） | `W' = R^T @ W`, `x' = R^T @ x` | `x = R @ x'`（side=left，对 Wrapper 外层输出） |

### SmoothQuant 抑制 - NonFusion 路径（需要逆变换）

NonFusion 路径下，被抑制 Linear 被包装为 `NonFusionSmoothQuantWrapper`，内部 Linear 输入被 `x/s` 缩放。

| 项 | 公式 |
|----|------|
| 保存值 | `div.mul_scale = 1/s` |
| 脚本保存的 npy 值 | `diag(s) = diag(1/div.mul_scale)`（对角矩阵） |
| 推理时激活 | `x' = x * div.mul_scale = x/s` |
| **逆变换（matmul 形式）** | `x = x' @ diag(s)`（side=right，对内部 Linear 输入） |

**为什么需要逆变换**：虽然输出数值等价，但内部 Linear 的输入被缩放，与浮点侧不一致。msprobe module 级采集下，能匹配到浮点侧的是内部 Linear（通过 data_mapping），所以必须对内部 Linear 输入做逆变换。

### SmoothQuant 抑制 - Fusion 路径（需要逆变换）

Fusion 路径下 s 已吸收进相邻层权重（`W_up /= s`，`W_down *= s`），不保存 `div.mul_scale`。但量化时加 `--debug` 会把 s 保存到 `debug_info.safetensors`：

| 项 | 公式 |
|----|------|
| `debug_info` 中保存值 | `scales = s` |
| 脚本保存的 npy 值 | `diag(s)`（对角矩阵） |
| **逆变换（matmul 形式）** | `x = x' @ diag(s)`（side=right，对上游层输出 = 下游 Linear 输入） |

**为什么需要逆变换**：Fusion 下上游层（如 norm）输出 = `norm(x)/s`（与浮点侧 `norm(x)` 不一致），是下游 Linear 的输入。虽然下游 Linear 输出等价，但中间激活（norm 输出 / Linear 输入）与浮点侧不一致。

**前提条件**：量化时必须加 `--debug`，否则 `debug_info.safetensors` 为空。详见 [DESIGN.md](DESIGN.md) context 机制说明。

## 限制

- 仅支持 QuaRot 离线旋转（正交 Hadamard 矩阵）
- 不支持在线 QuaRot（旋转矩阵分散保存，未集中到 quarot.safetensors）
- NonFusion SmoothQuant 需有 `div.mul_scale` 产物
- Fusion SmoothQuant 需量化时加 `--debug`（否则无法提取 scales，只能改用子图整体比对）
- 默认单 rank + 单 step，多 rank/step 需扩展参数
- 当前 dump 流程基于 vllm serve，其他推理框架需适配
- 旋转作用范围需用户通过 `--rotate-map` 参数提供（模型无关设计）
