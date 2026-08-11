# 模型访问序列

## 接口契约

`generate_model_visit()` 是 `ModelSlimPipelineInterfaceV1` 的通用抽象方法；继承该接口时必须实现。它按 Runner 修改或量化模块的顺序产生 `ProcessRequest`。访问序列必须与 `generate_model_forward()` 的分段执行序列一致，包括名称、模块边界和先后顺序。

## Qwen 两段式访问

本 Skill 的 Qwen 风格结构可以压缩成两个 yield 阶段：

1. 将完整视觉模块作为一个 `ProcessRequest` 访问。
2. 使用 `generated_decoder_layer_visit_func()` 和 `generate_decoder_layer()` 逐层访问语言解码器。

参考 `assets/qwen-style-adapter/target_vlm/model_adapter.py` 中的 `generate_model_visit()`。必须按目标模型替换视觉模块名称与路径，并验证第二阶段使用的动态语言层生成器。

`generate_model_visit()` 本身虽然只有两个 yield 表达式，但第二个 `yield from` 依赖以下行为正确：

- `generate_decoder_layer()` 按真实层数和索引顺序产生 `(name, layer)`；
- 缺失或位于 meta device 的层能按需创建并加载正确权重；
- 已加载首层不会被重复创建。

## 边界要求

- 如果视觉投影、resampler、merger、跨模态融合器或其他常驻模块不属于所 yield 的视觉模块，按真实前向顺序单独产生 `ProcessRequest`。
- `ProcessRequest.name` 使用模型内真实、稳定的模块路径，并与动态权重前缀保持一致。
- 不要为了套用两段模板而合并需要独立量化、独立加载或在前向中独立调度的模块。
- 不要仅比较 yield 数量；以访问序列和前向序列逐项对应为验收标准。

采用按需加载时，`generate_decoder_layer()` 及其依赖的权重加载规则见[动态 Decoder 与权重加载](dynamic-decoder-loading.md)。
