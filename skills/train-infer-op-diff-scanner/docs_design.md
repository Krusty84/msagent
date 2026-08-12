## 修订记录
| 日期 | 修订版本 | 修改描述 | 作者 | RFC文档 |
| -- | -- | -- | -- | -- |
| 2025-07-11 | 1.0 | 初稿完成 | — | — |
|  |  |  |  |  |

## 背景描述

### 问题背景

在 Ascend NPU 上进行 RL（强化学习）训练时，训练路径（Megatron）和推理/rollout 路径（vLLM）使用的是**不同后端引擎**，两者的算子实现存在天然差异：

- **训练路径（Megatron）**：侧重完整的前向+反向传播，算子粒度较细，如 `FlashAttentionScore`（独立注意力）、`Add` + `RmsNorm`（分离残差归一化）、`Swish`（独立 SiLU 激活）
- **推理路径（vLLM）**：侧重低延迟生成，大量使用 NPU 融合算子进行优化，如 `FusedInferAttentionScore`（融合注意力）、`AddRmsNormBias`（三合一归一化）、`SwiGlu`（SiLU+Gate 融合）、`_triton_rope`（Triton 融合 RoPE）

这种算子层面的不一致可能引入**数值精度偏差**，导致训练收敛异常或 rollout 生成质量下降。传统静态源码扫描无法获取运行时真实执行的算子（因为算子选择受输入 shape、数据格式、硬件特性等运行时因素影响）。

### 核心价值

- **运行时真实算子采集**：通过集成 verl profiler 到完整 RL 训练脚本，单次运行同时采集训练和推理路径的 CANN 级算子数据
- **全量算子对比**：生成逐模块（注意力、归一化、激活、RoPE、线性投影等）的算子差异对比表
- **差异影响评估**：按 🔴🟠🟡🟢 四级标记差异等级，量化精度风险
- **代码调用栈溯源**：每个差异算子附带源码路径和完整调用链，便于定位和修复

### 达成目标

- 生成标准化的训推算子差异报告（Markdown + CSV）
- 覆盖 Transformer 所有关键模块的算子对比
- 支持任意基于 verl + Megatron + vLLM 的 RL 训练脚本

---

## 方案设计

### 整体架构

本 skill 采用 **5 阶段串行流水线** 架构：

```
阶段 1 (配置解析 + 备份脚本)
  └─→ 阶段 2 (集成 Profiler 到脚本)
        └─→ 阶段 3 (运行 RL 脚本采集算子)  ← 最耗时
              └─→ 阶段 4 (提取算子 + 对比分析)
                    └─→ 阶段 5 (生成报告)
```

### 核心设计决策

#### 1. 运行时采集而非静态扫描

| 方案 | 优点 | 缺点 |
|------|------|------|
| 静态源码扫描 | 快速、不依赖 NPU | 无法获取运行时真实算子，融合策略不可知 |
| **运行时 Profiling（采用）** | 获取 NPU 真实执行算子，反映融合策略 | 需 NPU 环境，耗时较长 |

#### 2. 单次完整运行而非分离运行

**禁止**独立编写 Megatron 训练脚本和 vLLM 推理脚本来分别采集。必须通过完整 RL 脚本的 verl profiler 集成，在同一 step 内采集两条路径的算子。原因：
- RL 训练中训练和推理共享权重，分离运行无法保证权重一致性
- 分离运行可能触发不同的算子选择策略（如不同 batch size 导致不同融合决策）

#### 3. Profiler 集成策略

- **训练路径**：使用 e2e profiler（`global_profiler`），采集 actor 和 ref 的前向传播算子
- **推理路径**：使用 `discrete=True` 配置，将 vLLM rollout 的算子数据分离存储到独立 DB
- **采集级别**：`level=level1`（CANN 算子级别），采集 `npu`、`cpu`、`shapes` 内容

#### 4. DB 路径约定

```
profiler_output/
├── e2e/                                         # 训练路径（Megatron actor+ref）
│   └── <timestamp>/
│       └── ascend_pytorch_profiler_0.db         # ← 训练算子数据
└── agent_loop_rollout_replica_0/                # 推理路径（vLLM rollout）
    └── <timestamp>/
        └── ascend_pytorch_profiler_0.db          # ← 推理算子数据
```

### 数据流

```
RL 训练脚本
  ├── [verl profiler 集成]
  ├── Megatron actor/ref 前向 → e2e profiler → ascend_pytorch_profiler_0.db (训练)
  └── vLLM rollout 生成 → discrete profiler → ascend_pytorch_profiler_0.db (推理)

                    ↓ SQL 查询: COMPUTE_TASK_INFO

训练算子列表 (JSON)                        推理算子列表 (JSON)
                    ↓ 对比分析
            算子差异对比表 + 差异等级判定
                    ↓ 代码调用栈溯源
            完整报告 (Markdown) + CSV
```

### 算子差异等级判定规则

| 标记 | 颜色 | 判定条件 | 精度风险 |
|:----:|:----:|---------|:--------:|
| 🔴 | 红 | 融合 vs 非融合（如 Attention：多 kernel vs 单 kernel FusedInferAttentionScore） | 高 |
| 🟠 | 橙 | 同类操作但融合程度不同（如 Add+RMSNorm vs AddRmsNormBias），或不同后端实现同一数学运算 | 中 |
| 🟡 | 黄 | 实现方式不同但数学语义等价，或仅单侧存在且有等价替代 | 低 |
| 🟢 | 绿 | 相同基本算子，或单侧独占且无等价替代 | 极低 |

### 代码调用栈提取

对每个 🔴 和 🟠 差异算子，按以下优先级提取代码调用栈：

1. 查询 profiler DB 中 `MSTX` 表，获取算子与 Python 调用栈的对应关系
2. 若 MSTX 不可用，从预定义的源码路径映射表中标注文件路径和关键函数名
3. 以 call stack 形式展示：`上层调用路径 → 算子类/函数 → CANN 算子名`

---

## 使用说明

### 触发方式

在对话中提供 RL 训练脚本路径，并使用以下触发词之一：
- "训推算子扫描"
- "训推差异性"
- "算子差异报告"
- "融合算子对比"
- "train vs infer op diff"

### 前置条件

1. **NPU 可用**：至少 1 张 Ascend NPU 可用，驱动和固件正常
2. **环境依赖**：`torch`、`torch_npu`、`msprof`、`megatron-core`、`vllm`、`vllm-ascend`、`mbridge` 已安装
3. **模型就绪**：RL 训练脚本中指定的模型路径存在且包含 `config.json`
4. **脚本完整**：RL 训练脚本可正常运行（通过 verl + Megatron + vLLM 启动）

### 使用约束

- **必须运行完整 RL 脚本**：不允许编写独立脚本分离运行 Megatron 训练和 vLLM 推理
- **仅支持运行时采集**：所有算子信息来自真实 NPU 运行时 profiling，不支持静态源码扫描
- **每次执行会修改脚本**：profiler 配置会被注入到脚本中（执行前自动备份）
- **预计耗时**：0.6B 模型约 15-25 分钟（含脚本运行 5-10 分钟），大模型更长
- **vLLM Ascend 限制**：推理路径的 device-side 算子数据可能不完整，此时使用 `api_statistic_*.csv` 备选
- **不适用场景**：运行时 dump 数据的逐值对比（应使用 `rl-consistency-analysis` skill）、纯性能 profiling 分析（应使用 `msprof-analyze-cli` skill）

### 产物

| 产物 | 路径 | 格式 |
|------|------|------|
| 完整报告 | `<工作目录>/train_infer_op_diff_report.md` | Markdown |
| 算子对比表 | `<工作目录>/operator_diff_table.csv` | CSV (UTF-8 BOM) |
| 原始脚本备份 | `<脚本路径>.bak_<timestamp>` | Shell 脚本 |

---

## 测试设计

### 单元测试

| 测试项 | 测试方法 | 预期结果 |
|--------|---------|---------|
| 配置解析 | 提供标准 verl RL 脚本，验证各配置项提取正确性 | 准确提取脚本中的所有关键配置字段 |
| 脚本备份 | 执行备份命令，验证备份文件存在且内容与原文件一致 | 备份文件与原文件 diff 无差异 |
| Profiler 配置注入 | 检查修改后脚本是否包含正确的 `PROFILER` 数组 | 包含 `global_profiler`、`actor.profiler`、`ref.profiler`、`rollout.profiler` 配置 |
| 环境检查 | 在无 NPU 环境下执行，验证能正确报错并中断 | 返回明确错误信息，提示 NPU 不可用 |
| DB 路径验证 | 在模拟目录结构下验证路径发现逻辑 | 正确识别 e2e 和 rollout 路径下的 DB 文件 |

### 集成测试

| 测试项 | 测试方法 | 预期结果 |
|--------|---------|---------|
| 端到端运行 | 使用 Qwen3-0.6B + 标准 RL 脚本，完整执行 5 个阶段 | 生成含算子对比表和代码调用栈的完整报告 |
| vLLM DB 缺失降级 | 模拟 vLLM rollout 的 device-side 数据为空 | 自动降级到 api_statistic CSV，报告中标注 |
| 多 NPU 环境 | 在 8 卡环境下运行，验证多 rank 数据采集 | 各 rank DB 均正确生成 |

### 端到端测试

| 测试项 | 测试方法 | 验收标准 |
|--------|---------|---------|
| Qwen3-0.6B 标准场景 | 使用 `references/run_qwen3_0_6b_megatron_vllm_ascend.sh` | 报告包含注意力、归一化、激活、RoPE、线性投影 5 类模块对比 |
| 差异等级判定 | 人工审核报告中的 🔴🟠🟡🟢 标记 | 判定结果符合 4.3 节规则 |
| 代码调用栈完整性 | 检查报告中每个 🔴🟠 差异算子是否有调用栈 | 调用栈包含文件路径→函数→CANN 算子名 |
| CSV 产出 | 打开 CSV 文件验证编码和内容 | UTF-8 BOM 正确，字段完整，Excel 可正常打开 |
