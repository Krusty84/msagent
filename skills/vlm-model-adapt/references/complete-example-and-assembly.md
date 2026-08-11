# 完整示例与装配顺序

本页用于把 `assets/` 中的完整目录模板改造成一个可运行的 Dense 图文 VLM Adapter。示例只提供 msModelSlim 接入方式，目标模型的 processor、模块边界与 forward 语义仍以目标版本源码为准。

## Skill 内装配素材

以本 Skill 的目录模板作为待修改骨架：

```text
assets/qwen-style-adapter/
├── target_vlm/
│   ├── __init__.py
│   ├── model_adapter.py
│   └── loader.py
└── config.ini
```

`model_adapter.py` 集中展示类定义、数据处理、事务式初始化、动态 Decoder、访问序列、分段前向和 KV cache；`loader.py` 展示延迟导入；`config.ini` 展示三项注册。它们只定义 msModelSlim 接入形态，目标模型语义仍须从目标运行版本源码重新推导。

可以阅读当前 msModelSlim 已支持的 Dense VLM Adapter，以核对公共接口、Runner、Loader、注册和测试的当前写法；不得直接复制参考 Adapter，也不得用其 processor、forward、模块路径或融合逻辑替代目标模型源码。

将 `target_vlm/` 复制为 `msmodelslim/model/<adapter_key>/`，并把示例 `config.ini` 的内容替换占位符后合并到目标仓库的全局 `config/config.ini`。

不得从该示例复制下列与目标模型无关的内容：

- Qwen 专属的类名、模块路径、权重前缀、视觉 token、mRoPE、DeepStack、mask 或融合细节，除非目标版本源码证明目标模型使用完全相同的契约。


## 装配顺序

按以下顺序形成目标 `model_adapter.py`，每一步都用目标 Transformers 或自定义 `modeling*.py` 复核：

1. 目标版本可导入的标准库、Transformers 类和 msModelSlim 接口。
2. `@logger_setter()`、目标 Adapter 类名，以及仅包含 `VLMBaseModelAdapter`、`ModelInfoInterface`、`ModelSlimPipelineInterfaceV1` 的基础继承列表。
3. `__init__()` 和确有必要的目标模型状态。
4. `get_model_pedigree()` 与 `get_model_type()`。
5. `handle_dataset()`。
6. `init_model()`。
7. `generate_model_visit()`。
8. `generate_model_forward()`。
9. `enable_kv_cache()`。
10. safetensors 权重索引、参数与持久 buffer 收集、Decoder 构造/加载/生成 helper。
11. 由目标 forward 调用链证明必需的模型专属 helper。
12. `loader.py`、`config/config.ini` 三项注册和服务器真实图文验证。

装配时可以复用 Skill 内模板的结构，但必须重新推导并替换下列内容：Model/Processor/Decoder 类、文本层配置路径、视觉与融合模块边界、输入字段、位置编码、attention mask、跨层视觉状态、语言层调用参数、输出解包、权重名称和 Decoder 前缀。

## 成品清零门禁

`assets/` 是允许保留占位符的教学片段；下面的检查只针对最终生成的目标模型目录。交付前搜索目标 Adapter、Loader、配置和测试，结果必须不包含：

```text
TARGET_
TargetVLMModelAdapter
resolve_target_model_class
build_target_decoder
get_target_text_layers
_prepare_text_layer_inputs
_validate_init_state_dict_result
NotImplementedError
```

同时完成以下结构检查：

- `handle_dataset()`、`init_model()`、`generate_model_visit()`、`generate_model_forward()`、`enable_kv_cache()`、`get_model_pedigree()` 和 `get_model_type()` 均为目标类的实际实现。
- 运行时目标类的 `__abstractmethods__` 为空。
- Loader 可导入目标类，`config.ini` 的模型别名、Loader 入口和依赖约束使用同一个注册键。
- visit 与 forward 的请求名称、顺序和模块边界一致。
- 不存在本 skill 排除的 MoE 接口与辅助代码。

可在服务器仓库根目录将 `<target-model-dir>` 替换为实际目录后执行静态门禁：

```bash
rg -n 'TARGET_|TargetVLMModelAdapter|resolve_target_model_class|build_target_decoder|get_target_text_layers|_prepare_text_layer_inputs|_validate_init_state_dict_result|NotImplementedError' <target-model-dir>
```

命令无输出才通过占位符检查；它不能替代 import 和真实前向验证。
