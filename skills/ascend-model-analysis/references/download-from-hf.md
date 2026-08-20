# 从 HuggingFace 下载模型文件

## 下载策略

- 将所有文件下载到一个新的目录（如 `<model-name>`）
- 仅下载文本小文件，**绝不下载 safetensors 权重分片**（几十到几百 GB，架构分析用不到）；唯一例外是 `model.safetensors.index.json`（几 MB 的 JSON 索引）
- 仓库根目录全部 `.py` 一律下载

## 常见文件清单

按仓库实际目录树拉取，没有就跳过。

### 必查（Always）

| 文件 | 用途 |
| --- | --- |
| `config.json` | 架构分析核心输入：所有层超参（hidden_size / 层数 / 头数 / MoE / attention 配置） |
| `model.safetensors.index.json` | 权重名清单（weight_map）：用 keys 校验实际权重名；分片 checkpoint 才有 |
| `README.md` | Model Card：与 config/代码推导的数字对账 |
| 全部 `.py`（`configuration_*.py` / `modeling_*.py` / `processing*.py` / `image_processor.py` / `video_processor.py` / `kernel.py` / `tokenization_*.py`） | 承载架构实现，算子流的权威来源 |

### 可选（Often useful）

| 文件 | 用途 |
| --- | --- |
| `generation_config.json` | 生成默认参数与推测解码配置 |
| `tokenizer_config.json` | 分词器配置 |
| `preprocessor_config.json` | 多模态预处理配置（图像分辨率等） |
| `chat_template.jinja` | 对话模板（prompt 格式） |

## 下载命令

先用 HF tree API 列出仓库文件清单（`tree` 返回的就是仓库真实存在的文件——按这个清单逐个下载，不要假设"必下"文件一定存在）：

```bash
curl -sL "https://huggingface.co/api/models/<org>/<model>/tree/main"  # 先列仓库真实文件清单

mkdir -p <model> && cd <model>
for f in config.json README.md model.safetensors.index.json \
         configuration_*.py modeling_*.py processing*.py \
         image_processor.py video_processor.py kernel.py \
         generation_config.json tokenizer_config.json preprocessor_config.json \
         chat_template.jinja; do
  curl -sL -o "$f" "https://huggingface.co/<org>/<model>/resolve/main/$f"
done
```
