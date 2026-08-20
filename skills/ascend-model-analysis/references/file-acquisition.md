# 文件获取：本地目录与 HuggingFace 下载

> **章节归属**：本文档对应 SKILL.md §2 文件获取阶段。下载到的文件被 Ch1（架构识别）、Ch2（算子流）、Ch3（参数量/KVCache）共同消费。

## 来源与核对

两种来源。选定后核对文件齐全与否，缺口向用户说明风险。

- **本地目录**：核对 `config.json`、`modeling_*.py`（或 `model.py` / `kernel.py`）、`README.md`、多模态时还需 `processing_*.py`
- **HuggingFace**：先 WebSearch 定位 HF 仓库（官方 org 优先，多候选列出让用户确认），再走下方下载流程

## 从 HuggingFace 下载

### 策略

- 将所有文件下载到一个新的目录（如 `<model-name>`）
- 仅下载文本小文件，**绝不下载 safetensors 权重分片**（几十到几百 GB，架构分析用不到）；唯一例外是 `model.safetensors.index.json`（几 MB 的 JSON 索引）
- 仓库根目录全部 `.py` 一律下载（不必逐一甄别哪个是模型实现）
- JSON/MD 等其余小文件按下表优先级

### 文件优先级

| 文件 | 用途 | 优先级 |
| --- | --- | --- |
| `config.json` / 配置类 `.py` | 架构分析核心输入：所有层超参（hidden_size、层数、头数、MoE/attention 配置）；配置类 `.py` 额外说明自定义 config 键的含义与默认值 | 必下 |
| `model.safetensors.index.json` | 权重名清单（weight_map）：交叉校验架构判断，如 gate_up_proj 是否 packed、实际专家数 | 必下 |
| `README.md` | Model Card：官方总参数、上下文长度、量化版本、部署命令，与 config/代码推导的数字对账 | 必下 |
| `modeling_*.py` | 前向传播实现，**算子流的唯一权威来源** | 必下 |
| `processing*.py` / `image_processor.py` / `video_processor.py` | 多模态预处理器实现 | 必下 |
| `kernel.py` | 自定义算子内核（如 lightning attention） | 必下 |
| `tokenization_*.py` | 分词器实现 | 必下 |
| `generation_config.json` | 生成默认参数与推测解码配置 | 有用 |
| `tokenizer_config.json` | 分词器配置 | 有用 |
| `preprocessor_config.json` | 多模态预处理配置（图像分辨率等） | 有用 |
| `chat_template.jinja` | 对话模板（prompt 格式） | 有用 |

### 下载命令

先用 HF tree API 列出仓库文件清单（`tree` 返回的就是仓库真实存在的文件——按这个清单逐个下载，不要假设"必下"文件一定存在）：

```bash
curl -sL "https://huggingface.co/api/models/<org>/<model>/tree/main"  # 返回的就是真实清单
mkdir -p <model> && cd <model>
curl -sL -o config.json "https://huggingface.co/<org>/<model>/resolve/main/config.json"
# 其它文件按 tree 输出逐个下，没有就跳过
```

## 镜像源（HF 不可达时）

任选一个替换 `huggingface.co`：

- `https://hf-mirror.com/<org>/<model>/resolve/main/<file>` （最常用）
- `https://www.modelscope.cn/<org>/<model>/resolve/master/<file>` （阿里达摩院，路径非 HF 风格）

全部失败则走用户本地下载 + 粘贴文件内容的降级路径（见 SKILL.md §9.1）。
