# 模型实现代码缺失的降级处理

> **章节归属**：本文档对应 SKILL.md §2 / §9.2；降级流程末尾的"报告标注"插入到报告 **Ch0 末尾**（或无 Ch0 时插入到 Ch1 开头）。

仅当 HF 仓库与本地 transformers 都不提供模型实现代码时，才回退到仅凭 `model.safetensors.index.json` 逆向。

## HF 仓库缺 `modeling_*.py`

部分厂商（如 MiniMax、GLM/Z.ai、部分 sglang/vllm-first 发布）不在 HF 模型仓库发布 `modeling_*.py`。此时**先试本地 transformers 仓库**，再才回退 weight-map-only 逆向：

1. 定位项目本地 transformers 仓库，默认 `<workspace>/transformers`，缺失时检查同级路径如 `../transformers`；本地没有则 `git clone -b main --single-branch --depth 1 https://github.com/huggingface/transformers.git` 下载源码
2. 用 `config.json` 的 `model_type` 定位 `transformers/src/transformers/models/<model_type>/`；同时试规范化变体（`-` → `_`），需要时 grep：`configuration_<model_type>.py`、`modeling_<model_type>.py`、`modular_<model_type>.py`
3. 把匹配文件复制进下载的模型文件夹（`configuration_*.py`、`modeling_*.py`、`modular_*.py`、processor 文件若相关），让报告输入目录自包含
4. 读这些本地 transformers 文件作为算子流主来源；仍用 `model.safetensors.index.json` 交叉核对哪些层 / 额外模块（如 MTP 层权重）实际存在
5. 本地 transformers 也没有实现 → 回退 `model.safetensors.index.json` 的 `weight_map` 键逆向层结构（按层索引分组、识别不同子模块集合、从命名模式推断架构，如 `block_sparse_moe.experts.N.{w1,w2,w3}`、`self_attn.index_k_proj`）

## 降级后的报告标注

降级后必须在 Ch0 末尾加一行警告：

> ⚠️ 本报告基于 `model.safetensors.index.json` 权重名逆向推断算子流，未交叉验证 modeling 代码，请审阅时注意。
