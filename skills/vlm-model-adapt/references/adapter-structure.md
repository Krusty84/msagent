# 适配器结构与基类选择

## 最小文件结构

```text
msmodelslim/model/<adapter_key>/
├── __init__.py
├── model_adapter.py
└── loader.py

config/config.ini
```

- `__init__.py` 必须保留，允许为空。
- `model_adapter.py` 定义 Adapter 类并实现模型相关行为。
- `loader.py` 继承 `BaseModelAdapterLoader`，通过 `ADAPTER_CLASS_PATH` 延迟定位 Adapter 类。
- 需要时可增加辅助文件。

## Loader

默认使用当前仓库推荐的 Loader 注册方式：

```python
from msmodelslim.model.plugin_factory.base_loader import BaseModelAdapterLoader


class TargetVLMAdapterLoader(BaseModelAdapterLoader):
    ADAPTER_CLASS_PATH = (
        "msmodelslim.model.target_vlm.model_adapter:TargetVLMModelAdapter"
    )
```

Loader 负责依赖预检查、延迟导入和 Adapter 实例化，不负责模型权重加载或模型前向。`PluginModelFactory` 仍兼容 entry point 直接指向 Adapter 类，但新适配默认使用 Loader。

## 注册

三个 section 使用相同的 `<adapter_key>`：

```ini
[ModelAdapter]
target_vlm = Target-VLM-Model-Name

[ModelAdapterEntryPoints]
target_vlm = msmodelslim.model.target_vlm.loader:TargetVLMAdapterLoader

[ModelAdapterDependencies]
target_vlm = {"transformers": "==<verified-version>"}
```

仅在有实际版本证据时填写依赖约束。修改 `config/config.ini` 后重新安装 msModelSlim，因为安装过程会把配置转换为 Python entry point。

## 三种模型标识

| 标识 | 来源 | 用途 |
|------|------|------|
| Transformers 架构类型 | `config.model_type` | 定位 Transformers 实现和分析模型结构 |
| msModelSlim 注册名称 | 上层参数 `model_type` | 作为 `[ModelAdapter]` 右侧别名，由工厂选择并传给 Adapter |
| `adapter_key` | 按目标架构和仓库惯例确定 | 作为三个注册 section 的左值，并统一目录和 Loader 路径 |

遵守以下规则：

1. 优先使用上层传入的注册名称；`get_model_type()` 返回 `self.model_type`。
2. 注册名称缺失时，从模型目录名、`_name_or_path`、`architectures` 和官方标识推断；候选不唯一或覆盖范围不明确时再询问上层。不要用 `config.model_type` 冒充具体注册名称。
3. 三个注册 section、Adapter 目录和 Loader 路径必须使用同一个 `adapter_key`。

## 基类选择

基类由模型结构、模态和需要复用的行为决定，不由模型品牌决定。

1. 视觉语言理解模型默认继承 `VLMBaseModelAdapter`。
2. 纯文本 Transformers 模型可继承 `TransformersModel`，或在需要复用完整通用 Adapter 行为时继承 `DefaultModelAdapter`。
3. 再根据量化、敏感层分析和算法需求组合 `ModelSlimPipelineInterfaceV1` 等接口。

本 skill 只创建 Dense VLM Adapter。

VLM 的最小声明形态为：

```python
from msmodelslim.model.common.vlm_base import VLMBaseModelAdapter
from msmodelslim.model.interface_hub import ModelInfoInterface, ModelSlimPipelineInterfaceV1


class TargetVLMModelAdapter(
    VLMBaseModelAdapter,
    ModelInfoInterface,
    ModelSlimPipelineInterfaceV1,
):
    ...
```

不要因为目标属于 Kimi、DeepSeek、Qwen 等系列而推断基类；必须读取目标模型结构和当前仓库中对应版本的正式 Adapter。

类定义与能力接口的组合规则见[能力接口组合](capability-interfaces.md)。

## 模型谱系与最佳实践匹配

`get_model_pedigree()` 和 `get_model_type()` 是 LLM 与 VLM 共用的模型信息方法，不是 VLM 特有接口。本 skill 要求产出的 Adapter 必须继承 `ModelInfoInterface` 并同时实现两个方法，但不要把该接口误解为量化算法或实践 YAML 内容：

- msModelSlim 普通量化未传入 `config_path` 时，使用谱系键定位实践分组，再用具体模型名称、量化类型与标签筛选已有 YAML。

实现具体 Adapter 时，按以下规则确定谱系键：

1. 检查目标版本的现有 Adapter、官方 `lab_practice/`、自定义和插件实践仓及团队命名约定。
2. 若目标模型与已有谱系明确共享结构和实践，复用该稳定键；不要因为名称相似就跨结构复用。
3. 若是新模型家族且尚无实践目录，按仓库命名约定定义新的稳定键。为后续实践提供明确分组。
4. 报告该键当前属于“已有实践可读”还是“新分组，暂无实践”。后者不阻塞 Adapter 或显式 YAML 量化，但在添加首份实践前不能进行自动读取。

skill 和通用模板不得写死某个模型的谱系值；YAML 文件名也不必等于谱系键。具体 Adapter 可以返回已确认的固定键，也可以在一个 Adapter 覆盖多个谱系时使用显式映射。

使用：

```python
from msmodelslim.model.interface_hub import ModelInfoInterface


class TargetVLMModelAdapter(
    VLMBaseModelAdapter,
    ModelInfoInterface,
    ModelSlimPipelineInterfaceV1,
):
    def get_model_pedigree(self) -> str:
        return "<stable-model-pedigree>"

    def get_model_type(self) -> str:
        return self.model_type
```

`handle_dataset()` 的详细规则见[多模态数据处理](handle-dataset.md)。
