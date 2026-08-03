

## 第一步：确定 FLOPs 公式

确定 FLOPs 公式，按以下优先级依次处理：

1. **参考 `_flops_formulas.py` 中已有的同类算子**（第一优先）：先阅读文件中已注册的同类算子公式函数，参考其代码风格和入参处理模式，推导出目标算子的 FLOPs 公式。
2. **让用户提供算子实现代码**（第二优先）：没有同类算子可参考时，询问用户是否有该算子的实现代码或源码链接，等待用户回应后根据实现代码手动推导。
3. **推导不出来**：如果以上途径都无法确定 FLOPs 公式，**直接告诉用户无法推导，不继续往下执行**，不要强行估算。


## 第二步：注册到 `_flops_formulas.py`

### 2.1 确认目标 API

在 `_flops_formulas.py` 中注册，格式为：

```python
@register_npu_flop(target="模块路径:属性名", is_default=True)
```

`target` 参数指向对应的 Python 对象，会被替换为带 FLOPs 计算的 wrapper。例如：

- `torch:mm`
- `torch.nn.functional:linear`
- `torch_npu:npu_fusion_attention`

### 2.2 写公式函数

**先阅读 `_flops_formulas.py` 文件中已有的公式函数代码，参考其代码风格和入参处理模式**，然后仿照已有代码编写。

新增公式函数，入参签名尽量贴近真实 API：

```python
@register_npu_flop(target="torch_npu:my_new_op", is_default=True)
def my_new_op_flops(x, weight, *, transpose=False, group_list=None, **kwargs):
    m, k = x.shape[-2], x.shape[-1]
    n = weight.shape[-1]
    return 2 * m * k * n
```

注意事项：

- 公式函数只做 FLOPs 计算，不要有副作用
- 用 `**kwargs` 兜底可选参数，避免版本差异导致 wrapper 失败
- 遇到不合法 shape 可以直接抛异常，hook 层会捕获并跳过该次打点

## 第三步：采集并验证

代码添加完成后，**询问用户是否要按以下步骤验证**。如果用户同意，按流程操作：

> **注意**：修改完成后，**必须先展示修改的文件路径和修改内容**，让用户确认是否正确。格式如：
>
> ```text
> 修改文件：<torch_npu_module_path>/profiler/_flops_formulas.py
>
> 修改点：
>   - 新增 @register_npu_flop(target="torch_npu:xxx")
>   - 新增 def xxx_flops(...) 公式函数
> ```

### 3.1 采集 Profiling 数据

按主体流程采集前，先确认用户**是否有调用该算子的程序**：

- **已有程序**：按 SKILL.md 中的主体流程步骤，修改脚本补齐采集配置，运行采集。
- **没有程序**：询问用户是否需要帮你写一个调用该算子的测试脚本，用于验证。如果用户需要，按照 SKILL.md 第一步中的参考示例（Profiler 配置模板 + 算子调用），写一个 Python 测试脚本。脚本写好之后，**询问用户是否现在立即执行**；如果用户同意，再按主体流程运行采集。

### 3.2 用 msprof-analyze 验证

**按主体流程运行 `msprof-analyze` 解析 MFU**（详见 SKILL.md 第三步）。

直接检查分析结果（`operator_mfu_kernel_{rank_id}.xlsx` 或 `cluster_analysis.db` 的 `OperatorMFU` 表）中**是否出现该新算子**的记录：

- **未出现**：说明 FLOPs 打点未生效，检查公式注册是否有误后重试。
- **已出现**：核对该算子记录的 `flops` 字段，是否与用公式手动计算出的 FLOPs 值一致（可选取简单场景，如固定维度的 `torch.mm`，代入公式算出 FLOPs 再与 `flops` 字段比对）。
