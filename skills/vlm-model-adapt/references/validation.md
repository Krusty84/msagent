# Adapter 自身验收

按“静态契约 → 服务器真实前向”执行。只有两层全部通过，才能报告 Adapter 自身验收通过；量化产物验证和敏感层/调优不属于本 skill。

## 静态契约

- 按[完整示例与装配顺序](complete-example-and-assembly.md)执行成品清零门禁，目标模型目录中不得残留模板占位符。
- 确认 Adapter 实现 Pipeline V1 的五个抽象方法以及 `ModelInfoInterface` 的两个模型信息方法，类的 `__abstractmethods__` 为空。
- 确认 Loader 可导入 Adapter，`config.ini` 的模型别名、Loader 入口和依赖约束共用同一个注册键。
- 确认 `handle_dataset()` 的输出字段与 `generate_model_forward()` 的消费字段一一对应。
- 确认 `generate_model_visit()` 与 `generate_model_forward()` 的 `ProcessRequest` 名称、模块边界、数量和顺序一致。
- 对比目标 `modeling*.py`、Adapter 模块路径和 safetensors 索引，确认 Decoder 层数、名称、权重前缀及 checkpoint-backed 持久 buffer 的加载范围正确。
- 确认浮点 parameter/buffer 只按目标要求转换计算 dtype，整数、布尔和其他非浮点状态保持 checkpoint dtype。
- 确认 `enable_kv_cache()` 修改的是目标源码真正读取的配置对象，且分段前向没有用硬编码参数覆盖该状态。

## 服务器真实前向

使用服务器实际 Transformers 版本、目标权重和至少一条真实图像加文本样本，执行 Adapter 创建、`handle_dataset()`、`init_model()`、完整 visit 和完整 forward。用同一输入比较官方未拆分前向与 Adapter 分段前向的视觉输出、融合 embedding、position/mask、首层和末层 hidden states，并检查 shape、dtype、device 与数值误差。

只验证“不报错”不能通过。缺少服务器、权重或图文样本时，状态只能是“实现完成，真实前向待验”。
