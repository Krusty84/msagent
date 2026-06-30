---
name: msmodeling-optix-param-recommend
description: 当首次使用 msmodeling optix 的用户需要根据硬件、模型、负载和优化目标推荐 MindIE/vLLM 寻优参数、搜索范围、benchmark 侧字段或 config.toml 片段时使用。
version: 0.1.0
source: local-session-analysis
---

# 参数范围推荐

## 目标

为首次使用 `msmodeling optix` 的用户推荐保守、可解释的 MindIE / vLLM 寻优参数和初始搜索范围。用户不清楚应该调哪些参数、参数范围如何设置、benchmark 参数如何配置时，使用本 skill。

## 必须遵循的流程

缺少必需信息时，不要输出最终参数范围。

**优先级链**：

```text
历史经验（references/historical-configs.md） > 通用启发式规则 > 脚本默认值
```

0. **必须先检查历史经验**：在收集上下文和运行脚本之前，先打开 `references/historical-configs.md`，逐条比对用户提供的模型、硬件、负载是否命中已有经验配置。命中时：
   - 经验的固定参数直接采用，不纳入搜索。
   - 经验的搜索维度范围和候选沿用经验推荐值。
   - 经验的计算公式替换通用计算逻辑。
   - 超出经验适用边界的部分才回退到脚本推荐。
   - 如果经验中有 `historical_exp_id`，写入 context JSON 供脚本识别。
   - **严禁**跳过历史经验检查直接运行脚本——脚本输出是通用推断，不包含实战验证过的 MTP 参数、实测显存利用率、bench 命令骨架等关键信息。
1. 收集必需上下文：
   - 推理框架：`mindie` 或 `vllm`
   - 硬件信息：`single_card_mem_gb`、`world_size`、`num_per_nodes`、`num_nodes`
   - 模型信息：本地 `config.json` 路径，或显式模型结构字段
   - 业务负载：平均/最大输入 token，平均/最大输出 token
   - 优化目标：`throughput`、`ttft`、`tpot` 或 `balanced`
2. **检查历史经验**：阅读 `references/historical-configs.md`，如果模型名、量化方式、平台等匹配已有经验条目，将匹配的 `historical_exp_id` 和关键参数写入 context。
3. 将已收集信息写入一个 JSON context 文件。
4. 执行 `skills/msmodeling-optix-param-recommend/scripts/recommend_params.py --context <context.json>`。
5. 如果脚本返回 `status: need_more_info`，只询问返回的 `next_question`，不要给最终范围。
6. 如果脚本返回 `status: ok`，再总结推荐结果并附上 TOML 片段。

如果用户没有指定 benchmark：vLLM 默认使用 `vllm_benchmark`（random 数据集免配置）；MindIE 默认使用 `ais_bench`。用户显式指定 `ais_bench` 时才使用 AISBench。

## Context 格式

最小 JSON 结构如下：

```json
{
  "engine": "vllm",
  "hardware": {
    "single_card_mem_gb": 64,
    "world_size": 8,
    "num_per_nodes": 8,
    "num_nodes": 1
  },
  "model": {
    "config_path": "/path/to/model/config.json"
  },
  "workload": {
    "input_len_avg": 1024,
    "input_len_max": 4096,
    "output_len_avg": 256,
    "output_len_max": 512
  },
  "target": "balanced",
  "discovery": {
    "enabled": false
  },
  "historical_exp_id": null,
  "historical_note": ""
}
```

完整输入说明见 `references/input-schema.md`。

## 推荐规则

核心并行约束是：

```text
DP * TP * PP == world_size
```

首次使用时，优先推荐在稳定可行的前提下用满卡：

```text
DP * TP * PP == world_size
```

脚本会根据模型 config、单卡显存、TP、dtype 和业务负载估算模型权重与 KV cache 容量，然后推荐：

- MindIE：`max_batch_size`、`max_prefill_batch_size`、`max_prefill_token`、排队/调度策略参数、`tp`、`dp`、MoE 专家并行参数、`ais_bench` 压测参数。
- vLLM：`MAX_NUM_SEQS`、`MAX_NUM_BATCHED_TOKENS`、`MAX_MODEL_LEN`、TP/PP/DP 参数、`GPU_MEMORY_UTILIZATION`、`BLOCK_SIZE`、前缀缓存、分块 prefill、编译配置、`vllm_benchmark`/`ais_bench` 压测参数。

`ENABLE_PREFIX_CACHING` 和 `ENABLE_CHUNKED_PREFILL` 默认交给 vLLM 自身的模型感知默认值，不作为搜索维度，也不写入启动命令。只有用户明确要求覆盖默认行为时，才把对应 flag 放进 `vllm.command.others`。

详细启发式规则见 `references/parameter-rules.md`。

## 历史经验匹配

在运行推荐脚本之前，先检查 `references/historical-configs.md` 中是否已有与当前场景匹配的经验配置。

### 匹配流程

1. **读取经验库**：阅读 `references/historical-configs.md`，获取所有已记录的经验条目。
2. **提取模型特征**：从用户提供的 `config.json` 中提取 `model_type`、`num_hidden_layers`、`hidden_size`，或从模型名中匹配已知模式（如 `Qwen3.5-27B`、`DeepSeek-V3` 等）。
3. **逐条匹配**：检查是否满足经验条目的匹配条件（模型名模式、量化方式、平台、硬件约束）。
4. **写入 context**：匹配成功时，将经验条目中的 `historical_exp_id`、推荐值写入 context；不完全匹配但部分相似时，引用经验条目中的公式和约束（如 `max-model-len` 计算公式）。

### Context 扩展字段

```json
{
  "historical_exp_id": "EXP-001",
  "historical_note": "匹配到 Qwen3.5-27B-W8A8 Ascend 经验配置，部分参数采用经验值"
}
```

### 匹配结果的三种处理方式

| 匹配程度 | 处理方式 |
|----------|----------|
| **完全匹配** | 直接将经验值写入 context 的固定参数；搜索范围参考经验的 `推荐搜索维度`。 |
| **部分匹配**（同模型族但规模不同） | 按比例调整 TP/显存相关参数；保留公式（如 `max-model-len` 计算规则）但数值重新计算。 |
| **无匹配** | 使用 `recommend_params.py` 脚本的通用启发式规则，不做经验覆盖。 |

### 经验覆盖原则

- 经验值 > 脚本启发式 > 通用默认值。
- 经验中的固定参数不参与搜索（除非用户明确要求）。
- 经验的适用边界必须检查：不满足边界条件的参数不采用经验值。
- 如果经验值和脚本推荐冲突，优先采用经验值并向用户说明来源和理由。

## 启动命令发现

如果用户提供 vLLM 启动命令，把它当作可运行命令骨架，不要当作完整搜索空间。可以执行只读帮助命令：

```bash
vllm serve --help
vllm bench serve --help
```

第一版只把发现到的额外参数作为可选候选。对 MindIE，优先读取 `config.json`，不要依赖命令行帮助。

如需启用可选发现功能，先执行只读 help 命令，将输出写入 context：

```json
{
  "discovery": {
    "enabled": true,
    "vllm_help_text_path": "/tmp/vllm-serve-help.txt"
  }
}
```

脚本会根据模型和负载决定是否追加 `MAX_NUM_PARTIAL_PREFILLS`、`LONG_PREFILL_TOKEN_THRESHOLD`、`DISABLE_CHUNKED_MM_INPUT`、`ENABLE_EXPERT_PARALLEL` 等候选。`result.discovery.added_parameters` 会列出本轮由 help 发现追加的字段。

vLLM 的 presence flag 参数分两类处理：`ENABLE_PREFIX_CACHING` 和 `ENABLE_CHUNKED_PREFILL` 使用 vLLM 默认值，不写入命令；`COMPILATION_CONFIG` 这类需要搜索的字符串枚举才用 `$COMPILATION_CONFIG` 占位符接入 `vllm.command.others`。

## 输出要求

脚本返回 `status: ok` 时，输出：

- 输入摘要和假设。
- 如仍有假设，明确标注。
- 如使用了历史经验，明确标注经验编号和来源。
- 推荐参数表：参数名、范围、默认值、是否参与搜索、推荐理由。
- `toml_snippet` 中的 `config.toml` 片段。
- `config_skill_handoff`：供 `auto_config.py` 使用的机器可读交接对象。
- `DP * TP * PP == world_size` 约束解释。
- `next_command` 中的下一步命令。

不要静默应用 TOML 片段。修改现有 `config.toml` 前必须征得用户同意。

## config.toml 配置管理

推荐完成后，使用 `auto_config.py` 将结果写入 `config.toml`。该脚本支持场景模板、参数增改、服务/测评工具配置等操作。

### 前置条件

1. **已完成 optix 安装**（推荐按 `docs/zh/user_guide/optix_user_guide.md` 在 OptiX 源码目录执行 `pip install -e .`）
2. **config.toml 文件已存在**（位于 `experimental/optix/config.toml`，也可通过 `-c` 参数指定其他路径）

### 场景模板配置

```bash
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --scenario <场景> --engine <引擎> [选项]
```

| Scenario | n_particles | iters | sample_size | 适用场景 |
|----------|-------------|-------|-------------|----------|
| `quick-test` | 5 | 3 | 100000 | 快速验证，小范围 |
| `standard` | 8 | 4 | 100000 | 标准优化，8×4=32 迭代 |
| `deep-optimize` | 30 | 20 | 300000 | 深度搜索，启用 `pso_top_k=3-5` |
| `ttft-priority` | 15 | 10 | 150000 | TTFT 优先，高 ttft_penalty |
| `tpot-priority` | 15 | 10 | 150000 | TPOT 优先，高 tpot_penalty |
| `throughput` | 10 | 5 | 100000 | 吞吐优先，低延迟惩罚 |

**时间预算自动计算**：

```bash
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --scenario deep-optimize --time-budget 8h
```

- 每个 seed 运行服务两次（预热 + 正式）
- 总时间 ≈ n_particles × iters × 2 × 单次测试时间

### 添加搜索参数

```bash
# 整数参数
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --add-search-param \
    --engine vllm --param-name MAX_BATCH_SIZE \
    --min 10 --max 400 --dtype int --value 100

# 浮点参数
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --add-search-param \
    --engine vllm --param-name GPU_MEMORY_UTILIZATION \
    --min 0.8 --max 0.95 --dtype float --value 0.9

# 枚举参数
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --add-search-param \
    --engine vllm --param-name TENSOR_PARALLEL_SIZE \
    --dtype enum --enum-values "[1,2,4,8,16]" --value 4

# 比例参数
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --add-search-param \
    --engine vllm --param-name MAX_PREFILL_RATIO \
    --min 0.1 --max 0.7 --dtype ratio --dtype-param max_batch_size --value 0.3
```

### 添加固定参数

```bash
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --add-fixed-param \
    --engine vllm --param-name MAX_MODEL_LEN \
    --value 16384 --dtype int
```

### 参数类型

| dtype | 含义 | 必填字段 |
|-------|------|----------|
| `int` | 整数 | min, max |
| `float` | 浮点 | min, max |
| `bool` | 布尔 | value |
| `str` | 字符串 | value |
| `enum` | 枚举 | enum_values |
| `ratio` | 比例（相对于另一参数） | dtype_param |
| `factories` | 工厂（dp = product / tp） | factories_config |

### 服务配置

```bash
# VLLM 命令配置（自动同步 model/served_name/port 到 vllm_benchmark）
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --set-vllm-command \
    --model /path/to/model \
    --served-name my-model \
    --host 127.0.0.1 \
    --port 8000 \
    --others "--trust-remote-code"
```

### 测评工具配置

```bash
# AISBench
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --set-ais-bench \
    --models /path/to/models.yaml \
    --datasets /path/to/datasets.yaml \
    --mode perf \
    --ais-num-prompts 150

# vllm_benchmark（自动同步 model/served_name/port 到 vllm.command）
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --set-vllm-benchmark \
    --model /path/to/model \
    --served-name my-model \
    --dataset-name random \
    --vllm-num-prompts 100 \
    --others "--random-input-len 3000 --random-output-len 1000"
```

> **注意**：`--others` 必须由用户根据实际业务负载显式指定，不提供默认值。

### 预览模式

所有命令支持 `--dry-run` 预览，不实际写入：

```bash
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --scenario standard --engine vllm --dry-run
```

### 组合示例

```bash
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --scenario standard --engine vllm && \
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --set-vllm-command --model /data/model --served-name model && \
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --add-search-param --engine vllm --param-name TP --dtype enum --enum-values "[1,2,4,8]" --value 4 && \
python skills/msmodeling-optix-param-recommend/scripts/auto_config.py --set-vllm-benchmark --model /data/model --dataset-name random --others "--random-input-len 3000 --random-output-len 1000"
```

### config_skill_handoff 交接对象

脚本输出的 `config_skill_handoff` 包含：

- `consumer_skill`：固定为 `msmodeling-optix-param-recommend`。
- `target_fields`：规范化后的推荐字段，含 `section`、`name`、`dtype`、`min`、`max`、`value`、`dtype_param`、`search` 和推荐理由。
- `vllm_command_others`：vLLM 需放入 `[vllm.command].others` 的片段；可搜索参数用 `$VAR` 占位符。
- `apply_commands`：可审阅执行的 `auto_config.py` 命令清单。
- `notes`：当前 CLI 无法直接覆盖的部分（如 `ais_bench` section）。

交接原则：

- `toml_snippet` 用于人工审阅和精确复制。
- `config_skill_handoff.target_fields` 用于自动应用。
- `config_skill_handoff.apply_commands` 用于命令式衔接。
- vLLM `MAX_NUM_BATCHED_TOKENS` 和 `MAX_NUM_SEQS` 已被框架内置占位符，不追加到 `others`。
- PP=1 或 DP=1 时不应写入 `vllm.command.others`——显式传默认值会触发 rank 范围校验错误。仅在 PP > 1 或 DP > 1 时才需显式传参。
- `ENABLE_PREFIX_CACHING`、`ENABLE_CHUNKED_PREFILL` 不写入 target_field 也不写入 command。
- `ais_bench` 的 `CONCURRENCY`、`REQUESTRATE` 暂只保留在 JSON handoff 中。

## 已知问题与规避

### 1. `COMPILATION_CONFIG` 必须用 `dtype = "enum"`

optimizer 无法处理 `dtype = "str"`，字符串枚举参数必须用 `dtype = "enum"`：

```toml
# ❌ 错误
[[vllm.target_field]]
name = "COMPILATION_CONFIG"
dtype = "str"
value = "{\"cudagraph_mode\": \"FULL_DECODE_ONLY\"}"

# ✅ 正确
[[vllm.target_field]]
name = "COMPILATION_CONFIG"
dtype = "enum"
min = 0
max = 1
dtype_param = ["{\"cudagraph_mode\": \"FULL_DECODE_ONLY\"}"]
value = "{\"cudagraph_mode\": \"FULL_DECODE_ONLY\"}"
```

### 2. `$COMPILATION_CONFIG` 占位符展开缺少 flag 前缀

**规避**：固定值直接写 `--compilation-config '<json>'` 到 `others`；不要用 `$VAR` 占位符。

### 3. `others` 遗漏 `--max-model-len` 导致 OOM

vLLM 回退到 `max_position_embeddings`（如 262144），远超显存容量。确保 `others` 中包含 `--max-model-len $MAX_MODEL_LEN`。

### 4. `--data-parallel-size` / `--pipeline-parallel-size` 触发 rank 校验

DP=1 和 PP=1 不应写入 `vllm.command.others`，仅在 DP > 1 或 PP > 1 时显式传参。

### 5. 不应为适配保守 `MAX_MODEL_LEN` 而缩短 benchmark 长度

benchmark 的 I/O 长度必须反映实际业务负载。正确做法是先尝试 `MAX_MODEL_LEN = business_max`，启动失败再下调并告知用户上下文限制。

### 6. `target_field` 未设 `min`/`max` 导致 optimizer 生成随机大值

不需要搜索的固定参数不创建 target_field；必须保留的固定参数设 `min == max == value`。

### 7. 删除强制性 target_field 导致 `$VAR` 原样传入

`MAX_NUM_SEQS`、`MAX_NUM_BATCHED_TOKENS`、`CONCURRENCY`、`REQUESTRATE` 由框架自动注入，不可删除，也不应在 `others` 中重复。

## 快速命令

打印 context 模板：

```bash
# 打印 context 模板
python skills/msmodeling-optix-param-recommend/scripts/recommend_params.py --print-template

# 执行推荐
python skills/msmodeling-optix-param-recommend/scripts/recommend_params.py --context /path/to/context.json
```
