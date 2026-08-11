# 动态 Decoder 与权重加载

## 可复用层次

- 直接复用框架：`VLMBaseModelAdapter` 的配置、dtype 和输入收集，`generated_decoder_layer_visit_func()` 的访问请求，LayerWise Runner 的调度与设备管理，以及 `BaseModelAdapterLoader`。
- 复用 `assets/qwen-style-adapter/target_vlm/model_adapter.py` 中 `_get_weight_map()`、`_get_state_dict()`、`_load_decoder_if_not_exist()` 和 `generate_decoder_layer()` 的骨架。
- 必须按目标源码实现：Decoder 类与构造参数、层数配置路径、层容器路径、模块名前缀、权重前缀、加载状态判断及特殊 attention/dtype 参数。

当前这些权重辅助方法在多个 Adapter 中有相似实现，但尚不是 `VLMBaseModelAdapter` 提供的公共方法；可以复制验证后的骨架，不能假定继承后自动存在。

## 最小调用链

```text
generate_model_visit()/generate_model_forward()
  -> generate_decoder_layer(model)
     -> _load_decoder_if_not_exist(model, name, idx)
        -> 创建目标 Decoder
        -> _get_state_dict(decoder, prefix=name)
           -> _get_weight_map()
        -> load_state_dict + dtype/eval
        -> 插入或替换目标 ModuleList
```

Qwen 风格代码骨架见 `assets/qwen-style-adapter/target_vlm/model_adapter.py`。

## 权重边界

- 优先读取 `model.safetensors.index.json`；无索引的单文件或小模型可枚举 safetensors key 构建映射。
- 以 `module.state_dict()` 的键集确定加载范围，同时覆盖 parameter 和持久 buffer；不要只遍历 `named_parameters()`。非持久运行时 buffer 不应从 checkpoint 加载。
- 只把浮点 tensor 转换到全局模型 dtype；整数、布尔和其他非浮点 parameter/buffer 必须保持 checkpoint dtype。
- 严格核对 prefix、missing/unexpected keys 和 dtype；不要把空 state dict 当作成功。
- 使用当前安全路径和文件大小校验工具，保持权重在 CPU 上读取，并与 Runner 的设备策略配合。
- 判断现有层是否可用时同时考虑模块不存在、参数位于 meta device、参数未物化和特殊延迟加载结构。
- 首层已在 `init_model()` 加载时不得重复创建。

## Buffer 与导出

对比目标模型的 `named_buffers()`、`state_dict()` 和 safetensors 索引，分清三类对象：

- checkpoint-backed 持久 buffer：必须被动态加载，并通过 forward 对齐验证。
- 运行时可重建的非持久 buffer：保持模型原实现，不从 checkpoint 强行加载。
- forward 和量化产物都必须保留、但当前 msModelSlim 导出链路不保存的 buffer：在 Adapter 中将它精确替换为同名、`requires_grad=False` 的 `nn.Parameter`，再验证保存和重载。

转换前必须确认目标 forward 只依赖 tensor 语义，不依赖它仍被注册为 buffer。不要批量转换 rotary/cache 等可重建运行时状态。

## 最低检查

检查索引存在与缺失、权重跨多个分片、prefix 命中与缺失、已加载层、meta 层、缺失层、ModuleList append/replace、dtype 转换和零层行为。随后在服务器遍历所有 Decoder，确认层数、名称、权重前缀和顺序完整无重复。
