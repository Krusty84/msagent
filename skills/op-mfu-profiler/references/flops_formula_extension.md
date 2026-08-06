# 扩展注册新算子 FLOPs 公式

当目标算子没在 `torch_npu.profiler._flops_formulas.py` 里注册时，msprof-analyze 算不出它的 MFU。本文件描述通过 `@register_npu_flop` 自行扩展注册、并验证打点生效的完整流程。

## 第 1 步：确定 FLOPs 公式（按优先级，不可跳级硬估）

1. **参考 `_flops_formulas.py` 中已有的同类算子**（第一优先）：先读文件里已注册的同类算子公式函数，仿照其代码风格和入参处理模式推导目标算子公式。
2. **让用户提供算子实现代码**（第二优先）：没有同类算子可参考时，问用户要该算子的实现代码或源码链接，**等用户回应**后据实现手动推导。
3. **推导不出来**：直接告知无法推导、不继续往下执行。**不要强行估算**——错误的 FLOPs 比「没有」更糟，会让后续 MFU 全失真。

## 第 2 步：注册到 `_flops_formulas.py`

`target` 指向对应 Python 对象，hook 会把它替换成带 FLOPs 计算的 wrapper，例：

- `torch:mm`
- `torch.nn.functional:linear`
- `torch_npu:npu_fusion_attention`

**写公式函数前务必先读 `_flops_formulas.py` 里已有函数**，仿照其风格与入参处理模式，不要凭空造。骨架：

```python
@register_npu_flop(target="torch_npu:my_new_op", is_default=True)
def my_new_op_flops(x, weight, *, transpose=False, group_list=None, **kwargs):
    m, k = x.shape[-2], x.shape[-1]
    n = weight.shape[-1]
    return 2 * m * k * n
```

要点（解释 why，不是死规矩）：

- 函数只算 FLOPs、不带副作用——hook 层会缓存结果再打点，副作用会污染采集数据。
- 用 `**kwargs` 兜底可选参数——torch_npu 版本间 API 偶有差异，缺兜底会导致 wrapper 注册失败、整个算子采不到。
- 遇到不合法 shape 直接抛异常即可——hook 层会捕获并跳过该次打点，不会中断训练。

## 第 3 步：修改后必须展示确认

改完不要直接跑，先把改动呈现给用户确认：

```text
修改文件：<torch_npu_module_path>/profiler/_flops_formulas.py

修改点：
  - 新增 @register_npu_flop(target="torch_npu:xxx")
  - 新增 def xxx_flops(...) 公式函数
```

## 第 4 步：采集验证

代码加完后按流程验证 FLOPs 是否真的落盘且被解析。

### 采集 Profiling 数据

- **已有调用该算子的程序**：按 SKILL.md「第 2 步：采集配置检查」补齐四项必须配置后跑采集。
- **没有程序**：写个最小测试脚本（参考 `collection_template.md` 的模板 + 算子调用）再跑采集。

### 用 msprof-analyze 验证

1. 按 SKILL.md「第 4 步 → 确认 FLOPs 采集成功」的方法确认是否打点生效了，期望结果在通用基础上多一条：

- `message` 格式为 `<正整数FLOPs>-<op_name>`，例如 `137438953472-torch::mm`。
- **新算子的记录出现在结果中**。

2. 按 SKILL.md「第 4 步：解析」跑解析（默认 db 格式）。db 格式下产物为 `cluster_analysis_output/cluster_analysis.db`，打开其中的 `OperatorMFU` 表，检查**是否出现该新算子**（需直接看 Excel 时可加 `--export_type text` 导出 `OperatorMfu/operator_mfu_kernel_{rank_id}.xlsx`）：

- **未出现**：说明 FLOPs 打点未生效，回头查公式注册是否有误后重试。
- **已出现**：选个简单场景（如固定维度的 `torch.mm`）用公式手算 FLOPs，与记录的 `flops` 字段比对，一致才算闭环。
