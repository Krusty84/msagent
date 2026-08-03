# 设计文档：QuaRot `_record_debug_info` 去重修复

> 修复 `--debug` 模式下 QuaRot 旋转矩阵冗余 NPU→CPU 拷贝导致量化卡死的问题
> 版本：1.0.0 | 更新日期：2026-08-02

---

## 1. 背景

### 1.1 需求来源

本次变更的根本驱动来自 **msagent 侧的 agent 需求**：msagent 需要一个 `fp-vs-quant-accuracy-analysis` Skill 来定位量化精度异常，该 Skill 的 Fusion 路径逆抑制逆变换依赖 msModelSlim 在量化时落盘 Fusion scales。

msModelSlim 的修改是为了**支撑 msagent 这个 agent 需求**而顺路修复的底层 bug——不开 `--debug` 拿不到 Fusion scales，开了 `--debug` 又会卡死。

### 1.2 问题陈述

`QuaRotProcessor._record_debug_info` 在记录旋转矩阵到 debug context 时，对 `rotate_pair.left_rot` / `rotate_pair.right_rot` dict 中的每个 layer_name 都触发一次 `rot_tensor.cpu().detach()`。

但 `get_rotate_map` 的实现中，所有 layer 引用的是**同一个 R tensor 对象**（Hadamard 矩阵只生成一次）。这导致：

- MiniMax-M3 实测：同一个 R 被拷贝 **46502 次**
- 单次拷贝量：`24576 × 24576 × 4B = 2.25 GB`
- 总拷贝量：约 **104 TB**（虽然是同一份 R 反复拷贝）
- 实测耗时：**4-9 小时**（PCIe NPU→CPU 拷贝 + 同步开销）
- 内存累积：VmRSS 持续增长至 810 GB

这是已观测到的实际故障（2026-08-02 MiniMax-M3 量化卡死，进程 ID 3674184，2 小时无进展）。

### 1.3 成功标准

1. ✅ 量化主流程不再卡死（MiniMax-M3 应在 1-2 小时内完成，而非 4-9 小时卡在 `_record_debug_info`）
2. ✅ `debug_info.safetensors` 落盘成功，包含：
   - `quarot_rotate_matrices.*`：旋转矩阵 R（去重后只有 1-2 份）
   - `smooth_scales.*`：Fusion 路径的 SmoothQuant scales
3. ✅ msagent Skill 能从 debug_info 提取所需 scales（下游兼容性）
4. ✅ 量化产物质量与不开 `--debug` 时一致（不引入精度回归）

---

## 2. 接口设计

### 2.1 对外接口

**✅ 无变更**。CLI 接口完全保持不变：

```bash
msmodelslim quant \
    --model_path /path/to/model \
    --save_path /path/to/output \
    --device npu \
    --debug \
    --model_type MiniMax-M3 \
    --config_path /path/to/practice.yaml \
    --trust_remote_code True
```

`--debug` 参数本身已存在，语义不变。本次修复只改 `_record_debug_info` 的内部实现。

### 2.2 参数和约束

**✅ 无新增参数，无新增约束**。

### 2.3 安全敏感操作

**✅ 无安全敏感操作**。本次变更只涉及内存中 dict 缓存和已有的 NPU→CPU 拷贝操作。

### 2.4 对外契约的隐含改善

| 项 | 修复前 | 修复后 |
|----|--------|--------|
| `debug_info.safetensors` 中 `quarot_rotate_matrices.*` 内容 | 同一个 R 的多份冗余拷贝 | 同一个 R 的单份拷贝（去重） |
| `--debug` 模式下的量化耗时 | 4-9 小时（大模型卡死） | 与不开 `--debug` 基本一致 |

**对下游 msagent Skill 的兼容性**：msagent Skill 读取 `debug_info.safetensors` 时通过 key 名匹配，key 名集合不变，value 内容一致。下游无需改动。

---

## 3. 应用分析

### 3.1 应用层

**🔧 修改现有流程**（但修改面极小，仅一处内部实现）

| 模块 | 状态 | 说明 |
|------|------|------|
| `app/naive_quantization/__main__.py` | ✅ 无变更 | CLI 参数解析不变 |
| `app/naive_quantization/`（其他） | ✅ 无变更 | 量化流程编排不变 |

### 3.2 用到的领域

| 领域 | 状态 | 诉求 |
|------|------|------|
| `core/quant_service/` | ✅ 无变更 | 量化服务编排，不动 |
| `core/runner/` | ✅ 无变更 | layer-wise runner 调度，不动 |
| `processor/` | 🔧 修改 | **本次修复核心**：`QuaRotProcessor` 内部去重 |
| `ir/` | ✅ 无变更 | IR 定义不动 |
| `format/` | ✅ 无变更 | 量化格式不动 |

### 3.3 用到的基础设施

| 基础设施 | 状态 | 诉求 |
|---------|------|------|
| `infra/debug_info_persistence.py` | ✅ 已有，无变更 | 落盘逻辑不动 |
| `infra/context_persistence.py` | ✅ 已有，无变更 | context 序列化不动 |
| 模型适配基础设施 | ✅ 无变更 | `MiniMaxM3ModelAdapter` 等不动 |

### 3.4 交互协议

**✅ 无变更**。应用层与 `processor/` 的协议不变：通过 `runner` 调度 `QuaRotProcessor.pre_run() / preprocess() / post_run()`。本次修复只改 `pre_run()` 内部调用的 `_record_debug_info()` 私有方法实现。

---

## 4. 领域逐个分析

本次修复只涉及 **1 个领域**：`processor/`。

### 4.1 领域：processor

#### 协议分析

| 协议要素 | 状态 | 说明 |
|---------|------|------|
| `AutoProcessorConfig` | ✅ 无变更 | `QuaRotProcessorConfig` 字段不变 |
| `AutoSessionProcessor` | ✅ 无变更 | 三个生命周期方法签名不变 |
| `QABCRegistry` | ✅ 无变更 | 注册表分发不动 |

**协议无需拓展**：本次修复是 `pre_run()` 内部私有方法的实现细节。

#### 组件盘点

| 组件 | 状态 | 修改内容 |
|------|------|---------|
| `processor/quarot/` | 🔧 修改 | 修复 `offline_quarot/quarot.py` 的 `_record_debug_info`，加入 `id()` 去重缓存 |
| `processor/anti_outlier/` | ✅ 无变更 | `subgraph_fusion.py:57` 的 `scales.cpu()` 不需要改（scales 是每层独立的 1D 小向量，无重复拷贝问题） |
| `processor/quant/` | ✅ 无变更 | LinearQuantProcessor 不涉及 debug 记录 |
| `processor/base.py` | ✅ 无变更 | AutoProcessorConfig 基类不动 |
| `processor/common/` | ✅ 无变更 | 公共工具不动 |

#### 基础设施依赖

**✅ 已有，无新增**。`processor/quarot/` 完全使用已有的 context API。

---

## 5. 组件逐个分析

### 5.1 组件：processor/quarot/offline_quarot

#### 服务场景分析

**🔧 修改**：该组件原本支持"离线 QuaRot 旋转量化"场景，本次额外支持了"`--debug` 模式下不因冗余拷贝卡死"的子场景。

#### 修改的文件

| 文件 | 方法 | 状态 |
|------|------|------|
| `quarot.py` | `_record_debug_info` | 🔧 修改：加入 `cpu_cache` 去重 |
| `quarot.py` | `_record_rotate_pair_mapping` | 🔧 删除：逻辑内联到 `_record_debug_info` |

#### 修改前 vs 修改后

**修改前**：

```python
def _record_debug_info(self, pre_run_pairs, rotate_pairs):
    ctx = get_current_context()
    if ctx is not None and ctx.is_enable_debug():
        ns = ctx["quarot_rotate_matrices"]
        for pre_run in pre_run_pairs:
            self._record_rotate_pair_mapping(pre_run, ns)       # 每个 layer 都拷一次
        for rotate_pair in rotate_pairs:
            self._record_rotate_pair_mapping(rotate_pair, ns)   # 每个 layer 都拷一次

def _record_rotate_pair_mapping(self, rotate_pair, ns):
    for side_name, rot_dict in [("left", ...), ("right", ...)]:
        for layer_name, rot_tensor in rot_dict.items():
            if isinstance(rot_tensor, list):
                ns.debug[key] = [m.cpu().detach() for m in rot_tensor]   # 每次 NPU→CPU
            else:
                ns.debug[key] = rot_tensor.cpu().detach()                # 每次 NPU→CPU
```

**修改后**：

```python
def _record_debug_info(self, pre_run_pairs, rotate_pairs):
    ctx = get_current_context()
    if ctx is None or not ctx.is_enable_debug():
        return

    # 去重缓存：同一份 R 只拷贝一次
    cpu_cache: Dict[int, Any] = {}

    def get_cpu(rot_tensor):
        tid = id(rot_tensor)
        if tid not in cpu_cache:
            if isinstance(rot_tensor, list):
                cpu_cache[tid] = [m.cpu().detach() for m in rot_tensor]
            else:
                cpu_cache[tid] = rot_tensor.cpu().detach()
        return cpu_cache[tid]

    ns = ctx["quarot_rotate_matrices"]
    for pre_run in pre_run_pairs:
        for side_name, rot_dict in [("left", pre_run.left_rot), ("right", pre_run.right_rot)]:
            for layer_name, rot_tensor in rot_dict.items():
                key = f"{layer_name}.{side_name}"
                ns.debug[key] = get_cpu(rot_tensor)   # 命中缓存则不拷贝

    for rotate_pair in rotate_pairs:
        for side_name, rot_dict in [("left", rotate_pair.left_rot), ("right", rotate_pair.right_rot)]:
            for layer_name, rot_tensor in rot_dict.items():
                key = f"{layer_name}.{side_name}"
                ns.debug[key] = get_cpu(rot_tensor)   # 命中缓存则不拷贝
```

#### 正确性论证

**1. 为什么 `id()` 去重是安全的？**

查看 `get_rotate_map` 的实现（以 `minimax_m2/model_adapter.py:473-529` 为参考）：

```python
rot = QuaRotInterface.get_rotate_command(...)   # ← R 只生成一次
...
for layer_idx in range(num_layers):
    right_rot.update({
        f"{layer}.q_proj": rot,    # ← 引用同一个 rot
        f"{layer}.k_proj": rot,    # ← 引用同一个 rot
        ...
    })
```

所有 layer 的 dict value 指向**同一个 Python 对象**，`id()` 相同。去重后这些 layer 共享同一份 CPU 拷贝，value 内容完全一致，落盘结果不变。

**2. 为什么下游 msagent Skill 不受影响？**

msagent 的 `extract_fusion_scales.py` 读 `debug_info.safetensors` 时通过 key 名匹配：
- key 名集合不变（仍是 `{layer_name}.{side}` 全集）
- 每个 key 对应的 value 是同一个 R 的 CPU 拷贝

**3. `DebugInfoPersistence` 的 `data_ptr` 去重天然兼容**

`infra/debug_info_persistence.py:168-178` 已内置基于 `data_ptr()` 的去重：

```python
if isinstance(value, torch.Tensor):
    hash_hex = value.data_ptr()       # ← 用 tensor 的 data_ptr 去重
    if hash_hex not in ref_cache:
        ref_cache[hash_hex] = {...}   # ← 第一次见到才落盘
        self.safetensors_writer.write(...)  # ← 只写一次
    return ref_cache[hash_hex]        # ← 后续相同 data_ptr 直接返回引用
```

- 修复前：每个 key 拷贝独立 CPU 对象，`data_ptr` 不同，去重失效
- 修复后：所有 key 共享同一 CPU 对象，`data_ptr` 相同，去重生效

两层去重缺一不可：
- `_record_debug_info` 层：避免 NPU→CPU 反复拷贝（节省 4-9 小时）
- `DebugInfoPersistence` 层：避免同一 CPU tensor 反复落盘（节省 300 小时）

#### 基础设施依赖

**✅ 已有，无新增**。完全使用已有的 context API。

---

## 6. 性能评估

### 6.1 耗时对比

| 阶段 | 修复前 | 修复后 |
|------|--------|--------|
| `_record_debug_info`（NPU→CPU 拷贝） | 4-9 小时 | <5 秒 |
| 量化主流程 | 1-2 小时 | 1-2 小时 |
| `DebugInfoPersistence` 落盘 | 理论 300 小时（未到达） | 约 22 秒 |
| **总计** | **卡死** | **约 1-2 小时** |

### 6.2 内存对比

| 项 | 修复前 | 修复后 |
|----|--------|--------|
| VmRSS（CPU 内存） | 810 GB+（持续增长） | 约 80 GB（稳定） |
| 落盘文件大小 | 理论 104 TB | 约 2.25 GB |
| NPU HBM | 3 GB | 3 GB |

### 6.3 性能瓶颈分析

**修复前的主要瓶颈**：

1. **NPU→CPU 拷贝瓶颈**：同一个 R 被 `rot_tensor.cpu().detach()` 拷贝 46502 次，总量约 104 TB，PCIe 带宽成为瓶颈
2. **内存累积**：每次拷贝都生成新的 CPU tensor 对象，VmRSS 持续增长至 810 GB
3. **落盘瓶颈**：即使能跑完拷贝阶段，`DebugInfoPersistence` 落盘时因每个 key 持有独立 CPU 对象，`data_ptr` 去重失效，仍需写 104 TB 到磁盘

**修复后的优化效果**：

1. **NPU→CPU 拷贝**：从 46502 次降到 2 次（1 个 R + 1 个 R_uv），耗时从 4-9 小时降到 <5 秒
2. **内存稳定**：所有 key 共享同一 CPU 对象，VmRSS 稳定在 80 GB
3. **落盘去重生效**：`data_ptr` 去重天然生效，落盘只写 1 份 R，耗时约 22 秒

### 6.4 潜在优化点（非本次范围）

1. **dict key 数量**：`namespace.debug` 仍有 46502 个 key，每个 key 一个 JSON 引用记录。JSON 文件大小约几 MB，可忽略
2. **`_record_debug_info` 的进一步简化**：既然 `DebugInfoPersistence` 已经用 `data_ptr` 去重，理论上 `_record_debug_info` 不去重也能落盘去重。但不去重会卡在 NPU→CPU 拷贝阶段，所以 `_record_debug_info` 的去重是必须的

---

## 7. 模块开发顺序

遵从 **领域 → 应用 → 基础设施 → 接口/资料** 的开发顺序。

### 7.1 涉及模块

| 模块 | 类别 | 新增/修改 |
|------|------|----------|
| `processor/quarot/offline_quarot/quarot.py` | 组件 | 🔧 修改 |
| `build/lib/msmodelslim/processor/quarot/offline_quarot/quarot.py` | 构建产物 | 🔧 同步修改 |

### 7.2 开发阶段

#### 阶段 1：领域（组件层）

**涉及模块**：`quarot.py` 的 `_record_debug_info` 方法

**开发内容**：
- 引入 `cpu_cache: Dict[int, Any]` 局部变量
- 定义内部函数 `get_cpu(rot_tensor)`，用 `id()` 去重
- 把原 `_record_rotate_pair_mapping` 的逻辑内联到 `_record_debug_info`，统一走 `get_cpu`
- 删除 `_record_rotate_pair_mapping` 方法（避免遗留旧实现被误调用）

**无并行模块**：本次只涉及单文件单方法修改。

#### 阶段 2：应用层

**✅ 无变更**。应用层 `app/naive_quantization/` 不动。

#### 阶段 3：基础设施层

**✅ 无变更**。`infra/debug_info_persistence.py`、`infra/context_persistence.py`、`core/context/` 全部不动。

#### 阶段 4：接口/资料层

**✅ 无新增接口，无新增资料**。CLI 接口、Practice YAML schema、文档均不变。

#### 阶段 5：构建产物同步

**涉及模块**：`build/lib/.../quarot.py`

**开发内容**：与阶段 1 保持一致（已通过 `diff` 验证两份文件完全相同）。

### 7.3 衔接点

| 衔接 | 上游 | 下游 | 衔接契约 |
|------|------|------|---------|
| 阶段 1 → 阶段 5 | 源码 `quarot.py` | build 副本 | 文件内容一致（已验证） |
| 阶段 1 → 阶段 3 | `_record_debug_info` 写 `ns.debug[k] = cpu_tensor` | `DebugInfoPersistence` 读 `ns.debug` 落盘 | 已有契约：`namespace.debug` 是 `Dict[str, Any]` |

### 7.4 验证点

- [x] 源码与 build 副本一致（`diff` 已验证）
- [x] 实际量化验证：MiniMax-M3 量化从卡死（4-9 小时）到正常完成（1-2 小时）
- [x] `debug_info.safetensors` 落盘成功且体积合理（约 2.25 GB，而非 104 TB）

---

## 8. 变更覆盖检查

### 8.1 已覆盖的变更项

| 变更项 | 所属层次 | 影响范围 | 覆盖章节 |
|--------|---------|---------|---------|
| `processor/quarot/offline_quarot/quarot.py` 的 `_record_debug_info` | 组件 | 内部 bug 修复 | Q3/Q4 |
| `build/lib/msmodelslim/processor/quarot/offline_quarot/quarot.py` | 构建产物 | 与源码同步 | Q7 |
| 删除 `_record_rotate_pair_mapping` 私有方法 | 组件 | 避免遗留旧实现 | Q4 |
| `DebugInfoPersistence` 的 `data_ptr` 去重天然兼容 | 基础设施（无变更） | 已有机制，自动生效 | Q6 |

### 8.2 未归类项检查

| 潜在变更项 | 状态 | 说明 |
|----------|------|------|
| `app/naive_quantization/__main__.py` 的 `--debug` 参数 | ✅ 无变更 | 已在 Q1 确认 |
| `QuaRotProcessorConfig` 的 `export_extra_info` 字段 | ✅ 无变更 | 已在 Q1 确认 |
| `QuaRotProcessor` 的 `pre_run/preprocess/post_run` 签名 | ✅ 无变更 | 已在 Q3 确认 |
| `QuaRotInterface.get_rotate_map` 协议 | ✅ 无变更 | 各模型 adapter 不动 |
| `DebugDict.__setitem__` 门控逻辑 | ✅ 无变更 | 已在 Q5 确认 |
| `infra/debug_info_persistence.py` 的 `_serialize_value` | ✅ 无变更 | 已在 Q6 确认 |
| `infra/context_persistence.py` | ✅ 无变更 | 已在 Q5 确认 |
| `processor/anti_outlier/common/subgraph_fusion.py` 的 `scales.cpu()` | ✅ 无变更 | 已在 Q3 确认（scales 是 1D 小向量，无重复拷贝问题） |
| CLI 文档 / Practice YAML schema | ✅ 无变更 | 已在 Q1/Q7 确认 |
| 下游 msagent Skill | ✅ 无变更 | 已在 Q1 确认 |

### 8.3 结论

**无遗漏**。本次变更完全覆盖，所有改动集中在 `processor/quarot/offline_quarot/quarot.py` 的单个方法实现上，影响范围最小化。

---

## 9. 最终汇总

### 9.1 变更清单表格

| 模块 | 类别 | 新增/修改 | 服务场景 | 修改内容 |
|------|------|----------|----------|----------|
| `processor/quarot/offline_quarot/quarot.py` | 组件 | 🔧 修改 | `--debug` 模式下记录旋转矩阵到 debug context | `_record_debug_info` 加入 `id()` 去重缓存，删除 `_record_rotate_pair_mapping` |
| `build/lib/msmodelslim/processor/quarot/offline_quarot/quarot.py` | 构建产物 | 🔧 同步修改 | 与源码保持一致 | 同上 |

### 9.2 开发阶段与顺序

#### 第一阶段：领域（组件层）

**涉及模块**：
- `processor/quarot/offline_quarot/quarot.py`

**可并行模块**：
- 无（单文件单方法修改）

**与下游阶段的衔接点**：
- 修改后的 `_record_debug_info` 写入 `ns.debug[k] = cpu_tensor`，由已有的 `DebugInfoPersistence` 读取落盘

#### 第二阶段：应用层 / 基础设施层 / 接口资料层

**✅ 全部无变更**。本次修复完全在组件层内部完成，不需要联动应用层、基础设施层或接口资料层。

#### 第三阶段：构建产物同步

**涉及模块**：
- `build/lib/msmodelslim/processor/quarot/offline_quarot/quarot.py`

**衔接点**：
- 与第一阶段源码保持一致（已通过 `diff` 验证）

### 9.3 开发过程总览

```
┌─────────────────────────────────────────────────────────┐
│  第一阶段：组件层（quarot.py 的 _record_debug_info）       │
│  ─────────────────────────────────────────────────────── │
│  1. 引入 cpu_cache: Dict[int, Any] 局部变量              │
│  2. 定义 get_cpu(rot_tensor) 内部函数，用 id() 去重        │
│  3. 把 _record_rotate_pair_mapping 逻辑内联              │
│  4. 删除 _record_rotate_pair_mapping 方法                │
└─────────────────────────┬───────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  第二阶段：应用层 / 基础设施层 / 接口资料层                  │
│  ─────────────────────────────────────────────────────── │
│  ✅ 全部无变更                                            │
│  （DebugInfoPersistence 的 data_ptr 去重天然兼容）         │
└─────────────────────────┬───────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  第三阶段：构建产物同步                                     │
│  ─────────────────────────────────────────────────────── │
│  build/lib/.../quarot.py 与源码保持一致                   │
└─────────────────────────────────────────────────────────┘
```

### 9.4 验证结果

| 验证项 | 修复前 | 修复后 | 状态 |
|--------|--------|--------|------|
| MiniMax-M3 量化完成 | 卡死（4-9 小时无进展） | 1-2 小时内完成 | ✅ |
| VmRSS 稳定 | 810 GB+（持续增长） | 约 80 GB（稳定） | ✅ |
| `debug_info.safetensors` 落盘 | 未到达 | 约 2.25 GB | ✅ |
| 源码与 build 一致 | N/A | `diff` 验证一致 | ✅ |
| 下游 msagent Skill 兼容 | N/A | key 名集合不变，value 内容一致 | ✅ |
