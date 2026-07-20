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

`

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

如果一个数据集提供了多个可用任务，但输入中没有指定具体使用哪一个，不要只按任务名称猜测。应先排除明显不适合当前评测场景的任务，再在剩余任务中选择更通用、更稳定的注册名。

### 1. 先判断任务是否可用于当前服务

优先选择当前推理服务能够直接处理的任务。对于 LLM 文本任务，通常只需要确认任务是文本输入、文本输出。对于 VLM 任务，还需要额外确认图片输入方式是否匹配服务能力：

- 如果任务把图片路径传给服务，必须确认推理服务能访问评测机上的图片文件路径。
- 如果任务把图片转成 base64 传给服务，通常更适合服务化评测，因为服务不需要访问评测机文件系统。
- 如果任务名称或说明标明是某类模型专用，例如 GLM-4V 专用，只有在用户模型属于该类时才优先选择。

不满足服务输入能力的任务，即使指标或 few-shot 设置看起来更合适，也不应作为默认选择。

### 2. 再按评测目标选择任务形态

在可用任务中，按以下顺序选择更符合自动生成配置的注册名：

| 优先级 | 条件 | 说明 |
|--------|------|------|
| 1 | 评估指标符合用户目标 | 如果用户要看准确率，优先选择 README 中评估指标包含 `accuracy` 或等价准确率指标的任务。 |
| 2 | 生成式任务优先 | 服务化大模型评测通常走生成接口，因此 `gen` 或 `generation` 任务优先于判别式、打分式任务。 |
| 3 | prompt 格式更通用 | LLM 文本任务优先选择字符串或 chat prompt；VLM/LMM 任务允许列表格式 prompt，因为它需要同时包含文本和图片。 |
| 4 | 0-shot 优先 | 未指定 few-shot 时，优先选择 0-shot，减少样例拼接、上下文长度和数据集差异带来的额外变量。 |
| 5 | 避免依赖额外裁判模型 | 如果存在普通规则评测与 `llmjudge` 两种任务，默认避开 `llmjudge`，除非用户明确要求。 |

### 3. VLM 的额外默认规则

VLM 数据集的注册名选择应优先保证图片输入链路可用。若 README 同时提供 base64 与图片路径两类任务，且用户没有指定输入方式，默认优先 base64 任务。只有当用户明确说明服务端可以访问图片路径，或参考配置已经使用路径方式并可运行时，才选择路径任务。

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
- `glm4v_textvqa_gen_base64` - GLM-4V/GLM-4.1V Thinking 模型专用，用于适配该类模型的特殊输出格式

根据选择策略，如果用户只说评测 `textvqa` 且未指定图片输入方式，优先选择 `textvqa_gen_base64`。
