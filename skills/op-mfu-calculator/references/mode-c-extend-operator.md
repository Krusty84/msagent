# 模式 C：扩展新的算子（注册 FLOPs 公式）

> 当用户明确要计算某个**未在算子 FLOPs 公式表中覆盖**的算子 MFU，或需要为新算子注册 FLOPs 公式以便 msprof-analyze 能识别时，按此流程处理。

按以下流程处理：先走模式 B 的算子查找流程确定 FLOPs 公式（详见 [mode-b-cal.md](mode-b-cal.md)） → 注册到 `_flops_formulas.py` → 采集 Profiling 数据 → 确认打点落盘 → 用 msprof-analyze 解析验证。

## 前置检查

在执行模式 C 之前，先确认以下前置条件：

1. **torch_npu 版本检查**：确认系统中是否存在 `_flops_formulas.py` 文件，路径为 `<torch_npu_module_path>/profiler/_flops_formulas.py`（可通过 `python -c "import torch_npu; print(torch_npu.__file__)"` 查看具体路径）。如果文件不存在，说明当前 torch_npu 版本不支持 FLOPs 注册功能，需要升级 torch_npu 到较新版本后才能继续。

2. **算子是否已注册**：如果该算子**已**在 `_flops_formulas.py` 中注册（即有现成的 FLOPs 打点），则跳过注册步骤，直接进入采集 Profiling 数据 → 确认打点落盘 → 用 msprof-analyze 解析验证即可。

3. **msprof-analyze 版本检查**：执行 `msprof-analyze cluster --help`，确认 `-m` 的可选参数中是否包含 `operator_mfu`。如果不包含，需要先按模式 A 第三步 3.3 小节的步骤源码安装最新版 msprof-analyze（详见 [mode-a-profiling.md](mode-a-profiling.md#33-版本检查确认-msprof-analyze-支持-operator_mfu)）。

4. **确认测试程序**：确认用户是否有调用该算子的 Python 程序。如果没有，询问用户是否需要帮你写一个测试脚本。

## 第一步：确认目标 API

在 `_flops_formulas.py` 中注册，格式为：

```python
@register_npu_flop(target="模块路径:属性名", is_default=True)
```

`target` 参数指向对应的 Python 对象，会被替换为带 FLOPs 计算的 wrapper。例如：

- `torch:mm`
- `torch.nn.functional:linear`
- `torch_npu:npu_fusion_attention`

## 第二步：写公式函数

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
- 写公式前先确认口径：统计主计算还是包含 bias/activation/quant 等融合部分？稀疏场景下算理论满量还是有效计算量？变长场景下真实工作量如何恢复？

## 第三步：验证落盘

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

### 3.1 走模式 A 流程采集 Profiling 数据

按模式 A 流程采集前，先确认用户**是否有调用该算子的程序**：

- **已有程序**：直接按模式 A 的步骤（详见 [mode-a-profiling.md](mode-a-profiling.md)），修改脚本补齐采集配置，运行采集。
- **没有程序**：询问用户是否需要帮你写一个调用该算子的测试脚本，用于验证落盘。如果用户需要，按照模式 A 第一步中的参考示例（Profiler 配置模板 + 算子调用），写一个 Python 测试脚本。脚本写好之后，**询问用户是否现在立即执行**；如果用户同意，再按模式 A 流程运行采集。

### 3.2 确认打点落盘

采集完成后，在 `on_trace_ready` 输出目录中找到 `ascend_pytorch_profiler.db` 或 `ascend_pytorch_profiler_*.db` 文件（多卡场景下任选一个 rank 的 DB 文件确认即可），运行以下 SQL 查询确认新算子的 FLOPs 是否正确打点：

```sql
SELECT
    me.ROWID,
    si_domain.value AS domain,
    si_msg.value AS message
FROM MSTX_EVENTS me
LEFT JOIN STRING_IDS si_domain ON me.domainId = si_domain.id
LEFT JOIN STRING_IDS si_msg ON me.message = si_msg.id
WHERE si_domain.value = 'mfu_flops'
ORDER BY me.ROWID;
```

期望结果：

- `domain` 列为 `mfu_flops`
- `message` 格式为 `<正整数FLOPs>-<op_name>`，例如 `"137438953472-torch::mm"`
- 新算子的记录出现在结果中

### 3.3 确认结果正确

确认打点落盘后，运行 msprof-analyze 解析：

```bash
# -d 必须填 on_trace_ready 输出的 profiling 目录
msprof-analyze --agent -m operator_mfu -d <on_trace_ready输出目录>
```

核对输出结果中该算子的 `flops` 字段，是否与用公式手动计算出的 FLOPs 值一致（可选取简单场景，如固定维度的 `torch.mm`，代入公式算出 FLOPs 再与 `flops` 字段比对）。
