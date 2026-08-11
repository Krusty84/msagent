# 多模态数据处理

## 接口契约

`handle_dataset()` 是 `ModelSlimPipelineInterfaceV1` 的通用抽象方法；继承该接口时必须实现。它接收校准数据并返回 `List[Any]`，其中每项必须能被同一 Adapter 的 `generate_model_forward()` 直接消费。

当前范围只消费同时包含 `text` 与 `image` 的 `VlmCalibSample`。Adapter 不负责重新实现数据集扫描；必须拒绝纯文本、视频、音频以及缺失任一必需字段的样本。

## 通用工作流

1. 从目标模型官方推理示例确认使用 processor、tokenizer 还是自定义媒体预处理。
2. 加载预处理组件；本地模型默认使用 `local_files_only=True`，并传递 `self.trust_remote_code`，不得擅自硬编码为 `True`。
3. 校验样本字段和本地媒体路径。明确拒绝缺失字段、无效路径和未支持模态。
4. 按目标模型要求构造 messages、视觉占位符和 generation prompt。
5. 执行目标模型的预处理流程，获得文本与媒体 tensor。
6. 从目标模型真实 `forward()` 签名以及 `generate_model_forward()` 的消费逻辑确定输入 `keys` 和 `defaults`。
7. 优先使用 `VLMBaseModelAdapter._collect_inputs_to_device()` 收集字段并移动 tensor。
8. 返回处理结果列表；不要返回 processor、生成文本或尚未编码的媒体路径。

## 实现风格选择

按以下证据顺序选择实现，不要仅凭 processor 类名猜测：

1. 目标版本的官方推理示例。
2. processor、chat template 和 remote-code 实现。
3. 模型 `forward()` 签名及视觉编码路径。

常见风格包括：

- processor 通过 `apply_chat_template(..., tokenize=True, return_dict=True, return_tensors="pt")` 一步生成文本和视觉 tensor。
- 先以 `tokenize=False` 生成模板文本，再单独解析图片并调用 processor。
- tokenizer 生成文本输入，自定义逻辑加载、切分并编码图片。

这些风格都有效。判断标准是输出能否被目标 Adapter 的分段前向正确消费，而不是是否与 Qwen 示例写法相同。

## 边界要求

- 保持数据加载与模型编码职责分离；不要在 Adapter 中重新扫描数据集目录。
- 只接受图像加文本校准样本，并明确拒绝纯文本、视频和音频输入。
- 不要固定 messages 结构、图片对象类型、`add_generation_prompt`、`tokenize` 或 padding 策略。
- 不要照抄其他模型的 `keys`、`defaults`、视觉 tensor 名称或 shape。
- `self._processor` 与 `self._tokenizer` 可缓存，但重复调用 `handle_dataset()` 时不得残留跨数据集状态。
- 处理空数据集、非法样本和不支持模态时给出明确、可行动的错误。
- 图片占位 token、视觉特征数量、位置编码和网格信息必须与目标模型要求一致。

## Qwen 风格模板

当目标 processor 的官方实现确认支持一步生成多模态 tensor 时，参考 `assets/qwen-style-adapter/target_vlm/model_adapter.py` 中的 `handle_dataset()`。必须替换：

- `build_messages_for_target_model()`；
- `TARGET_FORWARD_KEYS`；
- `TARGET_FORWARD_DEFAULTS`；
- 样本模态校验和安全路径校验；
- processor 参数，包括 `add_generation_prompt`。

该模板是参考实现，不是默认答案。若目标模型采用两阶段 processor 或 tokenizer 风格，应直接按其官方流程实现，不要强行改造成 Qwen 风格。
