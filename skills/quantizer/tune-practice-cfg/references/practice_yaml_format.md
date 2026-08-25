# 量化配置格式（Practice YAML）

## 整体结构

```yaml
apiversion: modelslim_v1          # API 版本：modelslim_v0 | modelslim_v1 | multimodal_vlm_modelslim_v1
metadata:
  config_id: "unique-config-name" # 配置唯一标识
  score: 100.0                    # 排序分数（越高越优先）
  verified_model_types: []        # 已验证适用的模型类型列表
  label:                          # 过滤标签（必填 dict，禁止写成字符串）
    w_bit: 8
    a_bit: 8
    is_sparse: false
    kv_cache: false
  verified_tags: {}               # {model_type: [[tag1, tag2], [tag3]]}
spec:
  process: []                     # 量化处理器列表（见下文）
  dataset: "mix_calib.jsonl"          # 校准数据集（无路径时用短名，在 lab_calib 解析）
  save: []                        # 保存配置
```

- **metadata**：仅上述键；勿塞 `quantization` 等与顶层无关项。
- **spec**：仅 V1 允许的块；勿混 GPTQ/C4 等无关段名。
- **process 顺序**：一般先离群/旋转等前处理，再 `linear_quant` / `autoround_quant` 等。

## Process 处理器类型

`spec.process` 是一个有序列表，每个元素定义一个处理步骤。

### linear_quant — 线性层量化

```yaml
- type: "linear_quant"
  qconfig:
    act:
      scope: "per_token"
      dtype: "int8"
      symmetric: true
      method: "minmax"
      ext: {}                    # 扩展参数，如 ssz 的 step: 10
    weight:
      scope: "per_channel"
      dtype: "int8"
      symmetric: true
      method: "minmax"
      ext: {}
  include: ["*"]                 # 包含的层模式（fnmatch 匹配）
  exclude: []                    # 排除的层模式（fnmatch 匹配）
```

### flex_smooth_quant — 离群值抑制（SmoothQuant）

```yaml
- type: "flex_smooth_quant"
  include: ["*"]
```

### iter_smooth — 迭代式 SmoothQuant

```yaml
- type: "iter_smooth"
  include: ["*"]
```

### flex_awq_ssz — AWQ + SSZ 组合

```yaml
- type: "flex_awq_ssz"
  qconfig:
    act:
      scope: "per_token"
      dtype: "int8"
      symmetric: true
      method: "minmax"
    weight:
      scope: "per_channel"
      dtype: "int4"
      symmetric: true
      method: "ssz"
      ext:
        step: 10
  enable_subgraph_type:
    - "norm-linear"
    - "linear-linear"
    - "ov"
    - "up-down"
```

### quarot — Quantization-Aware RoT

```yaml
- type: "quarot"
```

## QConfig 字段取值

| 字段 | 有效值 | 说明 |
|------|--------|------|
| **dtype** | `int8`, `int4`, `float`, `mxfp8`, `mxfp4`, `fp8_e4m3` | 量化数据类型 |
| **scope** | `per_tensor`, `per_channel`, `per_group`, `per_block`, `per_token`, `pd_mix`, `per_head` | 量化粒度 |
| **symmetric** | `true`, `false` | 是否对称量化 |
| **method** | `minmax`, `mse`, `ssz`, `awq`, `quarot`, `none` | 校准算法 |
| **ext** | 对象，如 `{step: 10}` | 算法扩展参数 |

> `dtype: "float"` 表示保持浮点不量化（用于回退层）。`method: "none"` 表示不使用校准。

## Save 保存配置

```yaml
save:
  - type: "ascendv1_saver"
    part_file_size: 4            # 分片大小（GB）
```

- save的配置默认优先按照以上示例填写

## 完整示例（W8A8 默认配置）

```yaml
apiversion: modelslim_v1
metadata:
  config_id: default-w8a8
  score: 50
  verified_model_types: []
  label:
    w_bit: 8
    a_bit: 8
    is_sparse: false
    kv_cache: false
spec:
  process:
    - type: "iter_smooth"
      include: ["*"]
    - type: "linear_quant"
      qconfig:
        act:
          scope: "per_token"
          dtype: "int8"
          symmetric: true
          method: "minmax"
        weight:
          scope: "per_channel"
          dtype: "int8"
          symmetric: true
          method: "minmax"
      include: ["*"]
  dataset: "mix_calib.jsonl"
  save:
    - type: "ascendv1_saver"
      part_file_size: 4
```

## VLM 专属字段

- `spec.default_text`：默认值为 `"Describe this image in detail."`。
- `spec.dataset`：VLM 校准数据集，默认使用 `calibImages`。
- `spec.process[].include`：继承基准 Practice 中对应处理器的配置；自动调优过程中不得扩大其作用范围。
- `spec.process[type=linear_quant].exclude`：由 `protected_exclude` 与 `tuning_exclude` 合并生成。`protected_exclude` 包含基准 Practice 的静态 `exclude`，以及为保护视觉编码器、多模态投影层等非调优模块而增加的固定排除项；`tuning_exclude` 由敏感层搜索增减。

### 静态排除与调优排除

1. 优先选择与当前 `model_type` 和量化方案匹配的已验证 Practice，并原样继承其中的静态 `exclude`。
2. 静态排除项记录为 `protected_exclude`，自动调优过程中不得删除。
3. 每轮最终写入 YAML 的 `exclude` 为 `protected_exclude ∪ tuning_exclude`，并保持稳定顺序、去除重复项。
4. 若没有匹配的已验证 VLM Practice，应根据实际模型结构生成保守基线，确保视觉编码器和多模态投影层不在目标量化处理器的作用范围内；无法确认量化范围时立即返回，不得仅根据通用模块名称推断。

## VLM 完整示例（W8A8 默认配置）

```yaml
apiversion: multimodal_vlm_modelslim_v1
metadata:
  config_id: qwen3_vl_4b_w8a8
  score: 90
  verified_model_types:
    - Qwen3-VL-4B-Instruct
  label:
    w_bit: 8
    a_bit: 8
    is_sparse: false
    kv_cache: false

default_w8a8: &default_w8a8
  act:
    scope: "per_tensor"
    dtype: "int8"
    symmetric: false
    method: "minmax"
  weight:
    scope: "per_channel"
    dtype: "int8"
    symmetric: true
    method: "minmax"

spec:
  process:
    - type: "iter_smooth"
      alpha: 0.9
      scale_min: 1e-5
      symmetric: true
      enable_subgraph_type:
        - "norm-linear"
        - "linear-linear"
        - "ov"
        - "up-down"
      include:
        - "*"
    - type: "linear_quant"
      qconfig: *default_w8a8
      include:
        - "*"
      exclude:
        - "*vit*"
        - "*visual*"
        - "*merger*"
  save:
    - type: "ascendv1_saver"
      part_file_size: 4
  dataset: "calibImages"
  default_text: "Describe this image in detail."
```

## 常见错误

- `metadata.label` 写成字符串而非 dict
- `type` 与字段不匹配（如 `flex_awq_ssz` 缺少 `qconfig`）
- `dataset` 指向不存在的路径，或使用了当前安装环境中不存在的短名称
- `save` 字段的 `type` 不为 `"ascendv1_saver"`
