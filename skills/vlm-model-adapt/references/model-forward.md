# 模型分段前向

## 接口契约

`generate_model_forward()` 是 `ModelSlimPipelineInterfaceV1` 的通用抽象方法；继承该接口时必须实现。它把原模型的一次完整前向拆成有序的 `ProcessRequest`，并在 yield 之间复现原模型未被独立调度的张量运算。

这是 VLM Adapter 中最依赖目标模型源码、最不适合套用固定模板的部分。实现必须以目标运行版本的可执行代码为准；注释、docstring、旧文档和其他版本示例仅用于定位，冲突时不得覆盖代码行为。

## 主动定位模型源码

先按[模型实现定位](model-source-discovery.md)分别确定 Config、Model、Processor 和 Tokenizer 的真实来源。对 Model 优先读取服务器实际安装版本的 Transformers 源码；不存在或不兼容时，再读取模型目录中由 `auto_map` 指向的自定义实现。两边都没有时，从网络检索与服务器安装版本完全一致的 Transformers 官方源码；找不到精确版本时停止并报告。

搜索时优先使用 `inspect.getfile()`、`rg --files` 和 `rg` 定位实际类、`modeling*.py`、目标 `forward()` 及 helper。阅读完整函数体；本 Skill 的模板和当前 msModelSlim Dense VLM Adapter 只用于参考调度接法，不能替代目标原模型源码，也不能直接复制模型相关实现。

## 还原真实调用链

从实际入口逐层追踪，不要只阅读最外层 `forward()`：

1. 条件生成模型或顶层模型的 `forward()`。
2. 多模态主体模型的 `forward()`。
3. 视觉编码器调用及其返回结构。
4. 文本 embedding 和视觉占位 token 的构造。
5. 视觉特征投影、拼接、替换或 scatter 融合。
6. attention mask、cache position、position ids、mRoPE/rotary embedding 的构造。
7. 每个 decoder layer 的参数与返回值。
8. DeepStack、跨层视觉特征、残差或其他层间注入。
9. KV cache、输出归一化和其他校准流程需要保留的尾部行为。

把每一步记录为“输入字段 → 张量变换或模块 → 输出 shape/dtype → 下游消费者”，再决定哪些步骤形成 `ProcessRequest`，哪些保留为 yield 之间的普通张量操作。

## Qwen 风格分段

本 Skill 的 Qwen 风格模板可概括为：

1. 从 `handle_dataset()` 输出取得图像 tensor、网格、token 和 mask。
2. yield 完整视觉模块，取得图像特征及可选 DeepStack 特征。
3. 生成文本 embedding，按图像 token mask 将视觉特征融合进序列。
4. 依据目标源码构造 cache position、mRoPE position ids、causal mask 和 rotary embeddings。
5. 使用 `generate_decoder_layer()` 逐层 yield 文本解码器。
6. 在目标源码规定的层和时机注入 DeepStack 或其他跨层视觉特征。

参考 `assets/qwen-style-adapter/target_vlm/model_adapter.py` 中的 `generate_model_forward()`，但必须逐行对照目标版本的 `modeling*.py`。不得照抄图像 token id、视觉返回值数量、`masked_scatter`、mRoPE 维度、mask helper、decoder kwargs 或 DeepStack 注入条件。

## 硬性边界

- `handle_dataset()` 产生的字段必须全部有明确消费者；`generate_model_forward()` 不得暗中依赖不存在的字段。
- `generate_model_visit()` 和本方法产生的 `ProcessRequest` 必须在名称、模块边界、数量和顺序上对应。
- `ProcessRequest` 只包围 Runner 需要调度或修改的模块调用；融合、索引、mask 构造等普通张量操作按原源码保留。
- 不要因为注释声称某 shape、返回值或注入位置成立就采用它；读取赋值、分支、调用参数和返回语句验证。
- 区分移动模块和对齐临时 tensor。Runner 管理模块设备，不要擅自对模块 `.to(device)`；融合运算仍需按原源码对齐 tensor 的 device 和 dtype。
- 保留 `torch.no_grad()` 或等价推理上下文、KV cache 策略和校准所需的确定性行为。
- 只实现图像加文本分支；若原模型还存在纯文本、视频、音频或其他模态路径，明确拒绝，不把它们带入分段前向。

## 验证方式

使用相同真实样本，对官方未拆分前向与 Adapter 分段前向进行中间结果对齐。至少比较视觉输出、融合后的 embedding、位置与 mask 张量、首层和末层 hidden states 的 shape、dtype、device 及数值误差。仅验证流程不报错不足以证明实现正确。
