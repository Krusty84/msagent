# L1 跨网络通用坑与调优手段（cross-model-pitfalls）

> **定位与阅读规则**
> - 本文件是**与具体模型结构无关**的通用调优经验，任何新模型量化调优前**都先通读本文件**。
> - 本文件回答的不只是「回退哪些层」，而是**完整的精度调优手段链**：从量化格式选型、离群值抑制、量化方法/粒度/对称性、校准集，到敏感层回退（最终手段）。
> - 规则均标注证据来源（`source`）与置信度（高/中/低），只给有据可依的建议；无证据时不编造「必然」「所有模型都」。

---

## 0. 精度调优的递进路径（总纲）

量化精度不达标时，**不要一上来就回退**。按以下顺序递进，每步只改一类手段、留痕可回滚：

```text
步骤1：确认精度问题可信（排除环境/评测干扰）
    ↓
步骤2：调整离群值抑制算法（关键步骤，先于量化方法）
    ↓
步骤3：调整量化方法 / 粒度 / 对称性（权重与激活分开选）
    ↓
步骤4：调整校准集（数量、场景匹配、质量）
    ↓
步骤5：敏感层回退（最终手段，损失量化收益）
```

- 每一步都有对应的经验条目（见下）。**回退是最后一步**，不是默认出路。
- 来源：`msmodelslim/docs/zh/user_guide/process_quantization_precision_tuning.md`

---

## 1. 步骤1：先确认精度问题可信

进入任何调优动作前，先排除非量化因素造成的「假精度问题」。

| 验证项 | 操作 | 说明 |
|---|---|---|
| 推理引擎验证 | 用浮点模型在目标推理引擎上复测 | 确认能复现原始精度，排除引擎接入问题 |
| 测评结果检查 | 检查量化模型测评输出 | 排除上下文截断、超时等非量化问题 |
| 确定波动范围 | 了解测评数据集本身的精度波动 | 判断当前损失是否异常、是否在噪声范围内 |

- 若浮点基线本身无法在目标引擎复现，后续所有调优结论都不可信。
- 来源：`process_quantization_precision_tuning.md#步骤1`；置信度：高。

---

## 2. 步骤2：离群值抑制算法（关键步骤）

激活值中的**离群值会大幅扩展量化范围**，占用有效量化比特。首选思路是把激活的量化难度「转移」到权重上，而不是直接回退。

### 2.1 算法选择建议

| 场景 | 建议 | 说明 |
|---|---|---|
| 大多数 W8A8 场景 | **优先 Iterative Smooth** | 速度快、精度较高，通用首选 |
| 分布更复杂 / Iterative Smooth 不够 | 叠加 **Flex Smooth Quant** | 二阶段网格搜索自动寻 alpha/beta |
| INT4（w4a8）低比特权重 | **Flex AWQ SSZ** | AWQ + SSZ + 真实量化器评估误差，低比特首选 |
| 需要进一步抑离群 | 叠加 **QuaRot**（正交旋转） | 可与 Smooth 类协同，精度上限高 |
| MLA 长序列 / per-head 差异大 | 配 **FA3 Quant**（见结构文档 L2） | 属结构家族场景 |

- 离群值抑制类算法列表及对比见 `msmodelslim/docs/zh/knowledge_base/quantization_algorithms/README.md#离群值抑制算法`。
- 来源：`process_quantization_precision_tuning.md#步骤2`、`algorithm_brief.md`；置信度：高。

### 2.2 对称性选择

- **非对称离群值抑制算法在多数情况下优于对称方案**，但须**提前确认推理引擎是否已适配支持非对称**；否则改用对称。
- 来源：`process_quantization_precision_tuning.md`；置信度：中（受引擎适配约束）。

### 2.3 常用可调参数

| 算法 | 参数 | 经验 |
|---|---|---|
| Iterative Smooth | `alpha`（迁移强度） | 精度未达预期时优先调 `alpha`；激活异常值越多，迁移强度越大 |
| Iterative Smooth | `scale_min` | 防止缩放因子过小（如实践用 `1e-5`） |
| Flex AWQ SSZ | `enable_subgraph_type` | 常开 `norm-linear`、`linear-linear`、`ov`、`up-down` |
| QuaRot | `block_size` | 常见 `32`；决定旋转矩阵块 |

- 来源：`expert_experience.yaml`、`lab_practice/minimax_m3/minimax_m3_w8a8.yaml`；置信度：中。

---

## 3. 步骤3：量化方法 / 粒度 / 对称性

### 3.1 权重量化方法选型

| 位宽 | 建议 | 说明 |
|---|---|---|
| INT8 权重 | **优先 `minmax`** | 精度够、速度最快 |
| INT4 等低比特权重 | **优先 `ssz`**；不够再 `autoround`、`gptq` | `ssz` 是低比特实践的默认选择 |
| 极致精度 / 极度敏感 | `autoround` | 高精度上限，运行慢，最接近浮点 |

- 来源：`process_quantization_precision_tuning.md#权重量化`；置信度：高。

### 3.2 权重量化粒度与对称性

| 项 | 建议 | 说明 |
|---|---|---|
| `scope` | 通常 `per_channel` | 比 `per_tensor` 粒度细，精度更高 |
| `symmetric` | 权重通常 `true` | 对称计算更简单 |

- 来源：`process_quantization_precision_tuning.md`；置信度：高。

### 3.3 激活量化

| 维度 | 建议 |
|---|---|
| 方法 | 优先 `minmax`；长尾分布且 minmax 不佳时试 `histogram` |
| 粒度 `per_tensor` | 追求性能（静态量化） |
| 粒度 `per_token` | 追求精度（动态量化） |
| 粒度 `pd_mix` | prefill 用 per_token、decode 用 per_tensor，平衡精度与性能，有助 W8A8 精度 |
| `symmetric` | 激活通常 `false`（非对称），更好适应非零中心分布 |

- 来源：`process_quantization_precision_tuning.md#激活值量化`；置信度：高。

### 3.4 特殊数据类型（MXFP 系）

- W4A4 / W8A8 等可走 **MXFP8 / MXFP4**（`dtype: mxfp8 / mxfp4`），采用 `per_block` 粒度 + `minmax`，属硬件亲和低比特格式。mxfp4 常配 `ext.axes: -1`。
- 实践中 MXFP 系多用于稳定低比特（如 GLM-5.1 的 W4A4 mxfp4、LongCat-Flash 的 W4A4 mxfp4、MiniMax-M2.7 的 W8A8 mxfp8）。
- 来源：`lab_practice/glm_5/glm_5_1_w4a4c8_mxfp4.yaml`、`longcat_flash/longcat_flash_w4a4_mxfp4.yaml`、`minimax_m2/minimax_m27_w8a8_mxfp8.yaml`；置信度：中。

---

## 4. 步骤4：校准集调整

当算法调整效果有限时，优化校准数据（成本低于回退）。

| 策略 | 操作 | 目的 |
|---|---|---|
| 增加数据量 | 建议 10–50 条样本 | 提升参数估计准确性，过少/过多都不好 |
| 匹配应用场景 | 中文模型用中文数据、代码模型用代码数据 | 贴近实际场景 |
| 平衡数据分布 | 多数据集抽样混合 | 提升多样性与均衡性 |
| 删除异常数据 | 删掉导致精度显著下降的样本 | 减少异常样本干扰 |
| 加入 badcase | 加入模型在该数据集的 badcase | 反映真实困难输入，提升困难样本精度 |

- 来源：`process_quantization_precision_tuning.md#步骤4`；置信度：高。

---

## 5. 步骤5：敏感层回退（最终手段）

仅当前 4 步仍不满足精度时才回退。回退会**部分减少量化收益**，需在精度与收益间权衡。

### 5.1 操作流程

1. **敏感层分析**：用 `msmodelslim analyze`（linear 可用 Std/Quantile/Kurtosis，layer 可用 mse_layer_wise，attention 可用 mse），得到层/结构的量化敏感度排序。
2. **配置回退**：在 Practice YAML 中通过 `exclude` 排除高敏感层，或设为更高精度档位（提级）。

### 5.2 通用优先回退对象

- **`mlp.down_proj`**：调优指南点名为量化敏感度最高的层之一，应优先考虑。但**层范围不统一**，须按目标结构对齐（通配 `*mlp.down_proj*` 或逐层列举 `layers.<i>.mlp.down_proj` 是等价表达）。
- 来源：`process_quantization_precision_tuning.md#步骤5`；置信度：中（高频出现但层号不统一）。

### 5.3 回退手法（术语）

| 手法 | 定义 |
|---|---|
| 结构化回退 | 按模块模式/结构块排除，如 `*gate`、`*mlp.down_proj`、`*kv_b_proj` |
| 提级回退 | 低比特模型中把某模块提到高精度档位，如 W4A8 中把某几层 experts 单列 W8A8 |
| 逐层回退 | 结构化候选仍不足时，对敏感层逐个/小批恢复高精度 |

- 来源：`process_quantization_precision_tuning.md`；置信度：高。

---

## 6. 通用记录与置信度规范

每条结论都记录：`source`（YAML/代码/指南路径）、`quant_type`、`structure`、`module_pattern`、`layer_scope`、`action`、`evidence`、`confidence`、`validation`。

| 等级 | 判定 |
|---|---|
| 高 | 多份独立实践 YAML 对同一结构+模块模式采取相同处理 |
| 中 | 单个实践明确配置，或多个文件相近但不完全一致 |
| 低 | 仅由结构映射/代码逻辑/模块名推断，无实测结果 |

- 无足够证据时用「候选/待验证」，不用「必然」「所有模型都」「一定提升」。
- 最终验收以实际量化全集精度与浮点基线为准。
- 来源：`process_quantization_precision_tuning.md`；置信度：高。