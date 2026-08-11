# 模型实现定位

## 逐组件确定来源

分别确定 Config、Model、Processor 和 Tokenizer 的实现来源，不把整个模型简单判定为“官方”或“自定义”。先静态读取模型目录中的 `config.json`、`processor_config.json`、`preprocessor_config.json` 和 `tokenizer_config.json`，记录 `model_type`、`architectures`、`auto_map` 及依赖版本。

对每个组件执行以下顺序：

1. 优先检查服务器实际安装版本的 Transformers 是否提供兼容实现。
2. 若不存在或与配置、权重、调用签名不兼容，再按 `auto_map` 查找模型目录中的精确自定义类；之后才搜索同目录相关 `configuration*.py`、`modeling*.py`、`processing*.py`。
3. 两边都不存在时，从网络搜索与服务器实际安装版本完全一致的 Transformers 官方源码；优先使用官方仓库对应 tag、release 或该版本文档，并记录 URL、版本/tag 和目标文件。找不到精确版本源码时停止并报告，不用其他版本或相近模型猜写。

## Transformers 实现

优先通过实际类和 `inspect.getfile()` 定位当前运行文件，而不是根据包结构猜路径：

```python
import inspect

source_file = inspect.getfile(TargetModelClass)
```

检查 `trust_remote_code=False` 下 AutoConfig、目标 AutoModel 类和 AutoProcessor/Tokenizer 的解析结果；仅“存在同名类”不足以证明兼容，还要核对配置字段、forward 签名、模块结构和权重命名。记录 Transformers 的准确版本。

## 本地自定义实现

优先读取 `auto_map` 指向的类及其直接依赖，完整追踪真实 `forward()`。先静态审查代码和依赖，再决定是否需要执行 remote code。模板必须传递 `self.trust_remote_code`，不得硬编码为 `True`；需要自定义代码但未获允许时，报告风险和所需开关。

## 失败报告

至少报告：模型目录、`model_type`、`architectures`、`auto_map`、Transformers 版本、尝试解析的 Auto 类、搜索过的本地文件，以及缺失或不兼容的具体组件。

Pipeline、Runner、Loader 和权重加载骨架以本 Skill 的 `assets/` 与 `references/` 为起点，并可参考当前 msModelSlim 已支持的 Dense VLM Adapter 核对公共接入方式。不得直接复制参考 Adapter，也不得将其模型相关逻辑作为目标语义依据。目标代码行为与注释冲突时，以实际执行代码为准。
