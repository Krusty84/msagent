---
name: npu-fusion-op-replace
description: 针对华为昇腾 NPU 上的 PyTorch 模型，识别可做融合算子替换的代码段、替换为 torch_npu 融合算子并做性能/精度验证。当用户提到融合算子、算子融合、NPU 算子替换、RMSNorm/SwiGLU/RoPE/Attention 融合、torch_npu 亲和算子、模型性能优化、减少 kernel launch、把手写算子换成官方融合 API、MoE grouped matmul，或提供模型代码/拉起脚本想提升 NPU 性能时，务必使用本 Skill。即使用户没说"融合"，只要在 Ascend NPU 上跑 PyTorch 模型并想提速、或拿着 profiling 数据问哪里能优化，就应加载本 Skill 走两阶段流程（识别→验证）。
---

# 融合算子识别与替换

## 这个 Skill 做什么

针对昇腾 NPU 上跑的 PyTorch 模型（训练或推理），找到可以用 `torch_npu` 融合算子替换的代码段，给出替换示例，再做性能和精度验证，最终给出"该不该换"的结论。

整个工作分两个阶段，**阶段一输出识别报告后必须停下来等用户确认**，确认后再进阶段二逐个验证。这样设计是因为：识别准不准、要不要换，需要人把关；而验证是长耗时过程（每个替换都要重跑任务对比），一旦方向错了浪费极大。所以宁可先对齐再动手。

## 核心概念

先厘清三个容易混的概念，后面识别替换时判断标准不同：

- **融合算子**：把多个连续计算层（如 matmul→激活→归一化）合并成一个 NPU 硬件内核（Kernel），中间结果不写回显存（HBM），直接在片上缓存流转。优化原理是数学等价替换 + 减少冗余计算 + 减少下发次数。典型：`npu_fusion_attention` 把 Q@K^T→mask→softmax→@V 合成一个算子。
- **亲和算子**：不改数学逻辑，仅把 NPU 不擅长的"随机访存指令"等价代换成 NPU 擅长的"连续乘加指令"的微观操作。典型：IndexPut→乘法、Nonzero→乘法求和、where→lerp。
- **亲和 API 替换**：NPU 官方提供的、替代 PyTorch 原生复杂组合操作的高阶封装函数，通常是个黑盒，内部串联了多个亲和算子或底层指令。典型：`npu_confusion_transpose`、`clip_grad_norm_fused_`、`NpuFusedAdamW`。

本 Skill 主要处理融合算子替换；亲和算子/亲和 API 在"高频算子序列"环节顺带识别。

## 两阶段工作流

```
阶段一（识别，必做）                      阶段二（逐个验证，按用户确认的清单）
┌──────────────────────────┐    用户确认   ┌──────────────────────────┐
│ 1. 常用融合算子扫描（必做）│ ──────────► │ 1. 按条件分支改代码        │
│ 2. 高频算子序列识别（advanced）│  暂停     │ 2. 逐个跑：精度优先        │
│ 3. 融合价值评估            │  不改代码   │ 3. 精度不过→直接否决        │
│ 4. 输出 markdown 识别报告  │            │ 4. 精度过→再看性能         │
└──────────────────────────┘            └──────────────────────────┘
```

---

## 阶段一：识别可替换的融合算子

### 1.1 常用融合算子（必做项）

这是每个识别任务都要逐个排查的清单。拿着模型代码，对照下表，看有没有匹配得上的代码逻辑：

| 手写模式 | 融合 API | 一句话要点 |
|----------|----------|-----------|
| 手写 RMSNorm（`x.pow(2).mean → rsqrt → * weight`） | `torch_npu.npu_rms_norm(x, weight, epsilon=eps)` | 返回 tuple，取 `[0]`；最简单、收益稳定 |
| `a1 * F.silu(a2)` 门控乘法 | `torch_npu.npu_swiglu(x, dim=-1)` | concat 顺序必须正确：`[a2, a1]` 让 `SiLU(a2)*a1` |
| 手写旋转位置编码（`t*cos + rotate_half(t)*sin`） | `torch_npu.npu_rotary_mul(t, cos, sin)` | cos/sin 需 `expand_as(t_)`；处理 t_pass_ concat 回来 |
| 手写 `Q@K^T → mask → softmax → @V` | `torch_npu.npu_fusion_attention(...)` | 必须显式 causal mask；atten_mask 中 True=屏蔽；返回 tuple 取 `[0]` |
| MoE expert 循环 matmul | `npu_grouped_matmul` / `Ops.gmm` + `npu_moe_token_permute/unpermute` | 需确认 expert 归属、weight layout、空 expert、routing weight |

**每个算子的详细识别 pattern、原始→NPU 完整代码对照、踩坑点和 concat 顺序推导，见 `references/common_fusion_ops.md`。** 在写替换代码前务必读对应小节——这些算子都有容易踩错的地方（比如 SwiGLU concat 顺序反了语义就错了、Attention 不加显式 causal mask 余弦相似度从 0.999 掉到 0.5）。

### 1.2 高频算子序列识别（advanced，基于 profiling）

如果用户提供了替换前的 profiling 数据（或主动采集了），不要只扫常用算子清单。基于 profiling 里的高频算子序列，能发现常用清单覆盖不到的融合机会。

**三类融合模式，判断要不要尝试：**

- **Cube-Vector fusion**：合并计算 + 向量/数据搬运类操作。减少中间结果落地、改善 cache/locality 时收益大；数据量小时收益有限。
- **Vector-Vector fusion**：合并向量类操作。重复的 load/store/cast 开销被消掉时收益明显。
- **Small-operator fusion**：launch/下发开销占主导时有收益，尤其高频微小算子。

**操作方式**：在 profiling 的 `kernel_details.csv` 或 db 文件里，找属于同一个模块、上下衔接的算子序列（如 `slice+matmul+gelu`），按 pattern 去知识图谱或 `torch_npu` API 列表找现成融合 API，评估能否等价替换。没有现成 API 时，指出"建议做自定义融合算子"并给性能提升估计。**注意算子序列必须属于同一模块、上下衔接，不要跨模块拼凑**——跨模块的算子在硬件上不一定连续执行，拼起来没意义。

如何从 profiling 判断这段代码到底是不是融合有价值的瓶颈，见 `references/fusion_value_assessment.md`（含 Roofline 加速上限表、Memory/Compute/Launch-bound 现象对照、该看哪些字段）。

### 1.3 输出格式

以 markdown 输出识别报告，每个候选替换包含：

```markdown
### 候选 N：[算子名] 替换
- 文件：`path/to/file.py:L12-L34`
- 原始代码逻辑：（简述 / 贴关键行）
- 替换后示例：（完整可运行的替换片段）
- 优先级：高 / 中 / 低（按收益估计 + 置信度）
- 风险：（如 concat 顺序、mask 语义、shape 对齐等易错点）
- 预期收益：（Memory-bound ~2× / Compute-bound 极低 / Launch-bound 中等）
```

### 1.4 阶段一边界（重要）

阶段一结束后**不要直接进阶段二改代码**。先输出识别报告，让用户确认认可哪些替换、做哪些。原因：替换方向需要人把关，验证又是长耗时过程，方向错了浪费极大。用户确认后再进阶段二。

---

## 阶段二：修改代码 + 逐个验证

### 2.1 修改原则：不破坏原有逻辑

通过参数或 `is_torch_npu_available()` 等条件判断进入融合算子分支，**保留原分支做 fallback**。这样融合算子出问题时能切回去，也方便 A/B 对比。

```python
args = get_args()
if args.use_fused_rmsnorm and is_torch_npu_available():
    # aclnnRmsNorm 支持 FP32 gamma，对 FP32/BF16 输入都成立
    output = torch_npu.npu_rms_norm(hidden_states, self.weight.float(), epsilon=self.variance_epsilon)[0]
else:
    # 原 fallback 分支原样保留
    input_dtype = hidden_states.dtype
    hidden_states = hidden_states.to(torch.float32)
    variance = hidden_states.pow(2).mean(-1, keepdim=True)
    hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
    output = (self.weight * hidden_states).to(input_dtype)
return output
```

控制方式优先级：**项目已有参数 > 新增参数 > 环境变量 > 仅判断 `is_torch_npu_available()`**。优先复用项目已有的开关习惯；项目没有相关用法时，临时用环境变量或只判断 NPU 可用性也行。

### 2.2 评测原则：精度优先，逐个对比 baseline

代码可以一次改完，但**评测必须一个一个来**。每次只验证一个替换，和未修改的 baseline 对比精度和性能。

**精度（先过这关）：**
- 跑 50+ 个迭代，修改前后 loss diff 在 1% 以内。
- 如果比较早的迭代就有明显精度 loss 差异，提前结束，没必要跑完浪费时间。
- **精度不满足 → 直接标注不通过，跳过性能评测。** 没收益的替换不值得再花时间测性能。

**性能（精度过了才测）：**
- 不开 profiling：看端到端每个 step 耗时是否有稳定下降。
- 开 profiling：做更细对比——确认算子已替换、替换前那段 device 算子序列总耗时 vs 替换后算子耗时、关注调用次数。**profiling 不要采集太久**，够看就行。

详细评测流程、对比口径、长任务过程记录规范，见 `references/verification_workflow.md`。

### 2.3 长任务过程记录

阶段二是长耗时过程，需要重复拉起任务。期间必须有明确的过程记录和任务列表（哪些替换已验证、哪些待测、结论如何），方便中断后续接。用 TaskList 跟踪每个候选替换的验证状态，结论写进本地 markdown。

---

## 工具

### 1. torch_npu API 查询脚本（首选，确保版本配套）

`scripts/torch_npu_api_ref.py` —— 从当前环境 `torch_npu` 包内的 `_op_plugin_docs.py` 读取 API 文档，跟实际安装版本严格绑定，零版本 drift。**查 API 签名/约束时优先用这个**，比凭记忆或查在线文档可靠。

```bash
# 查单个 API 详情（默认输出 source/sections/summary/signature）
python3 scripts/torch_npu_api_ref.py show npu_fusion_attention
# 输出完整文档正文（带本地行号）
python3 scripts/torch_npu_api_ref.py show npu_fusion_attention --full
# 反向搜索：关键词匹配 API 名或文档内容
python3 scripts/torch_npu_api_ref.py search attention
python3 scripts/torch_npu_api_ref.py search "MoE" --max 20
# 枚举所有算子名（可按前缀过滤）
python3 scripts/torch_npu_api_ref.py list
python3 scripts/torch_npu_api_ref.py list --prefix npu_fusion
```

无 torch_npu 环境时可用 `--docs-path /path/to/_op_plugin_docs.py` 指定文档源，或降级到内嵌 fallback 集（覆盖本 Skill 关心的关键融合算子）。

### 2. 知识图谱（ascend-kg）

通过 ascend-kg 检索跨仓库参考实现、API 约束、错误码→源码映射。写替换代码前如果对某个 API 的版本兼容性/约束不确定，查 KG 而非凭记忆。

### 3. 在线文档

https://gitcode.com/Ascend/op-plugin/tree/master/docs/zh/custom_APIs/torch_npu —— 找不到本地文档时的兜底。


---

## 决策清单

开始任务前快速自检：

- [ ] 用户给的是模型代码/拉起脚本？有没有替换前 profiling 数据（有则进 1.2 advanced 识别）？
- [ ] 常用融合算子清单逐个扫了吗（1.1 必做）？
- [ ] 替换 API 的签名/约束查了 `scripts/torch_npu_api_ref.py` 而非凭记忆吗？
- [ ] 阶段一报告输出后，停下来等用户确认了吗（没直接改代码）？
- [ ] 阶段二逐个验证，精度优先，精度不过直接否决了吗？
- [ ] 长任务过程记录和 TaskList 跟上了吗？
