# 适配器注册指南

在 `config/config.ini` 中注册模型与入口。

## 示例

```ini
[ModelAdapter]
my_model = MyModel-7B, MyModel-13B

[ModelAdapterEntryPoints]
my_model = msmodelslim.model.my_model.model_adapter:MyModelAdapter
```

注册完成后，务必执行 `bash install.sh` 安装更新。

> **注意**：禁止使用 `pip install -e .`、`python setup.py install` 或其他任何替代命令。唯一正确的安装方式是 `bash install.sh`。
