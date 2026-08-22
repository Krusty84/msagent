# EP 并行适配验收检查清单

## 一、MoE 模型检查（前置）

确认目标模型是否适用 EP 适配：

- [ ] 读取模型 `config.json` / 模型代码，确认存在 routed experts（`num_local_experts` / `num_experts` / `n_routed_experts` > 0）
- [ ] 记录关键参数：`model_type`、`num_experts`、`experts_per_token`、`expert_container`、`expert_weights`
- [ ] 无 routed experts → 回传 `requires_ep=false`，无需 EP，返回单卡 / DP 流程

## 二、EP 适配硬检查（必须全部通过）

### EP Check 1：每卡专家数

```text
expected_local_experts = total_experts / ep_size
actual_local_experts   = 从专家容器中统计的已 materialized 非 None 专家数
```

要求：`actual_local_experts == expected_local_experts`

### EP Check 2：专家区间覆盖

所有 rank 的专家集合必须：

- 覆盖全部 routed experts
- 无遗漏
- 无非预期重复

连续分片时验证：

```text
rank0.end == rank1.start
rank1.end == rank2.start
...
first.start == 0
last.end == total_experts
```

### EP Check 3：非本地专家不驻留

检查每张卡：

- 非本地 routed expert 的 `module is None` 或 `weight.device` 为 meta 或未初始化
- 不能出现每卡仍持有全部 expert 权重参数

### EP Check 4：专家权重按 rank 加载

通过日志或 state_dict 加载路径确认：

- rank N 只读取 `local_expert_ids` 对应的 checkpoint key
- 不读取其他 rank 的 expert 权重

### EP Check 5：ModelSlim mapping 只访问本地专家

确认以下 mapping 均使用 `local_expert_ids` / `_get_expert_range()` 而非 `range(total_experts)`：

```text
Smooth mapping       PASS/FAIL
QuaRot mapping       PASS/FAIL
Up/Down mapping      PASS/FAIL
LN fuse mapping      PASS/FAIL
layer-wise loading   PASS/FAIL
```

### EP Check 6：多卡日志含 EP_CHECK

```text
[EP_CHECK] rank=0 ep_rank=0 ep_size=4 layer=3 total_experts=256 local_experts=64 expert_range=[0,64) non_local_experts=192
[EP_CHECK] rank=3 ep_rank=3 ep_size=4 layer=3 total_experts=256 local_experts=64 expert_range=[192,256) non_local_experts=192
```

要求：

- 日志中存在 `[EP_CHECK]` 且 `ep_size >= 2`
- 所有 rank 的 expert_range 连续覆盖 [0, total_experts)
- 若日志中无 `[EP_CHECK]` → EP 未生效 → **FAIL**

## 三、适配交付验收

适配完成后，向 orchestrator 回传结构化结论，而非继续量化调优：

```text
EP_ADAPT_RESULT=PASS
requires_ep=true
ep_size=<卡数>
total_experts=<专家总数>
experts_per_rank=<每卡专家数>
coverage=PASS
local_only=PASS
mapping_local_only=PASS
ep_check_log=<[EP_CHECK] 日志文件绝对路径>
```

或（非 MoE，无需 EP）：

```text
EP_ADAPT_RESULT=PASS
requires_ep=false
```

任一硬检查失败即回传：

```text
EP_ADAPT_RESULT=FAIL
```

## 常见失败场景

| 检查项 | 常见失败原因 | 判定 |
|--------|-------------|------|
| EP Check 1 | 未修改 expert 构造，每卡仍持有全部专家 | FAIL |
| EP Check 3 | forward 只走本地专家，但 `state_dict` 仍读全部权重 | FAIL |
| EP Check 5 | 忘记修改 `get_rotate_map` / `get_adapter_config_for_subgraph` | FAIL |
| EP Check 6 | 使用单卡，无 EP_CHECK 日志 | FAIL |

> 改造细节见 `ep_implementation_guide.md`（专家分片、权重按 rank 加载、mapping 本地化）
> 与 `ep_quant_mapping_guide.md`（Smooth/QuaRot/LN fuse 等量化映射的 EP 本地化）。