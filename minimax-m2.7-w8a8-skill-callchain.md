# MiniMax-M2.7 W8A8 调优 Skill 调用链

> 依据 msagent 代码梳理：`skills/quantizer/quantization-accuracy-tuning-orchestrator/SKILL.md`（v0.9.4）、
> `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/routing.md`、
> `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/quantization_tuning.md`、
> `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/subagent_io_protocol.md`、
> `resources/configs/default/prompts/subagents/*.md` 与 `subagents/*.yml` 注册。

## 0. 参与者总览（按 msagent 注册）

| 类型 | 名称 | 注册 / 代码依据 | 角色 |
|------|------|----------------|------|
| workflow skill | `quantization-accuracy-tuning-orchestrator` | `skills/quantizer/` | 主控，按序委派，不展开细节 |
| 外部 skill | `msmodelslim-ep-parallel-adaptation` | `skills/quantizer/` | EP 适配（MoE 检查 + 改造 + 验证），回传 `EP_ADAPT_RESULT` / `requires_ep` |
| 外部 skill | `quantization-expert-experience-tuning-rules` | `skills/quantizer/` | 结构化回退意见，只回答不执行 |
| 外部 skill | `aisbench-dataset-compression-herding` | `skills/benchmark/` | 生成 coreset 压缩数据集（aime2025/gpqa） |
| subagent | `msmodelslim-model-analysis` / `msmodelslim-model-adapt` | `skills/quantizer/` | 适配前分析 / 模型适配（MSAGENT_IO v1） |
| subagent | `quant-tuning-evaluation-generator` | prompts/subagents + subagents/*.yml | 生成 Evaluation YAML，内部委派 `skills/quantizer/gen-evaluation-cfg` skill |
| subagent | `quant-tuning-practice-generator` | prompts/subagents + subagents/*.yml | 生成 Practice YAML，内部委派 `skills/quantizer/tune-practice-cfg` skill |
| subagent | `quant-tuning-quantizer` | prompts/subagents + subagents/*.yml | 执行量化，内部委派 `skills/quantizer/quant-tuning-quantize` skill |
| subagent | `quant-tuning-evaluator` | prompts/subagents + subagents/*.yml | 执行评测，内部委派 `skills/quantizer/quant-tuning-evaluate` skill |
| 编排层脚本 | `history_clear` 等 6 个 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/scripts/` | `execute` 直接执行，不委派 |

委派协议：subagent 一律 `task` 委派 + `msagent-io v1` 信封（`subagent_io_protocol.md`）；
编排层脚本一律 `execute`；外部 skill 用 `task` 委派（非 subagent）。

## 1. 主调用链

```
用户输入 "帮我进行 Minimax-M2.7 模型的 W8A8 调优"
  │
  ├─ 1. quantization-accuracy-tuning-orchestrator（编排层，workflow）
  │      1.1 参数提取 → 回显确认 ←→ 用户（user_input.md）
  │      1.2 路由决策（参数回显认可后、环境准备前，routing.md）
  │            └─ 设备卡数 ≥ 2 ─→ task 委派 msmodelslim-ep-parallel-adaptation
  │                  透传：model_path / model_type / device_list / 量化方案（W8A8）/ save_path
  │                  ├─ MoE 检查：无 routed experts → 回传 requires_ep=false，退回普通多卡/单卡
  │                  ├─ EP 就绪检查与改造（专家分片/权重按 rank 加载/mapping 本地化）
  │                  └─ EP 验证
  │                        ├─ 结构门禁：[EP_CHECK] 且 Check 1~6 全过（ep_checklist.md）
  │                        └─ 数值门禁：[EP_ACT_GATE] Check 7 —— 同一量化配置下
  │                              先「单卡量化」采集逐层激活基准，再「多卡 EP 量化」对比
  │                              逐层激活余弦相似度（ep_activation_gate.md）
  │                        └─ 回传 EP_ADAPT_RESULT=PASS/FAIL + requires_ep=true/false
  │            └─ 单卡 / 用户明确不用多卡 → 普通单卡流程，不委派 EP
  │
  ├─ 2. 环境准备（prepare_environment.md）
  │      └─ 编排层检查 msmodelslim / NPU 驱动 / vLLM / AISBench → 回显确认 ←→ 用户
  │
  ├─ 3. 模型准备（prepare_model.md，委派走 MSAGENT_IO v1）
  │      ├─ [未注册/结构不明] ─→ task 委派 msmodelslim-model-analysis（subagent）
  │      ├─ [需适配] ─→ task 委派 msmodelslim-model-adapt（subagent）
  │      └─ 回显确认 ←→ 用户
  │
  ├─ 4. 量化配置调优（核心，quantization_tuning.md）
  │
  │     4.1 FP baseline 与两个出口标准（循环前前置）←→ 用户
  │           ├─ 用户给绝对目标（子集/全集分别）→ 直接作为 target
  │           └─ 未给某方 → 生成浮点评测配置（target/tolerance 占位 100，gen-evaluation-cfg 规则）
  │                 跑 FP 基线 → target = FP baseline - tolerance 回填；占用额外卡数须提示确认；基线缓存复用
  │
  │     4.2 压缩数据集确认（默认）←→ 用户，三选一
  │           ├─ 用户自备已压缩数据集
  │           ├─ [需生成] ─→ task 委派 aisbench-dataset-compression-herding（coreset：aime2025/gpqa，约 30 分钟须确认）
  │           └─ 退回全集测试
  │
  │     4.3 服务化推理脚本询问（可选加速）←→ 用户
  │           ├─ 提供（预启动给地址 / 代启动给脚本路径）→ 循环内评测不走 evaluator 子 agent
  │           └─ 未提供 / 不确定 → 默认每轮启停服务
  │
  │     4.4 Evaluation YAML 生成（循环前一次）
  │           └─ task 委派 quant-tuning-evaluation-generator（subagent → gen-evaluation-cfg skill）
  │                 生成「子集 / 全集」两份测评配置（config_name 不同）；
  │                 测试配置须与浮点配置保持通用参数一致（aisbench / max-model-len）
  │
  │     4.5 结构化回退经验（二分前一次，standing_high_with_experience）
  │           └─ task 委派 quantization-expert-experience-tuning-rules
  │                 回传 experience_hints（优先回退候选 + 专家意见可信度），供 practice-generator 作 exclude 初值
  │
  │     4.6 调优循环（standing_high_with_experience；多卡时全程 EP）
  │           [每轮循环]
  │           ├─ 编排层脚本：history_clear（清空历史）
  │           ├─ task 委派 quant-tuning-practice-generator（subagent → tune-practice-cfg skill）
  │           │     输入含 strategy / round / prev_result / anchor_practice / experience_hints
  │           │     ├─ msmodelslim analyze（敏感层分析，首轮执行、各轮复用）
  │           │     └─ validate_practice_yaml.py（YAML 校验）
  │           │     └─ 生成 Practice YAML（exclude = experience_hints + 敏感层分析，二分截断守 gate_up_proj 配对）
  │           ├─ 编排层脚本：accuracy_lookup（查精度缓存）
  │           │     └─ 命中 → 跳过量化/评测
  │           ├─ [未命中缓存]
  │           │     ├─ task 委派 quant-tuning-quantizer（subagent → quant-tuning-quantize skill）
  │           │     │     └─ msmodelslim quant（多卡 EP 时固定 --device npu:0,1,... 且日志含 [EP_CHECK]）
  │           │     ├─ 评测二选一：
  │           │     │     ├─ [无服务化脚本] task 委派 quant-tuning-evaluator（subagent → quant-tuning-evaluate skill）
  │           │     │     │     └─ run_evaluation.py --device-indices 多卡（EP 保持多卡不退回单卡）
  │           │     │     └─ [有服务化脚本] 编排层直接 execute：start/reload + evaluate（跳过 evaluator 子 agent）
  │           │     └─ 编排层脚本：accuracy_append（写精度缓存）
  │           ├─ 编排层脚本：history_append（写调优历史）
  │           └─ 检查退出条件
  │                 ├─ 子集达标 → 全集验证 → 不达标则切全集重跑调优（切前回显确认 ←→ 用户）
  │                 ├─ 二分收敛（上下界不可再分）→ 输出上界为最优
  │                 ├─ 达到最大迭代次数 → 输出历史最优达标配置
  │                 └─ 未收敛 → 继续循环
  │                 （experience_hints 只供 exclude 初值，二分上下界/收敛判定不受其影响）
  │
  └─ 5. 结果输出（output_format.md）
        ├─ 输出最优量化权重 + 评测报告
        ├─ 编排层脚本：finalize_practice_repo.py（回写 practice 仓库）
        └─ 磁盘清理（保留 ≤ 2 份完整权重，用 rm -r 禁 rm -rf）
```

## 2. 角色说明（委派方式）

| 调用方 | 角色 | 调用方式 |
|--------|------|----------|
| 编排层 | orchestrator（workflow skill） | 主控流程，按序委派 |
| EP 适配 / 经验库 / 压缩数据集 | 外部 skill | `task` 委派，非 subagent；经验库只回答问题 |
| 模型分析 / 适配 | subagent | `task` 委派，MSAGENT_IO v1（`prepare_model.md` 定义字段） |
| evaluation-generator | subagent | `task` 委派，MSAGENT_IO v1；内部调 `gen-evaluation-cfg` skill |
| practice-generator | subagent | `task` 委派，MSAGENT_IO v1；内部调 `tune-practice-cfg` skill |
| quantizer | subagent | `task` 委派，MSAGENT_IO v1；内部调 `quant-tuning-quantize` skill |
| evaluator | subagent | `task` 委派，MSAGENT_IO v1；内部调 `quant-tuning-evaluate` skill（有服务化脚本时跳过） |
| 编排层脚本 | orchestrator | `execute` 直接执行，不委派 |

## 3. 实现依据（skills/ 对应位置）

| 环节 | 依据 |
|------|------|
| 编排主流程 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/SKILL.md` |
| 参数提取/回显 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/user_input.md` |
| 路由/EP 适配 | `SKILL.md` 1.5 节 + `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/routing.md` |
| EP 验证门禁 | `skills/quantizer/msmodelslim-ep-parallel-adaptation/SKILL.md` 决策树 + `references/ep_checklist.md`（Check 1~6 结构 + Check 7 数值）+ `ep_activation_gate.md`（单卡量化 vs 多卡 EP 量化） |
| FP baseline / 双出口标准 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/quantization_tuning.md`「FP baseline 获取」「两个出口标准（子集+全集）」 |
| 压缩数据集 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/quantization_tuning.md`「压缩数据集的使用」 |
| 服务化推理脚本 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/quantization_tuning.md`「服务化推理脚本」 |
| 结构化回退经验 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/quantization_tuning.md`「结构化回退经验（二分前接入）」 |
| 调优循环 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/quantization_tuning.md` 流程图 + `skills/quantizer/tune-practice-cfg/SKILL.md` |
| 经验库接入 | `skills/quantizer/quantization-expert-experience-tuning-rules/SKILL.md`（L1+L2+L3，只回答问题） |
| 模型准备 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/prepare_model.md` + prompts/subagents（model-analysis / model-adapt） |
| subagent 协议 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/references/subagent_io_protocol.md`（MSAGENT_IO v1） |
| 编排脚本 | `skills/quantizer/quantization-accuracy-tuning-orchestrator/scripts/`：history_clear / accuracy_lookup / accuracy_append / history_append / accuracy_cleanup / finalize_practice_repo |

## 4. MiniMax-2.7 要点速查

| 要点 | 内容 |
|------|------|
| 模型结构 | MoE，专家命名 `block_sparse_moe.experts`（非标准 `mlp.experts`） |
| 量化方案 | W8A8 int：`quarot` + `linear_quant`（per_token/per_channel、minmax）；W8A8 mxfp8：`quarot` + `linear_quant`（per_block、mxfp8、minmax） |
| 量化范围 | include 仅 `*block_sparse_moe.experts*` + `*self_attn*`，其余层不量化 |
| 参考配置 | `lab_practice/minimax_m2/minimax_m27_w8a8_mxfp8.yaml`、`minimax_m27_w8a8.yaml` |
| EP 并行 | 多卡自动适配；加入后全程多卡不退单卡；`EP_ACT_GATE` 需先单卡量化取基准再 EP 量化对比 |
| 调优策略 | `standing_high_with_experience`：结构化回退经验 + 摸高二分搜索 |
| 二分约束 | `gate_proj`/`up_proj` 在 vLLM 融合为 `gate_up_proj` 必须同退同量化，截断不得落在配对中间 |
| 数据集 | 默认压缩子集快速迭代，全集最终验收（两个出口标准） |