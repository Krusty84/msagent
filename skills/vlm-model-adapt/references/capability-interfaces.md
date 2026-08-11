# 能力接口组合

## 组合原则

先确定目标 Adapter 必须支持的服务和量化配置，再组合基类与接口。不要复制相近模型的全部父类，也不要先继承接口、后用空实现绕过抽象方法。

对于当前 VLM V1 分层量化流程，按“具体模型基类、通用信息接口、调度接口、经需求证明的可选算法接口”排列。

```python
@logger_setter()
class TargetVLMModelAdapter(
    VLMBaseModelAdapter,
    ModelInfoInterface,
    ModelSlimPipelineInterfaceV1,
):
    ...
```

从 `assets/qwen-style-adapter/target_vlm/model_adapter.py` 复制声明骨架，并完成保留接口的全部契约。

## 各父类与接口的职责

- `VLMBaseModelAdapter`：VLM 默认基类，提供模型路径与配置初始化、全局 dtype、`_collect_inputs_to_device()` 等共性能力；它不能替代目标模型的 processor、融合或 forward 逻辑。
- `ModelInfoInterface`：LLM 与 VLM 共用的最佳实践路由接口，提供 `get_model_pedigree()` 与 `get_model_type()`。它用于查找已有实践，不实现量化算法本身。本 skill 产出的 Adapter 必须组合并完整实现该接口；谱系键的确定规则见 `adapter-structure.md`。
- `ModelSlimPipelineInterfaceV1`：LLM 与 VLM 共用的分层调度接口，不是 VLM 专属接口。本 skill 面向的 VLM V1 基础量化和敏感层分析流程实现其五个抽象方法。
- 其他算法接口：根据目标 YAML、处理器能力检查和当前接口定义逐项加入，不因参考 Adapter 已继承就自动加入。

`@logger_setter()` 用于绑定日志上下文，但它不是模型能力接口，也不决定 Adapter 是否可实例化。

## 命名与导入

- Adapter 类名应清晰表达目标结构，通用 skill 不写死该名称。
- 使用目标分支当前公开导入路径，并检查安装后的导出是否一致。
- 类定义完成后检查 `__abstractmethods__`，确保没有因拼写错误或漏实现而残留抽象方法。
