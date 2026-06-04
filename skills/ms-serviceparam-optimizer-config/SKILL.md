---
name: ms-serviceparam-optimizer-config
description: Automates the configuration of msServiceProfiler config.toml for parameter optimization. Use when modifying optimizer parameters, setting up VLLM/MindIE target fields, configuring benchmark tools (evalscope/vllm_benchmark), or preparing config.toml for service parameter optimization.
---

# msServiceProfiler 寻优工具配置管理

## 前置条件

使用本 skill 前，请确保：

1. **已完成工具安装**（使用 msprofiler skill 或其他方式）
2. **config.toml 文件已存在**（位于 `ms_serviceparam_optimizer/ms_serviceparam_optimizer/config.toml`）
3. **了解寻优参数类型**（见下文参数类型说明）

## 快速开始

### 完整配置流程示例

**VLLM + evalscope 配置**:
```bash
# 1. 应用场景模板
python scripts/auto_config.py --scenario standard --engine vllm

# 2. 配置 VLLM 服务参数
python scripts/auto_config.py --set-vllm-command \
    --model /data/models/llama-70b \
    --served-name llama-70b

# 3. 添加搜索参数（带范围）
python scripts/auto_config.py --add-search-param \
    --engine vllm --param-name MAX_NUM_BATCHED_TOKENS \
    --min 8192 --max 32768 --dtype int

# 4. 配置测评工具
python scripts/auto_config.py --set-evalscope \
    --url "http://127.0.0.1:8000/v1/chat/completions" \
    --model llama-70b
```

**VLLM + vllm_benchmark 配置**:
```bash
# 1. 应用场景模板
python scripts/auto_config.py --scenario standard --engine vllm

# 2. 配置 VLLM 服务
python scripts/auto_config.py --set-vllm-command \
    --model /data/models/deepseek-v3 \
    --served-name deepseek-v3

# 3. 配置 vllm_benchmark
python scripts/auto_config.py --set-vllm-benchmark \
    --model /data/models/deepseek-v3 \
    --served-name deepseek-v3 \
    --dataset-name random \
    --num-prompts 500
```

## 场景模板配置

### 使用方式

```bash
python scripts/auto_config.py --scenario <场景> --engine <引擎> [选项]
```

### 支持的场景

| 场景 | 说明 | n_particles | iters | 特点 |
|------|------|-------------|-------|------|
| `quick-test` | 快速测试 | 5 | 3 | 小范围参数，快速验证 |
| `standard` | 标准寻优 | 15 | 10 | 平衡深度和广度 |
| `deep-optimize` | 深度寻优 | 30 | 20 | 大范围精细搜索 |
| `ttft-priority` | TTFT优先 | 15 | 10 | 高ttft_penalty, 低tpot_penalty |
| `tpot-priority` | TPOT优先 | 15 | 10 | 高tpot_penalty, 低ttft_penalty |
| `throughput` | 吞吐优先 | 20 | 10 | 时延惩罚设为0 |

### 时间预算自动计算

```bash
# 根据可用时间自动计算最优参数
python scripts/auto_config.py --scenario deep-optimize --time-budget 8h
```

**时间估算说明**:
- 每个种子会拉起两次服务（预热+正式测试）
- 总时间 ≈ n_particles × iters × 2 × 单次测试时间

## 参数配置

### 添加搜索参数（带寻优范围）

> **重要**: `--value` 参数是必须的，用于指定参数默认值
> 
> **枚举参数默认值规则**: 如果用户未指定 `--value`，脚本会自动选择第一个**非空值**作为默认值（而不是空字符串）

```bash
# 整数参数
python scripts/auto_config.py --add-search-param \
    --engine vllm --param-name MAX_BATCH_SIZE \
    --min 10 --max 400 --dtype int --value 100

# 浮点参数
python scripts/auto_config.py --add-search-param \
    --engine vllm --param-name GPU_MEMORY_UTILIZATION \
    --min 0.8 --max 0.95 --dtype float --value 0.9

# 枚举参数
python scripts/auto_config.py --add-search-param \
    --engine vllm --param-name TENSOR_PARALLEL_SIZE \
    --dtype enum --enum-values "[1,2,4,8,16]" --value 4

# 字符串枚举参数（包含 JSON 值时，脚本自动添加单引号转义）
# 用户输入格式：--enum-values '["", "--config {\"key\": \"value\"}"]'
# 脚本自动生成：dtype_param = ["", "--config '{\"key\": \"value\"}'"]
python scripts/auto_config.py --add-search-param \
    --engine vllm --param-name COMPILATION_CONFIG \
    --dtype enum \
    --enum-values '["", "--compilation-config {\"cudagraph_mode\": \"FULL_DECODE_ONLY\"}"]' \
    --cli-arg=""

# 比例参数（相对于另一个参数）
python scripts/auto_config.py --add-search-param \
    --engine vllm --param-name MAX_PREFILL_RATIO \
    --min 0.1 --max 0.7 --dtype ratio --dtype-param max_batch_size --value 0.3

# 派生参数（dp = 16 / tp）
python scripts/auto_config.py --add-search-param \
    --engine vllm --param-name DP \
    --dtype factories \
    --factories-config '{"target_name":"TENSOR_PARALLEL_SIZE","product":16,"dtype":"int"}' --value 4
```

### 添加固定参数（不参与寻优）

```bash
# 固定整数
python scripts/auto_config.py --add-fixed-param \
    --engine vllm --param-name MAX_MODEL_LEN \
    --value 16384 --dtype int

# 固定字符串
python scripts/auto_config.py --add-fixed-param \
    --engine vllm --param-name COMPILATION_CONFIG \
    --value "" --dtype str

# 固定布尔值
python scripts/auto_config.py --add-fixed-param \
    --engine vllm --param-name ENABLE_PREFIX_CACHING \
    --value true --dtype bool
```

### 参数类型说明

| dtype | 含义 | 必需字段 | 示例配置 |
|-------|------|----------|----------|
| `int` | 整数参数 | min, max | `min=10, max=400` |
| `float` | 浮点参数 | min, max | `min=0.8, max=0.95` |
| `bool` | 布尔参数 | value | `value=true/false` |
| `str` | 字符串参数 | value | `value="string"` |
| `enum` | 枚举参数 | enum_values | `enum-values="[1,2,4,8]"` |
| `ratio` | 比例参数 | dtype_param | `dtype-param=target_param` |
| `factories` | 派生参数 | factories_config | `product/target_name` |
| `times` | 倍数参数 | dtype_param | `product/target_name` |

## 服务配置

### VLLM 命令配置

```bash
python scripts/auto_config.py --set-vllm-command \
    --model /path/to/model \
    --served-name my-model \
    --host 127.0.0.1 \
    --port 8000 \
    --others "--trust-remote-code --enable-expert-parallel"
```

配置字段：
- `model`: 模型路径
- `served_model_name`: 服务模型名
- `host`: 服务主机
- `port`: 服务端口号
- `others`: 其他启动参数（支持 `$VAR` 变量引用）

## 测评工具配置

### evalscope 配置

```bash
python scripts/auto_config.py --set-evalscope \
    --url "http://127.0.0.1:8000/v1/chat/completions" \
    --model "my-model" \
    --tokenizer-path "/path/to/tokenizer" \
    --dataset random
```

### vllm_benchmark 配置

```bash
python scripts/auto_config.py --set-vllm-benchmark \
    --model /path/to/model \
    --served-name my-model \
    --host 127.0.0.1 \
    --port 8000 \
    --dataset-name random \
    --num-prompts 500 \
    --others "--input-len 128 --output-len 256"
```

## 高级用法

### 预览模式（不实际修改）

所有命令都支持 `--dry-run` 预览：

```bash
python scripts/auto_config.py --scenario standard --engine vllm --dry-run
```

### 配置文件路径指定

```bash
python scripts/auto_config.py --scenario standard \
    --config-path /custom/path/config.toml
```

### 组合使用示例

```bash
# 完整配置一条命令（使用 && 串联）
python scripts/auto_config.py --scenario standard --engine vllm && \
python scripts/auto_config.py --set-vllm-command --model /data/model --served-name model && \
python scripts/auto_config.py --add-search-param --engine vllm --param-name TP --dtype enum --enum-values "[1,2,4,8]" && \
python scripts/auto_config.py --add-search-param --engine vllm --param-name DP --dtype factories --factories-config '{"target_name":"TP","product":8}' && \
python scripts/auto_config.py --set-vllm-benchmark --model /data/model --dataset-name random
```

## 配置验证

修改完成后，验证配置是否正确：

```bash
# 检查 TOML 语法
python -c "import tomllib; tomllib.load(open('config.toml', 'rb'))"

# 查看帮助确认工具可用
msserviceprofiler optimizer --help
```

## 常见问题

**Q: 如何删除已添加的参数？**
- 手动编辑 config.toml，删除对应的 `[[engine.target_field]]` 块

**Q: 如何修改已有参数的范围？**
- 重新执行 `--add-search-param` 命令，会自动更新同名参数

**Q: 参数未生效？**
- 检查参数名是否正确（区分大小写）
- 确认 `config_position` 设置正确（通常为 `"env"`）
- 验证 TOML 语法无错误

**Q: 如何查看当前所有配置？**
- 直接查看 config.toml 文件
- 或使用 `cat config.toml | grep -A 10 "target_field"`

## 参考文档

- [完整安装指导](../../docs/zh/serviceparam_optimizer_instruct.md)
- [参数类型详细说明](../../docs/zh/serviceparam_optimizer_instruct.md#配置文件说明)
- [MindIE 配置参考](https://www.hiascend.com/document/detail/zh/mindie/20RC1/mindieservice/servicedev/mindie_service0285.html)
