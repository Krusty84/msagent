# 参数推荐规则

本文档记录第一版启发式规则。规则面向首次使用用户，优先保守、稳定、可解释。

## 通用规则

- 默认 benchmark：vLLM 使用 `vllm_benchmark`，MindIE 使用 `ais_bench`。
- 核心约束：`DP * TP * PP == world_size`。
- 首次寻优优先推荐 `DP * TP * PP == world_size`，即尽量用满卡。
- TP 从 `world_size` 的因子中选择，同时要求能整除 `num_attention_heads`。
- 单机场景首次寻优默认 PP 为 1；多机场景或模型很大时再考虑 PP。
- 根据模型 config、单卡显存、TP、dtype 和业务 token 长度估算 KV cache 容量。

## MindIE 字段

首次寻优推荐字段：

- `max_batch_size`：根据 KV cache 容量估算。
- `max_prefill_batch_size`：`max_batch_size` 的比例；TTFT 优先时更低，吞吐优先时更高。
- `max_prefill_token`：覆盖首次寻优业务负载的 prefill token 压力。
- `max_queue_deloy_mircroseconds`：TTFT 优先时更低，吞吐优先时更高。
- `support_select_batch`：TTFT 优先时关闭，吞吐或均衡场景开启。
- `prefill_time_ms_per_req`、`decode_time_ms_per_req`：作为可搜索调度参数。
- `max_preempt_count`：按 `max_batch_size` 的比例生成，首次寻优保持较低范围。
- `prefill_policy_type`、`decode_policy_type`：使用 optimizer 已支持的枚举候选 `[0, 1, 3]`。
- `tp`、`dp`：作为并行参数；`dp` 默认由 `world_size / tp` 派生。
- `moe_ep`、`moe_tp`：仅在模型为 MoE 或 config 中存在专家数信息时推荐，`moe_tp` 由 `moe_ep` 派生。
- `CONCURRENCY`、`REQUESTRATE`：`ais_bench` 压测负载参数。

MindIE 第一版默认 PP 为 1，除非用户确认自己的配置里有 PP 字段。因此脚本输出的约束说明统一写作 `DP * TP * PP == world_size`；在未显式配置 PP 的场景下，TOML 约束默认等价为 `$dp * $tp == $NPU_COUNT`。

## vLLM 字段

首次寻优推荐字段：

- `MAX_MODEL_LEN`：`align_up(input_len_max + output_len_max + MODEL_LEN_MARGIN, MODEL_LEN_ALIGN)`。短负载（total < FAST_THRESHOLD）固定为 FAST_MAXLEN。受模型最大上下文限制。
- `MAX_NUM_SEQS`：根据 KV cache 容量估算，并参考下方「MAX_NUM_SEQS 初始范围分级」。
- `MAX_NUM_BATCHED_TOKENS`：与输入长度和 `MAX_NUM_SEQS` 联动，并参考下方「MAX_NUM_BATCHED_TOKENS 初始范围分级」。
- `TENSOR_PARALLEL_SIZE`：选择能整除 attention heads 的 world_size 因子。
- `PIPELINE_PARALLEL_SIZE`：首次单机场景固定为 1；多机或大模型再考虑增加。
- `DATA_PARALLEL_SIZE`：派生固定值；只有用户当前 vLLM 启动模式支持时才接入命令。
- `GPU_MEMORY_UTILIZATION`：保守范围 `0.85-0.92`，降低首次启动 OOM 风险。
- `BLOCK_SIZE`：KV cache block size 候选，建议用本机 `vllm serve --help` 确认可用枚举。
- `ENABLE_PREFIX_CACHING`：presence flag，首次推荐使用 vLLM 默认值，不纳入搜索，也不显式传参。
- `ENABLE_CHUNKED_PREFILL`：presence flag，首次推荐使用 vLLM 默认值，不纳入搜索，也不显式传参。
- `COMPILATION_CONFIG`：字符串枚举，默认空值；确认环境支持后可加入 cudagraph 编译配置候选。
- `CONCURRENCY`、`REQUESTRATE`：压测负载参数。

### MAX_NUM_SEQS 初始范围分级

不要对 Ascend 所有模型一刀切使用 16-32。根据模型 attention 类型和规模分三档：

| 模型特征 | 首轮默认 | 首轮范围 | 适用场景 |
|---------|---------|---------|---------|
| GQA/MLA + 非 MoE + ≤32B | **36** | 24~48 | Qwen3-32B 及以下、DeepSeek-V3.2 等 GQA/MLA 模型 |
| GQA/MLA + MoE 或 >32B | 24 | 16~32 | DeepSeek-V3、Qwen3-235B 等大模型或 MoE 模型 |
| 纯 MHA（`num_attention_heads == num_key_value_heads`） | 16 | 8~24 | GLM-4.7-Flash 等纯 MHA 模型，KV cache 密度极高 |

**为什么 GQA/MLA 可以更高**：GQA 的 KV cache 每 token 约为纯 MHA 的 1/6~1/10（如 Qwen3-32B GQA ~96 KB/token vs GLM-4.7-Flash MHA ~962 KB/token），同等显存可容纳更多并发序列。

**首轮跑通后立即放宽**：首轮 baseline 无报错后，应把 `MAX_NUM_SEQS` 上限翻倍（如 32→64、48→96），让 optimizer 探索真正的吞吐上限。

### CONCURRENCY 与负载特征的关系

**CONCURRENCY 不能只按 `MAX_NUM_SEQS / 1.5` 机械推算**。必须结合负载的 decode 占比：

| 负载特征 | CONCURRENCY 策略 | 原因 |
|---------|-----------------|------|
| **长 decode**（output ≥ 1k tokens） | 保守（12~20），小幅试探 | decode 步每步都要算所有活跃序列，并发翻倍 → TPOT 翻倍，吞吐可能反降 |
| **短 decode**（output ≤ 256 tokens） | 可激进（20~32+） | decode 很快结束，并发带来的吞吐收益 > 延时代价 |
| **均衡** | 中等（16~24） | 按实测 TPOT 余量调整 |

**预判公式**（粗略）：
```text
TPOT_new ≈ TPOT_baseline × (CONCURRENCY_new / CONCURRENCY_baseline)
```
如果预判 `TPOT_new > tpot_slo`，就不要提 CONCURRENCY，改为提 `MAX_NUM_BATCHED_TOKENS` 或 `MAX_NUM_SEQS`。

**关键原则**：长 decode 负载的吞吐瓶颈通常在 prefill 批次大小（`MAX_NUM_BATCHED_TOKENS`）和调度效率，而非并发数。盲目加 CONCURRENCY 只会让所有请求一起变慢，吞吐反而下降。

### MAX_NUM_BATCHED_TOKENS 估算（γ-based continuous-batching 模型）

单次 scheduler iteration 中，绝大多数序列处于 decode 阶段（1 token），只有少数新到达请求同时执行 prefill。因此 `MAX_NUM_BATCHED_TOKENS` 不应等于 `input_len_avg × max_num_seqs`（会高估 5-10 倍），而应基于 prefill 并发比例 γ 估算：

```
MAX_NUM_BATCHED_TOKENS = min(
    max_model_len × max_num_seqs × γ  +  max_num_seqs,
    platform_ceiling
)

γ  = β × (input_avg / max_model_len)²
γ  = clamp(γ, 0.02, 0.50)
```

| 符号 | 含义 | 取值逻辑 |
|------|------|----------|
| β | 目标相关的 prefill 并发基系数 | throughput → 0.22 / balanced → 0.15 / ttft → 0.08 |
| `input_avg / max_model_len` | 输入占上下文比例 | 比值越大 prefill 越重，γ 越接近上限 |
| 平方 | 惩罚高上下文占比 | 长 prompt 场景 prefill 并发迅速衰减 |
| `+ max_num_seqs` | decode token 贡献 | 每个 decode 序列贡献 1 token，几乎忽略 |
| `platform_ceiling` | 硬件硬上限 | Ascend → 32768 / CUDA → 131072 |

**与旧公式的差异**：

| 公式 | 你的场景 (3500入/1500出, max_model_len=8192, 57 seqs) | 评价 |
|------|------|------|
| 旧 `input_avg × max_num_seqs` | 3500 × 67 = 234500 → ceiling 32768 | 假设所有序列同时 prefill，脱离实际 |
| 旧 `max_model_len × max_num_seqs × 0.028` | 8192 × 57 × 0.028 = 13074 | γ 写死为定值，不同场景不通用 |
| 新 γ-based | 8192 × 57 × 0.0427 + 57 = **20000** | 自适应 input/max_model_len 比例 |

**校准依据**：β 值从 EXP-001（balanced, γ=0.028, ratio=0.43）和用户 Ascend W8A8 实测（throughput, γ≈0.043, ratio=0.43）反推并泛化。

### MAX_NUM_BATCHED_TOKENS 初始范围分级（旧表，已由 γ 公式替代）

以下为参考值，当 γ 公式因缺参数无法计算时回退使用：

| 模型特征 | 首轮默认 | 首轮上限 | 说明 |
|---------|---------|---------|------|
| GQA/MLA + 非 MoE | 24576 | 49152 | 非 MoE 无 all-to-all buffer 压力，可放宽 |
| GQA/MLA + MoE | 16384 | 32768 | MoE all-to-all 和 ACL graph 双重约束 |
| 纯 MHA | 8192 | 16384 | KV cache 密度极高，必须严格限制 |

presence flag 字段需要谨慎处理。vLLM 已有模型感知默认值的 flag 默认不写入 `vllm.command.others`；需要强制启用的 discovery flag 直接写字面量；需要搜索的字符串枚举才使用 `$COMPILATION_CONFIG` 这类占位符。

### 框架自动注入参数（custom_command.py）

`msmodeling optix` 框架在 `config/custom_command.py` 中会**自动注入**以下参数，不依赖 `others` 字段。这些参数的 `$VAR` 占位符和 target_field 定义是**强制性**的——缺少会导致 `$VAR` 原样传入命令而报错。

**vllm serve 命令自动注入**（`VllmCommand.command`）：

```text
--max-num-batched-tokens $MAX_NUM_BATCHED_TOKENS
--max-num-seqs $MAX_NUM_SEQS
```

**vllm bench serve 命令自动注入**（`VllmBenchmarkCommand.command`）：

```text
--max-concurrency $CONCURRENCY
--request-rate $REQUESTRATE
```

**强制规则**：

| 规则 | 说明 |
|------|------|
| `CONCURRENCY` 和 `REQUESTRATE` 是 vLLM 引擎的**必需** target_field，**必须写在 `[[vllm.target_field]]` 中** | 删除或错放在 `ais_bench` section 会导致 `$CONCURRENCY` / `$REQUESTRATE` 原样传入 → `invalid int value: '$CONCURRENCY'` |
| `MAX_NUM_SEQS` 和 `MAX_NUM_BATCHED_TOKENS` 是 vLLM 引擎的**必需** target_field | 删除会导致 `$MAX_NUM_SEQS` / `$MAX_NUM_BATCHED_TOKENS` 原样传入 |
| `--max-num-seqs` 和 `--max-num-batched-tokens` **不要**写入 `vllm.command.others` | 框架已自动注入，重复会导致命令行参数重复 |
| `--max-concurrency` 和 `--request-rate` **不要**写入 `vllm_benchmark.others` | 框架已自动注入，重复会导致命令行参数重复 |

**正确做法**：
- `vllm.command.others`：只包含框架未注入的参数（如 `--max-model-len`、`--tensor-parallel-size`、`--gpu-memory-utilization`、`--block-size`、`--speculative-config`）
- `vllm_benchmark.others`：只包含框架未注入的参数（如 `--random-input-len`、`--random-output-len`、`--temperature`）
- 四个必需 target_field（`MAX_NUM_SEQS`、`MAX_NUM_BATCHED_TOKENS`、`CONCURRENCY`、`REQUESTRATE`）必须在 `[[vllm.target_field]]` 中定义（注意是 vllm section，不是 ais_bench section），不可删除

**错误示例**：

```toml
# ❌ vllm.command.others 包含框架已注入的参数
others = "--max-num-seqs $MAX_NUM_SEQS --max-num-batched-tokens $MAX_NUM_BATCHED_TOKENS --max-model-len 8192 ..."

# ❌ vllm_benchmark.others 包含框架已注入的参数
others = "--max-concurrency 35 --request-rate inf --random-input-len 3500 ..."

# ❌ 删除了 CONCURRENCY target_field → $CONCURRENCY 原样传入
```

**正确示例**：

```toml
# ✅ vllm.command.others：只包含框架未注入的参数
others = "--max-model-len $MAX_MODEL_LEN --tensor-parallel-size $TENSOR_PARALLEL_SIZE --gpu-memory-utilization $GPU_MEMORY_UTILIZATION --block-size $BLOCK_SIZE $SPECULATIVE_CONFIG"

# ✅ vllm_benchmark.others：只包含框架未注入的参数
others = "--random-input-len 3500 --random-output-len 1500 --temperature 0"

# ✅ 四个必需 target_field 全部保留
[[vllm.target_field]]
name = "MAX_NUM_SEQS"
...

[[vllm.target_field]]
name = "MAX_NUM_BATCHED_TOKENS"
...

[[vllm.target_field]]
name = "CONCURRENCY"
...

[[vllm.target_field]]
name = "REQUESTRATE"
...
```

## vLLM Help Discovery 字段

当 `discovery.enabled = true` 且提供 `vllm_help_text` 或 `vllm_help_text_path` 时，脚本会额外扫描 help 输出。当前支持的可选追加规则：

- help 包含 `--max-num-partial-prefills` 且业务有长 prefill 时，追加 `MAX_NUM_PARTIAL_PREFILLS`。
- help 包含 `--long-prefill-token-threshold` 且业务有长 prefill 时，追加 `LONG_PREFILL_TOKEN_THRESHOLD`。
- help 包含 `--disable-chunked-mm-input` 且 `model.is_multimodal = true` 时，追加固定 flag `DISABLE_CHUNKED_MM_INPUT`。
- help 包含 `--enable-expert-parallel` 且模型为 MoE 时，追加固定 flag `ENABLE_EXPERT_PARALLEL`。

追加字段会标记 `source = "vllm --help"` 和 `optional = true`，并在 `result.discovery.added_parameters` 中列出。

## Benchmark 影响

benchmark 类型会影响 benchmark 侧 target field 和性能指标解释，但不应该覆盖基于硬件和模型推导出的服务侧参数范围。

用户未指定 benchmark 时，vLLM 默认使用 `vllm_benchmark`，MindIE 默认使用 `ais_bench`。

## 启动命令发现

用户提供的启动命令是可运行骨架，不是完整搜索空间。对 vLLM，help 输出可能发现模型相关的可选参数。除核心首次寻优字段外，发现到的额外参数默认保持固定或作为可选候选。

## 输出交接规则

脚本输出同时服务三类消费方：

- 用户阅读：看 `recommendations` 和 `toml_snippet`。
- agent 总结：看 `assumptions`、`constraints`、`discovery`。
- 配置 skill 应用：看 `config_skill_handoff`。

`config_skill_handoff.apply_commands` 只生成当前 `auto_config.py` CLI 能表达的命令，并使用 `--option=value` 形式承载以 `--` 开头的参数值。`ais_bench` 相关字段保留在 `target_fields` 和 `notes` 中，不写入 `toml_snippet` 的 target-field 块，因为当前 Settings loader 对 `ais_bench` target field 的处理存在兼容风险。

## 历史经验优先规则

对于已有实战验证的模型配置，优先采用历史经验而非通用启发式规则。详见 `references/historical-configs.md`。

优先级链：

```text
历史经验（historical-configs.md） > 通用启发式规则 > 默认值
```

匹配到历史经验时：
1. 经验中的固定参数（如 `COMPILATION_CONFIG`、`SPECULATIVE_CONFIG`、`QUANTIZATION`）直接采用，不纳入搜索维度。
2. 经验中的搜索维度范围和候选沿用经验推荐值，不做通用推断。
3. 经验中的计算公式（如 `max-model-len = input_len_max + output_len_max + 2048`）替换通用计算逻辑。
4. 超出经验适用边界的参数（如不同模型规模、不同平台），回退到通用规则。
