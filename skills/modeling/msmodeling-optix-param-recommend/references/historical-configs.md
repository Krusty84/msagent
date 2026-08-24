# 历史经验参数配置

本文档记录经过实战验证的 vLLM serve + bench 参数配置，供 `optix-param-recommend` 在推荐时参考。

每条经验记录包含匹配条件（model / hardware / target）和推荐配置，匹配条件满足时优先采纳经验配置。

---

## 配置索引

| 编号 | 模型 | 量化 | 硬件 | 负载 | 适用场景 |
|------|------|------|------|------|----------|
| EXP-001 | Qwen3.5-27B | W8A8 (Ascend) | 2×54GB | 3.5k入/1.5k出 | 首次服务化寻优 / 通用文本生成 |


## 命名常量

本文档公式中使用的常量统一定义于此。修改常量值即可影响所有引用处。

| 常量 | 值 | 说明 | 来源 |
|------|-----|------|------|
| `MODEL_LEN_MARGIN` | `3192` | 业务 max token 之上的上下文余量 | 3.5k入/1.5k出/8k context 实测反推 |
| `MODEL_LEN_ALIGN` | `1024` | max_model_len 向上取整对齐单位 | CUDA 对齐惯例 |
| `FAST_MAXLEN` | `8192` | 快算固定值：当 `input+output < FAST_THRESHOLD` 时固定为此值 | 常用 8K context |
| `FAST_THRESHOLD` | `5000` | 快算阈值：业务总 token 低于此值时使用 FAST_MAXLEN | — |
| `SEQS_PER_GB` | `1.7` | 单卡每 GB 显存可支持的并发序列数 | 54GB→91 seqs 实测基准 |
| `BATCHED_TOKENS_RATIO` | `0.28` | max_num_batched_tokens 占 `MAX_MODEL_LEN × MAX_NUM_SEQS` 的比例 | 8192×91×0.28≈20928 实测收敛 |
| `CONCURRENCY_RATIO` | `0.38` | max_concurrency 占 MAX_NUM_SEQS 的比例 | 91×0.38≈35 实测收敛 |
| `NUM_PROMPTS_FIRST` | `140` | 首次压测推荐 prompts 数 | 实测：快速跑通验证 |
| `NUM_PROMPTS_FORMAL` | `[500, 1000]` | 正式寻优推荐 prompts 范围 | — |
| `MTP_NUM_SPEC_TOKENS_DEFAULT` | `3` | MTP 推测 token 数默认值 | 实测验证 |


---

## EXP-001: Qwen3.5-27B-W8A8 (Ascend)

### 匹配条件

```
engine: vllm
platform: ascend
model.name_pattern: "Qwen3.5-27B.*w8a8"
quantization: w8a8 / ascend
world_size: 2
single_card_mem_gb: 54  (≥54 均适用，<54 需下调 max-num-seqs)
workload.input_len_avg: 2000~4000
workload.output_len_avg: 1000~2000
target: 通用（首次服务化寻优）
```

### 推荐 Serve 命令骨架

```bash
vllm serve <model_path> \
    --served-model-name "<model_name>" \
    --host 127.0.0.1 \
    --port <port> \
    --max-num-batched-tokens <FAST_MAXLEN * max_num_seqs * BATCHED_TOKENS_RATIO> \
    --max-num-seqs <根据单卡显存计算: 54GB→91, 64GB→108 (公式: single_card_mem_gb × SEQS_PER_GB)> \
    --max-model-len <input_len_max + output_len_max + MODEL_LEN_MARGIN, 向上取整到 MODEL_LEN_ALIGN 的倍数> \
    --tensor-parallel-size 2 \
    --pipeline-parallel-size 1 \
    --gpu-memory-utilization 0.94 \
    --block-size 16 \
    --speculative-config '{"method": "qwen3_5_mtp", "num_speculative_tokens": 3, "enforce_eager": true}'
```

### 推荐 Bench 命令骨架

```bash
vllm bench serve \
    --host 127.0.0.1 \
    --port <port> \
    --model <model_path> \
    --served-model-name "<model_name>" \
    --dataset-name random \
    --num-prompts <NUM_PROMPTS_FIRST ~ 500, 首次建议 NUM_PROMPTS_FIRST> \
    --max-concurrency <并发数, = world_size × 17 ~ 35> \
    --request-rate inf \
    --result-dir ./bench_result \
    --save-result \
    --random-input-len <业务 input_len_avg> \
    --random-output-len <业务 output_len_avg> \
    --temperature 0
```

### 参数详解

#### 并行策略

| 参数 | 值 | 推荐理由 |
|------|-----|----------|
| `--tensor-parallel-size` | 2 | 27B W8A8 约需 27GB 权重显存。2×54GB 场景 TP=2 每卡权重 ~13.5GB，剩余 ~40GB 可用于 KV cache。单卡 ≥80GB 时可尝试 TP=1。 |
| `--pipeline-parallel-size` | 1 | 单机 2 卡场景不启用 PP。 |

#### 显存与上下文

| 参数 | 值 | 推荐理由 |
|------|-----|----------|
| `--max-model-len` | `input_len_max + output_len_max + 3192` | **动态计算**。以 3.5k入/1.5k出 为例：3500+1500=5000，8192-5000=3192 余量。向上取整到 1024 的倍数。经验规则：在业务最大 token 数基础上 +~3k 作为余量；若 `input+output < 5k`，固定 8192 是常用选择。 |
| `--gpu-memory-utilization` | 0.94 | 经实测验证的稳定值。W8A8 量化后权重显存较小，0.94 可最大化 KV cache 容量。显存更紧张时下调至 0.90-0.92。 |
| `--max-num-seqs` | 54GB: 91 / 64GB: 128 | **按单卡显存线性缩放**。54GB 时 91 并发稳定；64GB 时可达 128；80GB 约 180。公式参考：`max_num_seqs ≈ single_card_mem_gb × 1.7`。 |
| `--max-num-batched-tokens` | 54GB: 20928 / 64GB: ~30000 | **联动公式**：`≈ max_model_len × max_num_seqs × 0.28`。以 8192×91×0.28≈20928 为例。与 `max-model-len` 和 `max-num-seqs` 联动调整。 |
| `--block-size` | 16 | KV cache block 大小。16 是常见选择，在碎片率和调度效率间平衡。可根据 `vllm serve --help=CacheConfig` 确认可用值。 |

#### 投机解码

| 参数 | 值 | 推荐理由 |
|------|-----|----------|
| `--speculative_config.method` | `qwen3_5_mtp` | Qwen3.5 系列原生支持的 MTP（Multi-Token Prediction）投机解码方法。 |
| `--speculative_config.num_speculative_tokens` | MTP_NUM_SPEC_TOKENS_DEFAULT | 经实测验证。推测 token 越多潜在收益越大但精度风险越高。首次建议 MTP_NUM_SPEC_TOKENS_DEFAULT，后续可尝试 [1, 3, 5] 搜索。 |
| `--speculative_config.enforce_eager` | `true` | MTP 投机解码 + CUDA Graph 混合使用时需 eager 模式避免兼容问题。 |

### Bench 参数详解

| 参数 | 值 | 推荐理由 |
|------|-----|----------|
| `--dataset-name` | `random` | 随机数据集，首次压测标准选择。 |
| `--num-prompts` | NUM_PROMPTS_FIRST | 首次压测用较小样本量快速验证。正式寻优建议 NUM_PROMPTS_FORMAL。 |
| `--max-concurrency` | 35 | 与 `max-num-seqs=91` 配合，35 并发留有调度余量。公式参考：`≈ max_num_seqs × 0.38`。 |
| `--request-rate` | `inf` | 最大压力模式，所有请求同时发送，测量系统峰值吞吐。 |
| `--random-input-len` | 3500 | 对应业务平均输入长度。 |
| `--random-output-len` | 1500 | 对应业务平均输出长度。 |
| `--temperature` | 0 | 确定性生成，排除采样随机性对性能测量的干扰。 |
| `--save-result` | 启用 | 保存压测结果供后续分析。 |
| `--result-dir` | `./bench_result` | 结果保存目录。 |

### 推荐搜索维度（Serve 侧）

首次寻优时，以下参数作为搜索维度（其余固定）：

| 搜索参数 | 范围/候选 | dtype | 推荐理由 |
|----------|----------|-------|----------|
| `TENSOR_PARALLEL_SIZE` | `[1, 2]`（2 卡场景取 world_size 因子） | enum | 测试 TP=1 vs TP=2 对吞吐和延迟的影响 |
| `MAX_MODEL_LEN` | `[4096, 8192, 16384]` | enum | 根据实际业务 case 调整 |
| `MAX_NUM_SEQS` | `[64, 91, 128]`（54GB）/ `[64, 128, 180]`（64GB+） | int | 测试并发上限 |
| `MAX_NUM_BATCHED_TOKENS` | 按 `max_model_len × max_num_seqs × [0.2, 0.28, 0.35]` 计算 | enum | 与 max-model-len 联动 |
| `GPU_MEMORY_UTILIZATION` | `[0.90, 0.92, 0.94]` | float | 微调显存利用率 |
| `NUM_SPECULATIVE_TOKENS` | `[1, 3, 5]` | enum | MTP 推测 token 数对吞吐的影响 |

### 推荐搜索维度（Bench 侧）

| 搜索参数 | 范围/候选 | dtype | 推荐理由 |
|----------|----------|-------|----------|
| `NUM_PROMPTS` | `[140, 500, 1000]` | enum | 不同样本量对统计稳定性的影响 |
| `MAX_CONCURRENCY` | `[17, 35, 70]` | enum | 不同并发压力下的系统表现 |
| `RANDOM_INPUT_LEN` | `[1024, 3500, 6144]` | enum | 不同输入长度下的吞吐/延迟曲线 |
| `RANDOM_OUTPUT_LEN` | `[256, 1500, 2048]` | enum | 不同输出长度下的吞吐/延迟曲线 |
| `TEMPERATURE` | `[0, 0.7, 1.0]` | enum | 验证采样参数对性能的影响（通常影响极小） |

### 以下参数首次寻优建议固定（不参与搜索）

| 固定参数 | 值 | 原因 |
|----------|-----|------|
| `PIPELINE_PARALLEL_SIZE` | 1 | 单机场景 |
| `DATA_PARALLEL_SIZE` | 1（默认） | 首次寻优不引入多副本复杂度 |
| `SPECULATIVE_CONFIG.method` | `qwen3_5_mtp` | 模型专属，不参与搜索 |
| `BLOCK_SIZE` | 16 | 首次固定，后续可探索 [8, 16, 32] |
| `QUANTIZATION` | `ascend` | 平台固定（Ascend） |
| `DATASET_NAME` | `random` | 首次固定，后续可对比 sharegpt 等 |
| `REQUEST_RATE` | `inf` | 首次测峰值吞吐 |

### max-model-len 计算公式

```
# 通用公式
MAX_MODEL_LEN = align_up(input_len_max + output_len_max + MODEL_LEN_MARGIN, MODEL_LEN_ALIGN)

# 快算（当 input + output < FAST_THRESHOLD 时）
MAX_MODEL_LEN = FAST_MAXLEN  # 常用固定值

# 多模态场景
MAX_MODEL_LEN = FAST_MAXLEN  # 固定值，多模态 encoder token 膨胀需要更大余量
```

公式说明：
- `input_len_max`：业务最大输入 token 数（从用户负载中获取）
- `output_len_max`：业务最大输出 token 数（从用户负载中获取）
- `MODEL_LEN_MARGIN`：经验余量（基于 3.5k入/1.5k出/8k context 的实测反推）
- `MODEL_LEN_ALIGN`：向上取整到 MODEL_LEN_ALIGN 的倍数（CUDA 友好）

### max-num-seqs 估算公式

```
# 按单卡显存线性缩放（基于 54GB → 91 seqs 的实测基准）
MAX_NUM_SEQS ≈ single_card_mem_gb × SEQS_PER_GB

# 参考值
54GB → 91
64GB → 108  (64 × SEQS_PER_GB ≈ 108.8)
80GB → 136  (80 × SEQS_PER_GB ≈ 136)
```

### max-num-batched-tokens 估算公式

```
MAX_NUM_BATCHED_TOKENS ≈ MAX_MODEL_LEN × MAX_NUM_SEQS × BATCHED_TOKENS_RATIO

# 以 EXP-001 实测为例
8192 × 91 × BATCHED_TOKENS_RATIO ≈ 208,700 → 实测收敛到 20928
```

### max-concurrency 估算公式

```
MAX_CONCURRENCY ≈ MAX_NUM_SEQS × CONCURRENCY_RATIO

# 以 EXP-001 实测为例
91 × CONCURRENCY_RATIO ≈ 34.6 → 实测收敛到 35
```

### 典型 context.json 片段

```json
{
  "engine": "vllm",
  "hardware": {
    "single_card_mem_gb": 54,
    "world_size": 2,
    "num_per_nodes": 2,
    "num_nodes": 1
  },
  "model": {
    "config_path": "/path/to/Qwen3.5-27B-w8a8-mtp/config.json",
    "name_pattern": "Qwen3.5-27B.*w8a8"
  },
  "workload": {
    "input_len_avg": 3500,
    "input_len_max": 4096,
    "output_len_avg": 1500,
    "output_len_max": 2048
  },
  "target": "balanced",
  "historical_exp_id": "EXP-001",
  "discovery": {
    "enabled": true
  }
}
```

### 适用边界

- ✅ Qwen3.5-27B 系列（含 instruct / mtp 变体）
- ✅ W8A8 量化（Ascend 平台 `--quantization ascend`）
- ✅ 单机 2 卡，单卡显存 ≥ 54GB（＜54GB 需下调 max-num-seqs 和 gpu-memory-utilization）
- ✅ 纯文本场景，输入 2k~4k + 输出 1k~2k
- ⚠️ 其他 Qwen3.5 模型规模（8B / 14B / 32B / 235B-A22B）需调整 TP 和显存参数
- ⚠️ 非 Ascend 平台需替换 `--quantization`、去掉 `--additional-config`
- ⚠️ 输入/输出长度超出此范围时，需重新计算 `max-model-len` 和 `max-num-batched-tokens`
- ❌ 不适用于 dense 非 MoE 场景之外的 EP 相关参数
- ❌ 不适用于 FP16/BF16 未量化版本的显存估算

### 变更记录

| 日期 | 变更 | 来源 |
|------|------|------|
| 2026-06-17 | 提取硬编码常量为命名常量，新增「命名常量」章节 | refactoring |
| 2026-06-17 | 更新为 2×54GB 实测数据：调整 max-num-seqs(91)、max-num-batched-tokens(20928)、gpu-memory-utilization(0.94)、block-size(16)；新增 bench 命令骨架和参数；新增 max-concurrency 估算公式 | user-provided benchmark result |
| 2026-06-16 | 初始版本，基于 Qwen3.5-27B-W8A8 Ascend 通用推荐 | user-provided production config |
