# OOM 诊断指南

## 1. 数据准备

OOM 场景需采集时配置 `--analysis=oom[:K]`（K∈[1,1000]，默认 10）。采集后 OOM 诊断数据写入 `memscope_dump_{ts}.csv`，筛选 `Event=OOM_DETAIL` 查看。

- K 越大记录越多，OOM 时刻 dump 耗时越长。
- `--analysis=oom` 自动联动开启 alloc/free 采集，无需额外配置 `--events`。
- 建议采集时开启 `--call-stack`（Python 与 C），否则 OOM 记录无调用栈，无法归因到代码位置。

## 2. 诊断步骤

### 第一步：看 OOM_TRIGGER（触发操作）

筛选 `Event Type=OOM_TRIGGER`：

| Attr 键 | 解读 |
| --- | --- |
| func | 哪个函数触发了 OOM（如 aten 算子、框架分配接口） |
| req_size | 申请了多大内存（判断是"巨量申请"还是"总量已满"） |
| ret | 驱动返回码（OOM 特征返回码） |

**要点**：`req_size` 巨大 → 单次申请不合理（如一次性分配超大 Tensor）；`req_size` 不大 → 说明总量已耗尽，看第二步的大头占用。

### 第二步：看 OOM_TOP_ALLOC（最大未释放记录）

筛选 `Event Type=OOM_TOP_ALLOC`（按大小排序的最大 K 条未释放记录）：

- 这些记录是 OOM 时刻**占用空间最大**的分配，即显存的"大头"。
- 结合 `owner`（若开启 decompose）、`step`、`kernel`、Call Stack 归因：权重？kv_cache？激活值？临时中间量？

### 第三步：看 OOM_RECENT_ALLOC（最近未释放记录）

筛选 `Event Type=OOM_RECENT_ALLOC`（按时间排序的最近 K 条未释放记录）：

- 这些是 OOM 前**最后发生的分配**，即"压死骆驼的最后一根稻草"。
- 结合 `timestamp` 与 OOM_TRIGGER 的时间差，判断是同一时刻的连续申请还是逐步累积。

### 第四步：综合归因

**定性决策树**：

```
OOM 场景归因判断
   │
   ├─ req_size 巨大（≈ 剩余空间）─────► 单次巨量申请：检查 batch/序列长度/大 Tensor 分配
   ├─ TOP_ALLOC 为权重/优化器状态 ────► 模型显存超配：检查模型规模 vs 卡显存
   ├─ TOP_ALLOC 为 kv_cache ─────────► 推理场景：检查 kv_cache 配置/上下文长度
   ├─ TOP_ALLOC 为 aten 中间量 ───────► 激活/中间量：检查 batch/序列长度/重计算配置
   ├─ RECENT_ALLOC 集中且总量接近上限 ─► 累积型 OOM：检查泄漏（转泄漏诊断）或缓存增长
   └─ 无法归因（无 call stack）───────► 明确告知用户需补采（加 --call-stack）
```

**量化推断规则**（辅助定性判断，规则为启发式；OOM 时刻剩余空间取最近的 SNAPSHOT `free_mem`，无快照时用 `device_used` 曲线反推并标注）：

| 证据（msmemscope 字段） | 判定规则 | 推断根因 | 建议方向 |
| --- | --- | --- | --- |
| `OOM_TRIGGER.req_size` vs 剩余空间 | `req_size` ≥ 80% × OOM 时刻剩余空间 | 单次巨量申请 | 检查 batch/序列长度/大 Tensor 一次性分配 |
| `OOM_RECENT_ALLOC` 前 5 条 | 同一调用栈出现 ≥3 次 | 同栈重复分配（循环/固定路径反复申请） | 定位该调用栈所在循环，检查循环内累积申请 |
| `OOM_RECENT_ALLOC` 前 5 条 | 5 条 `size` 累计 > 3 × OOM 时刻剩余空间 | 一连串大分配（多块接力耗尽） | 检查批量连续申请路径（如多算子大中间量） |
| OOM 时刻剩余空间 | 剩余充足（>1GB）却仍 OOM，且 RECENT 单块 ≤ 剩余 | 单次过大分配请求（非累积） | 与第一行结合确认，检查单点分配 |
| `OOM_RECENT_ALLOC` 分布 + used 曲线 | RECENT 随 Step 均匀分布、总量随 Step 增长、TOP_ALLOC 单块不大 | 累积型（泄漏/缓存增长） | 转泄漏诊断（SKILL B3 ④），检查增长源 |
| `OOM_TOP_ALLOC` owner | `weight`/`optimizer_state` | 模型显存超配 | 模型规模 vs 卡显存 |
| | `kv_cache` | 推理 KV 配置 | kv_cache 大小/上下文长度 |
| | `aten` | 激活/中间量 | batch/序列长度/重计算配置 |

> 多条规则命中时按层级归因：先单次巨量（第一步 req_size）→ 再大头归属（TOP_ALLOC）→ 再分配模式（RECENT 量化规则）→ 累积型最后排除；与框架池扩容失败 OOM（SKILL B3 ⑤ 第 5 点）区分：扩容失败时 `func` 指向池扩容路径，此时关注池内使用率而非分配模式。

## 3. 典型场景解读示例

### 示例 A：训练中途 OOM（激活值）

- OOM_TRIGGER：func=某个 forward 算子，req_size=2GB。
- OOM_TOP_ALLOC：多条 `aten` owner 记录（各 1~4GB），Call Stack 指向 transformer layer forward。
- 结论：batch/序列长度下的激活值总量超限。建议：减小 batch、开启重计算（activation checkpointing）、改用更小的中间量。

### 示例 B：推理长稳 OOM（kv_cache）

- OOM_TRIGGER：func=kv_cache 分配接口，req_size 较小。
- OOM_TOP_ALLOC：`kv_cache` owner 记录，大小随生成推进持续增长。
- 结论：KV cache 随序列长度增长耗尽显存。建议：限制上下文长度、调整 kv_cache 复用策略、扩容。

### 示例 C：累积型 OOM（疑似泄漏）

- OOM_TRIGGER：func=普通分配，req_size 很小。
- OOM_TOP_ALLOC：单块不大，但 OOM_RECENT_ALLOC 与曲线显示总量持续攀升。
- 结论：显存增长累积导致 OOM，转泄漏诊断流程定位增长源。

## 4. 注意事项

- OOM 数据仅采集 OOM 发生时刻前后未释放的记录，不影响正常 malloc/free 热路径性能。
- OOM 通常出现在采集区间内（immediate 模式为整个运行期），若 dump 中无 OOM_DETAIL，确认采集配置并重采。
- `req_size`/`size` 单位为字节，报告时换算为 MB/GB 并标注。
