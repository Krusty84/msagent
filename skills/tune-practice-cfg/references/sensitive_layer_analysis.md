# 敏感层分析

## 分析工具

通过 `execute` 调用 **msmodelslim CLI**：

```bash
msmodelslim analyze layer \
    --model_type Qwen3-32B \
    --model_path "${model_path}" \
    --metrics mse_layer_wise \
    --calib_dataset "${effective_calib_dataset}" \
    --quant_modules "${analysis_include_patterns[@]}" \
    --topk "${NUM_HIDDEN_LAYERS}" \
    --device npu \
    > "${SAVE_PATH}/analysis_console.log" 2>&1
```

执行命令前须构造以下变量：

- `NUM_HIDDEN_LAYERS`：从模型 `config.json` 中读取 `num_hidden_layers`。依次检查顶层、`text_config`、`language_config` 和 `thinker_config.text_config`；均未找到时停止执行。
- `effective_calib_dataset`：优先使用显式传入的 `calib_dataset`；未传入时，`modelslim_v1` 使用 `mix_calib.jsonl`，`multimodal_vlm_modelslim_v1` 使用 `calibImages`。
- `analysis_include_patterns`：从基准 Practice 的 `spec.process[type=linear_quant].include` 读取；缺失或为空时使用 `["*"]`。多个模式必须作为同一个 `--quant_modules` 后的独立数组元素参数传递，不得拼接成一个字符串。

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `model_type` | 模型类型（模型名） | 必填 |
| `model_path` | 模型路径 | 必填 |
| `metrics` | 分析指标（layer scope） | `"mse_layer_wise"` |
| `calib_dataset` | 校准数据集 | LLM 可使用已确认的纯文本校准集；VLM 必须使用当前模型适配器支持的多模态数据 |
| `quant_modules` | 参与量化对比的模块匹配模式列表 | `"*"`（全量） |
| `topk` | 返回 Top-K 敏感层 | 从模型 `config.json` 解析出的 `num_hidden_layers` |
| `device` | 执行设备 | `npu` |

- `model_type` 与模型 `config.json` 中的 `model_type` 并非同一概念，你应该参考 `msmodelslim/config/config.ini`，如 `Qwen3-32B` `DeepSeek-V3` 才是正确合法的 `model_type`。
- 官方文档：[敏感层分析使用指南](https://gitcode.com/Ascend/msmodelslim/blob/master/docs/zh/feature_guide/sensitive_layer_analysis/usage.md)

**注意事项**
- 敏感层分析运行时长可能较长，必须按上方命令将输出直接写入日志文件，不得依赖 `tee` 保存结果。若外层执行超时，先确认原分析进程是否仍在运行；**务必避免**在上一个敏感层分析进程未结束时再次拉起一个敏感层分析进程。
- 当使用 mse_layer_wise 时，topk 按照模型实际层数（config.json 中 num_hidden_layers）填写。

## 支持的分析指标

当前 `msmodelslim analyze layer` 服务支持以下两种指标：

| 指标 | 说明 | 使用规则 |
|------|------|----------|
| **mse_layer_wise** | 计算各 Decoder Block 量化前后的 MSE | 默认使用；用于生成敏感层排序 |
| **mse_model_wise** | 计算整模型量化前后的 MSE | 可选；用于模型整体误差分析 |

## 分析结果结构

CLI 将各层 Score 写入 `analysis_console.log`。解析后写入 `{save_path}/analysis_result.yaml`：

```yaml
layer_scores:
  - name: "model.layers.0"
    score: 12.5
  - name: "model.layers.15"
    score: 8.3
  # ... 按 score 降序排列
method: "mse_layer_wise"
patterns:
  - "*"
```

- **score 越高，层越敏感**，量化时越容易造成精度损失。
- `layer_scores[].name` 保存 ModelSlim 返回的原始 Decoder Block 名称，不在分析结果中预先追加通配符。
- 分析结果用于调优策略中的 `exclude` 决策和回退层排序。生成 `linear_quant.exclude` 时，对 layer scope 的名称追加 `.*`，例如将 `model.layers.15` 转换为 `model.layers.15.*`；名称已经以 `.*` 结尾时不得重复追加。

## 分析结果在调优中的使用

敏感度得分在调优任务开始时计算一次，写入 `{save_path}/analysis_result.yaml`，各轮复用。每轮根据策略从预计算的得分排序中选择回退层，无需重新调用分析命令。

选择回退层时需遵守**同分同退约束**：分析结果按 score 降序排列，分数相同的层作为一个整体（同分组），`topk` 参数选取的是前 K 个**同分组**而非前 K 个单独层。在调优过程中，同分组内的层必须同时回退或同时保留，不可拆分。

## 基准 Practice 范围约束

- 分析前读取基准 Practice，将`spec.process[type=linear_quant].include` 作为 `--quant_modules`。
- 基准 Practice 中的静态 `exclude` 记录为 `protected_exclude`，包括视觉编码器、投影层及其他不参与自动调优的模块；它不参与二分搜索，任何轮次都不得删除。
- `multimodal_vlm_modelslim_v1` 使用的 `calib_dataset` 必须与后续 Practice 的 `spec.dataset` 解析到同一份多模态校准数据，并满足当前模型适配器的要求。

## 工具不可用时的经验规则

当 `msmodelslim analyze` 失败（非 0 exit code）或超时时，按以下步骤占位：

1. **获取总层数 N**：从 `<model_path>/config.json` 读取 `num_hidden_layers`。嵌套 config 依次查顶层、`text_config`、`language_config`、`thinker_config.text_config` 等同名字段
2. **构造经验排序**：层序上前 2-4 层 + 后 2-4 层视为更敏感，中间段相对低敏感
3. **写出结果文件**：将经验排序按上方"分析结果结构"的格式写入 `{save_path}/analysis_result.yaml`，确保后续步骤无需区分数据来源

仅作占位，**弱于**精确分析。
