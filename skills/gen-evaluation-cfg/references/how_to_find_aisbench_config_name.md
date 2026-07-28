# 查找 AISBench 注册名

## 概述

AISBench 注册名（`config_name`）是 AISBench 数据集配置的唯一标识符。每个数据集可能有多个可用的任务配置。

## 查找步骤

### 1. 定位 AISBench 安装路径

执行以下 Python 命令：

```bash
python -c "import ais_bench; print(ais_bench.__file__)"
```

输出示例：
```
/path/to/ais_bench/__init__.py
```

提取安装路径（去掉 `__init__.py`）：
```
/path/to/ais_bench
```

### 2. 找到数据集配置目录

数据集配置位于：

```
<ais_bench_path>/benchmark/configs/datasets/<数据集名称>
```

示例：
- GSM8K: `benchmark/configs/datasets/gsm8k`
- AIME25: `benchmark/configs/datasets/aime25`

### 3. 读取 README.md

在该目录下查找并阅读 `README.md` 文件。文件中包含一个可用数据集任务表格。

表格列通常包含：
- 任务名称（**这是合法的 ais_bench 注册名**）
- 评估指标
- 其他配置信息

## 多任务选择策略

如果一个数据集提供多个可用任务，但用户未指定具体任务，应先排除输入格式与当前模型或推理服务不兼容的任务，按以下优先级选择：

| 优先级 | 条件 |
|--------|------|
| 1 | 评估指标符合用户目标 |
| 2 | 生成式任务（gen 或 generation）优于其他任务类型 |
| 3 | 0-shot（zero-shot）优于 few-shot |
| 4 | 避免使用 `llmjudge` |

对于 VLM，若同时提供 base64 和图片路径两种任务，默认优先选择 base64；仅当推理服务能够访问图片路径时才选择路径任务。模型专用任务仅用于对应模型或具有相同特殊输出格式的模型。

## 示例

### GSM8K

执行步骤：

```bash
# 1. 获取 ais_bench 路径
python -c "import ais_bench; print(ais_bench.__file__)"
# 输出: /opt/conda/lib/python3.10/site-packages/ais_bench/__init__.py

# 2. 查找 GSM8K 配置目录
cd /opt/conda/lib/python3.10/site-packages/ais_bench/benchmark/configs/datasets/gsm8k

# 3. 读取 README.md
cat README.md
```

README.md 任务表格可能包含：
- `gsm8k_gen_0_shot_cot_str` - 推荐（生成式、0-shot、字符串、accuracy）
- `gsm8k_gen_0_shot_cot_llmjudge` - 不推荐（使用 llmjudge）

根据选择策略，优先选择 `gsm8k_gen_0_shot_cot_str`。

### AIME25

类似地，查找 `benchmark/configs/datasets/aime25/README.md`，可能获得：
- `aime2025_gen_0_shot_chat_prompt` - 优先（生成式、0-shot、chat 模板）
- `aime2025_gen_0_shot_chat_llmjudge` - 不推荐

### TextVQA

类似地，查找 `benchmark/configs/datasets/textvqa/README.md`，可能获得：
- `textvqa_gen` - 图片以文件路径传入服务化；仅当推理服务能够访问评测机上的图片路径时使用
- `textvqa_gen_base64` - 图片转为 base64 后传入服务化；默认优先，适合服务化评测
- `glm4v_textvqa_gen_base64` - GLM-4V 模型专用，用于适配该类模型的特殊输出格式

根据选择策略，如果用户只说评测 `textvqa` 且未指定图片输入方式，优先选择 `textvqa_gen_base64`。
