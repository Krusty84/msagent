# 模型初始化

## 接口契约

`init_model()` 是 `ModelSlimPipelineInterfaceV1` 的通用抽象方法；继承该接口时必须实现。它返回可供 Runner 修改和分段前向的 `nn.Module`。本 skill 对所有受支持模型大小使用相同的分层初始化粒度。

统一策略是完整加载视觉部分和必要的融合模块，仅实例化并加载第一个语言解码层，其余语言层由 `generate_decoder_layer()` 按 safetensors 权重索引逐层加载。大小模型不得采用不同的语言层常驻路径。

## 通用工作流

1. 确认目标模型类、配置层级、语言层容器、视觉与融合模块的构造方式。
2. 取得全局计算 dtype并校验本地模型路径。列出初始化期间将修改的每个配置对象与属性，包括语言层数、KV cache、attention implementation 及目标模型的其他临时开关。
3. 在首次修改前快照上述属性的原值；随后关闭初始化阶段不需要的 KV cache，并临时把真实语言层数设为 `1`。
4. 使用目标模型官方 `from_pretrained()` 创建模型；保持视觉与融合模块完整，只构造一个语言层，并使用适合分层调度的 attention 实现。
5. 将模型创建、常驻权重加载和加载结果校验放在同一个事务边界内。任一步失败都恢复快照中的全部属性；成功时恢复语言层数，并只保留后续动态加载或 Runner 明确需要的运行态设置。
6. 按当前模型的权重布局加载视觉模块、融合模块、首个语言层和后续流程所需的其他常驻权重。
7. 调用 `eval()`，检查常驻模块、首层和配置，然后返回模型。

## 模型相关边界

- 语言层数的位置可能是 `config.text_config.num_hidden_layers`、`config.llm_config.num_hidden_layers` 或其他路径，必须读取目标配置。
- 视觉模块不一定名为 `visual`，融合模块也可能独立存在；必须保证首个分段前向所需的非语言模块已加载。
- `from_pretrained()` 的模型类、dtype、`device_map`、attention 参数和 remote-code 行为必须与目标版本一致。
- 不要硬编码 `torch_dtype="auto"`；优先使用 Adapter 验证后的全局 dtype。
- 初始化固定在 CPU，Runner 负责执行时的模块设备调度；不得根据模型大小切换初始化位置或完整加载全部语言层。
- `_get_state_dict()` 和动态 decoder 加载不是 Pipeline V1 提供的通用方法；使用前必须由目标 Adapter 实现并与 safetensors 权重索引匹配。
- 常驻模块和动态 Decoder 都必须检查 `state_dict()` 中的持久 buffer；不得因只加载 parameter 而丢失 checkpoint-backed buffer。
- 临时修改共享 config 时必须逐对象、逐属性快照。至少覆盖实际修改的层数、`use_cache`、顶层及嵌套 `_attn_implementation`；目标模型还有 dtype、rope、专家数或其他临时开关时一并纳入。
- 顶层 config 的 setter 可能递归修改子 config。恢复时先恢复顶层，再恢复各子 config 的精确原值，不能假定它们原本相同。
- 异常恢复范围不能只包围 `from_pretrained()`；其后的 state dict 加载、missing/unexpected keys 校验和常驻模块检查失败时也必须回滚。
- 区分临时值与成功后的目标运行态。例如层数必须恢复；cache 是否保持关闭、attention 是否保持 `eager` 必须由目标源码和 Runner 契约决定，不得把所有属性无条件恢复或无条件保留。
- 目标模型无法通过临时设置语言层数为 `1` 完整构造视觉、融合和首个语言层时，报告超出当前 skill 范围，不增加另一套加载策略。

## Qwen 风格模板

当目标模型确认采用“完整视觉部分 + 首个语言层 + 后续按需加载”策略时，参考 `assets/qwen-style-adapter/target_vlm/model_adapter.py` 中的 `init_model()`。必须替换或验证：

- 目标模型类及依赖版本；
- 语言层数、KV cache 和 attention 配置路径；
- 初始化期间所有配置修改的快照、成功状态与异常回滚边界；
- 本地路径安全校验；
- dtype 与 CPU 初始化；
- 常驻权重筛选和 `_get_state_dict()`；
- `generate_decoder_layer()` 是否能加载其余所有层。

动态语言层不是全部从零实现：访问生成器与 Runner 使用当前框架能力，权重索引、分片加载和 Decoder 创建使用本 Skill 示例。具体边界见[动态 Decoder 与权重加载](dynamic-decoder-loading.md)。
