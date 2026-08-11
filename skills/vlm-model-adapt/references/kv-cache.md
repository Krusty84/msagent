# KV cache 控制

## 接口契约

`enable_kv_cache()` 是 `ModelSlimPipelineInterfaceV1` 的抽象方法。

Runner 根据所有 processor 的 `need_kv_cache()` 结果调用：

```python
adapter.enable_kv_cache(model, need_kv_cache)
```

关闭 cache 通常可降低校准内存；若 processor 明确需要 cache，Adapter 必须真正开启，不能只修改一个无人读取的配置属性。

## 确定真实配置对象

不要把以下两条写成可任选的通用模板：

```python
model.config.use_cache = need_kv_cache
model.model.config.use_cache = need_kv_cache
```

根据目标版本 `modeling*.py` 中实际执行的逻辑确定配置对象。搜索 `use_cache` 的默认值解析，例如：

```python
use_cache = use_cache if use_cache is not None else self.config.use_cache
```

追踪这里的 `self` 是顶层模型、语言模型还是内部 decoder。常见但不通用的路径包括：

- `model.config.use_cache`；
- `model.config.text_config.use_cache`；
- `model.config.llm_config.use_cache`；
- `model.language_model.config.use_cache`；
- `model.model.language_model.config.use_cache`。

某些 Transformers 包装层共享同一个 config 对象，此时多个路径可能暂时等价；不要依赖对象共享。`PretrainedConfig` 可能允许添加任意属性，修改错误路径可能不报错却完全无效。

## Qwen 风格模板

本 Skill 的 Qwen 风格模板默认修改顶层配置：

```python
def enable_kv_cache(self, model: nn.Module, need_kv_cache: bool) -> None:
    model.config.use_cache = need_kv_cache
```

参考 `assets/qwen-style-adapter/target_vlm/model_adapter.py` 中的 `enable_kv_cache()`，但必须对照目标模型源码验证。不要因为 `VLMBaseModelAdapter._enable_kv_cache()` 当前访问 `model.model.config` 就假定该路径适用于新 VLM。

对于实际从独立语言配置读取 cache 的模型，修改对应配置。若顶层和语言层持有不同配置副本且都会参与执行，只同步源码证明需要同步的对象，不要盲目遍历所有 config。

## 与分段前向保持一致

检查 `generate_model_forward()` 是否把 `use_cache=False`、`past_key_values=None` 等值硬编码进 decoder kwargs。显式参数通常会覆盖 config；如果 Runner 需要开启 cache，分段前向也必须传播 `need_kv_cache` 对应状态。若 Adapter 明确不支持需要 cache 的 processor，应报告不支持，而不是假装切换成功。

## 验证

分别调用 `enable_kv_cache(model, False)` 和 `enable_kv_cache(model, True)`，然后：

1. 断言 `modeling*.py` 真正读取的配置对象发生变化。
2. 用最小前向或测试替身确认 decoder 收到的 `use_cache` 与请求一致。
3. 确认关闭 cache 时不创建或返回 KV cache；开启时按目标模型约定创建或返回。
4. 检查 Adapter 类不再包含未实现的 Pipeline V1 抽象方法。
