# msMemScope 采集数据理解与通用解读（字段 / 口径 / 解读方法）

> 本文档对工具采集的数据做**通用的理解和解释**（粗粒度分析）：数据文件与字段口径（§1~§3）、曲线/大头/峰值等通用解读方法（§4~§9）。
> **下一篇**：具体问题（OOM/泄漏）的诊断步骤见 `{scenario}_diagnosis_guide.md`（`oom_diagnosis_guide.md` / `leak_diagnosis_guide.md`）；框架/组件内存逻辑见 `{framework}_memory_management.md`（`pta_memory_management.md` / `vllm_ascend_memory_management.md`）与 `{component}_memory_detail.md`（`hccl_memory_detail.md`）。
> 与 msMemScope 官方《输出文件说明》对齐。字段以实际落盘数据为准（版本演进可能调整）。

## 1. 数据文件与列结构

### 1.1 输出文件总览

| 文件 | 内容 |
| --- | --- |
| `memscope_dump_{ts}.csv` / `.db` | 内存事件数据（列结构见 §1.2，字段口径见 §2/§3） |
| `memory_compare_{ts}.csv` | Step 间对比结果（字段见 §1.4） |
| `python_trace_{TID}_{ts}.csv` | Python Trace 数据（字段见 §1.5） |
| `config.json` | 采集配置信息（如 start/stop 时间记录，shadow 判别用） |

### 1.2 memscope_dump_{timestamp}.csv 列结构

```
ID,Event,Event Type,Name,Timestamp(ns),Process Id,Thread Id,Device Id,Ptr,Attr,Call Stack(Python),Call Stack(C)
```

| 列 | 说明 |
| --- | --- |
| ID | 事件 ID |
| Event | 事件类型（见下） |
| Event Type | 事件子类型 |
| Name | 与 Event 相关：ACCESS 为算子名/ID、OP_LAUNCH 为算子名、KERNEL_LAUNCH 为 kernel 名、MSTX 为打点名、OOM_DETAIL 为 OOM_Trigger/OOM_RecentAlloc/OOM_TopAlloc，其余为 N/A |
| Timestamp(ns) | 事件时间（纳秒） |
| Process Id / Thread Id | 进程号 / 线程号 |
| Device Id | 设备：数值为 NPU 卡序号，"cpu" 为 CPU |
| Ptr | 内存地址，标识内存块：同一 ptr 从 malloc 到下一次 free 为一个生命周期 |
| Attr | 事件属性（JSON 风格键值，见 §2） |
| Call Stack(Python) | Python 调用栈（可选，需采集时配置 call_stack） |
| Call Stack(C) | C 调用栈（可选） |

### 1.3 Event 类型

| Event | 说明 |
| --- | --- |
| SYSTEM | 系统级事件（Event Type：ACL_INIT / ACL_FINI） |
| MALLOC | 内存申请 |
| FREE | 内存释放 |
| ACCESS | 内存访问（Event Type：READ / WRITE / UNKNOWN） |
| OP_LAUNCH | 算子执行（Event Type：ATEN_START / ATEN_END / ATB_START / ATB_END） |
| KERNEL_LAUNCH | kernel 执行（Event Type：KERNEL_LAUNCH / KERNEL_START / KERNEL_END） |
| MSTX | 打点（Event Type：Mark / Range_start / Range_end） |
| SNAPSHOT | 内存快照数据 |
| OOM_DETAIL | OOM 详细分析数据 |

### 1.4 memory_compare_{timestamp}.csv 字段

| 字段 | 说明 |
| --- | --- |
| Event | OP_LAUNCH / KERNEL_LAUNCH |
| Name | kernel 名称 |
| Device Id | 设备类型、卡号 |
| Base | input 第一个文件路径中的数据 |
| Compare | input 第二个文件路径中的数据 |
| Allocated Memory(byte) | kernel 调用前后的内存变化（N/A 表示不存在该 kernel） |
| Diff Memory(byte) | Base 与 Compare 的内存相对变化（0 表示无差异） |

### 1.5 python_trace_{TID}_{timestamp}.csv 字段

| 字段 | 说明 |
| --- | --- |
| FuncInfo | 函数名 |
| StartTime(ns) / EndTime(ns) | 起止时间戳（与 memscope_dump 时间轴一致） |
| Thread Id / Process Id | 线程 / 进程 ID |

## 2. Attr 键说明（memscope_dump）

> **Attr 格式**：`{k:v,k2:v2}` 自定义格式——**键无引号**（如 `{allocation_id:1,addr:0x123,used:4096}`），值可能为 hex 数字或字符串，**不是合法 JSON，不能用 `json.loads` 解析**；分析用脚本（`scripts/aggregate_dump.py`、`scripts/convert_dump.py`）内置 `parse_attr` 已兼容（先剥花括号再按逗号拆分），复用脚本即可，勿手写正则解析。

### 2.1 MALLOC / FREE（内存池类型）

| 键 | 说明 |
| --- | --- |
| allocation_id | 相同 allocation_id 属于同一块内存的操作 |
| addr | 地址 |
| size | 本次申请/释放的大小（字节） |
| owner | 内存块所有者（多级分类 `{A}@{B}@{C}...`），**仅 MALLOC** 存在；仅开启 decompose 时含显存类别与组件名称 |
| total | 内存池总大小（仅 Event Type 为 PTA/MindSpore/ATB 时存在）。PTA 池 total = 池向驱动申请的**段总量**（reserved 视角，各流池段的总和；PTA 池事件**无 stream 字段**，按 device 聚合上报） |
| used | 内存池二次分配累计大小（仅池类型事件）。PTA 池 used = 池内**活跃块累计**（allocated 视角，块级事件累加） |
| process_used | 本进程显存使用量（池事件 = HAL 累计） |
| device_used | 整卡显存用量（与 npu-smi 同源；环境不支持时为 -1/省略） |
| page_type | HAL 类型：normal / huge / giant（仅 Event Type 为 HAL） |
| alloc_type | HAL 分配类型：alloc / create（仅 Event Type 为 HAL）。`alloc` = 普通段申请（`halMemAlloc`，对应 `aclrtMallocAlign32` 等）；`create` = expandable 模式物理页申请（`halMemCreate`，对应 `AclrtMallocPhysical`+`AclrtMapMem`）——看到 create 即说明框架开启 expandable_segments 模式 |
| shadow | 幽灵释放标记（仅 FREE 存在）：`true` 表示该释放事件为幽灵释放，有两类来源——非采集期正常释放与进程退出时统一释放，判别方法见 [[leak_diagnosis_guide]] §1/§4。⚠️ **幽灵机制使 dump 中每条 MALLOC 必有配对 FREE（真实或 shadow）——配对不能证明无泄漏** |

**内存池类型（Event Type）语义**：`HAL` 为 host 侧经驱动接口（`aclrtMemAlloc` 等，下层调用 hal 层接口）申请的内存，其总量是各框架池总量的**父集**；`PTA`（PyTorch-for-Ascend）、`MindSpore`、`ATB` 均由框架向驱动申请内存池后自行维护——模型触发申请时从池中分配、释放时回收到池、池空间不足时扩容（再次调 `aclrtMemAlloc` 等），**扩容失败会抛出 OOM**（池模型细节与分析入手顺序见 §5）。

### 2.2 ACCESS / OP_LAUNCH / KERNEL_LAUNCH

**ACCESS 事件**：`dtype`、`shape`、`size`、`format`、`type`（内存池类型）、`allocation_id`（PTA 时存在）。

**OP_LAUNCH（ATB_START/ATB_END）**：`path`（算子在模型中的位置，含 pid/模块名/算子名）、`workspace ptr`、`workspace size`。

**KERNEL_LAUNCH**：`path`（kernel 位置，仅 KERNEL_START/KERNEL_END 存在）、`streamId`、`taskId`。

### 2.3 SNAPSHOT（内存快照）

| 键 | 说明 |
| --- | --- |
| total_mem | 设备总内存 |
| free_mem | 设备总空闲内存 |
| reserved | torch 框架预留总内存 |
| peak_reserved | torch 框架预留内存峰值 |
| allocated | torch 框架使用内存 |
| peak_allocated | torch 框架使用内存峰值 |
| device_utilization | 设备内存使用率 |
| pt_utilization | torch 预留内存使用率 |

> `reserved`/`allocated` 即 torch memory_stats 的同视角值，`peak_*` 为**事件驱动**更新（段申请/块下发时更新，释放/empty_cache 不降 peak）。

### 2.4 MALLOC/FREE（HOST_PINNED）

`addr`、`size`、`pinned`（是否锁页）、`used`（本进程已使用 HOST 物理内存）。

### 2.5 OOM_DETAIL

| Event Type | 键 | 说明 |
| --- | --- | --- |
| OOM_TRIGGER | func | 触发 OOM 的函数名 |
| | req_size | 申请内存大小 |
| | flag | 申请标志位 |
| | ret | 驱动返回码 |
| OOM_RECENT_ALLOC | pool | 内存池类型 |
| | ptr | 内存地址 |
| | size | 分配大小 |
| | timestamp | 分配时间戳 |
| | step | 所属 Step 编号 |
| | kernel | 所属 kernel 编号 |
| | client | 客户端进程 ID |
| | （Call Stack(Python)/(C)） | 调用栈 |
| OOM_TOP_ALLOC | 同 OOM_RECENT_ALLOC | 按大小排序的最大 K 条 |

> 脚本汇总：`--oom [--detail N]` 将三类记录分表输出，并依据附近 SNAPSHOT `free_mem` 自动给出量化推断提示（判定规则见 `oom_diagnosis_guide.md` §2）。

## 3. 统计键口径（曲线分析用）

| 键 | 含义 | 使用建议 |
| --- | --- | --- |
| `used` | 进程内显存使用量（HAL 事件 = HAL 申请累计，段级，含各框架池向驱动申请的池内存；池事件 = 池二次分配累计，块级） | 绘制"本进程使用曲线"；**区分层级**：池 used 反映框架当前活跃块，HAL used 反映已从驱动申请总量，两者不可直接对比 |
| `device_used` | 整卡物理显存占用总量（dcmi 查询，与 npu-smi 的 HBM-Usage 一致） | 对照 npu-smi；环境不支持时为 -1 |
| `process_used` | 本进程显存占用总量（池事件 = HAL 累计；HOST 事件 = VmRSS；与 npu-smi 的 Process memory 一致） | 对比本进程 vs 整卡差值 |
| `total` | 内存池容量（仅 PTA/MindSpore/ATB 等池事件），即池向驱动申请的内存大小。PTA 池 total = 各流池段总和（按 device 聚合，无 stream 字段） | 池预留水位；total 变大 = 扩容（伴随 HAL 事件，`alloc_type` 见 §2.1） |

**口径说明**：

- **事件时刻之后的值**：`used`/`device_used` 等统计量为**事件时刻之后**的值——MALLOC 事件显示申请后的用量，FREE 事件显示释放后的用量。
- **层级关系**：`device_used`（整卡）≥ `process_used`（本进程）≥ HAL `used`（本进程 HAL 申请）。工具运行在 host 侧，只能感知 host 侧代码直接/间接调用驱动接口申请的显存。
- `device_used` 构成：NPU 片上 RTOS 占用 + 片上程序申请 + AICPU 算子申请（可能有）+ 业务进程占用（主要）；`process_used` 构成：框架内存池（主要）+ CANN 各组件 host 侧申请 + 三方库/自定义代码调 acl 接口申请。
- HAL `used` 与 `process_used` 的差值一般为 HCCP 占用。
- `device_used` 查询失败（环境无卡/CANN 不支持）时字段为 `-1` 或省略，此时用 `used`/`process_used`。
- **池事件语义速记**（PTA 为例，其余框架池同构）：池 `used` = 池内活跃块累计（allocated 视角，张量级）；池 `total` = 池向驱动申请的段总量（reserved 视角，只增不减——释放不归还驱动，仅 empty_cache / OOM 自动回收 / GC 阈值 / expandable unmap 时回落）。详见 [[pta_memory_management]]。

## 4. 曲线趋势解读

曲线为**事件驱动**：仅 HAL/内存池 MALLOC/FREE 事件时刻有点，两事件之间为阶梯保持，不是轮询采样（各键口径见 §3）。

### 4.1 曲线形态识别决策树

```
提取 used / device_used 序列（--curve）
   │
   ├─ 持续攀升且不回落 ────► 泄漏/缓存累积嫌疑 → 进入泄漏诊断（leak_diagnosis_guide.md）
   ├─ 阶梯上升 ────────────► 内存池按需预留（看 total 是否同步增长）
   │                          ├─ total 同步增长且池使用率低 → 池预留策略问题（碎片/预留过大）
   │                          └─ total 稳定、used 阶梯 → 峰值申请（如大中间量）瞬态
   ├─ 平台期稳定 ───────────► 正常
   ├─ 锯齿（申请-释放循环）──► 正常动态分配，关注振幅是否过大
   └─ 峰值后回落 ───────────► 瞬时峰值 → 峰值占比分析（§7）
```

### 4.2 与 npu-smi 对照

- `device_used` 曲线与同期 `npu-smi info` 整卡用量应一致（同源接口）。
- 若工具曲线系统性低于 npu-smi：属正常现象（工具可见范围 = host 侧申请），向用户说明差值来源（片上侧、其他进程）。
- 若用户想定位"进程释放后整卡未回落"：对比 `process_used`（回落）与 `device_used`（残留）两条曲线。

## 5. 内存池模型（曲线与大头分析的基础）

- **内存池层级**：`HAL` 是 host 侧经驱动接口（`aclrtMemAlloc` 等，下层调用 hal 层接口）申请的内存，其总量是各框架池总量的**父集**；`PTA`/`MindSpore`/`ATB` 均由框架向驱动申请内存池后自行维护——模型触发申请时从池中分配（释放时回收到池），池空间不足时扩容（再次调 `aclrtMemAlloc` 等），**扩容失败则框架抛 OOM**（见 [[oom_diagnosis_guide]]）。
- **分析入手顺序**：先从一个内存池类型入手（pytorch 用户优先分析 `PTA` 池），初步分析后再进行联动分析（HAL 等）；框架内存池是显存分析的重点对象，可优先过滤框架池事件分析。
- **扩容判定与池效率**：同一池的连续两条池事件中，后一条 `total` 变大 = 发生扩容；两条池事件之间一般伴随一条 HAL 申请事件（即扩容申请）。扩容前 `used/total` 为该池的**有效使用率**，使用率偏低提示池预留过大或存在内存碎片。

## 6. 显存大头（owner 聚合）

### 6.1 前提与维度约束

只有开启 `analysis=decompose` 时，MALLOC 事件 Attr 才含 `owner` 字段（显存类别与组件名称）。未开启时只能降级分析：

| 数据可用度 | 可做的分析 |
| --- | --- |
| 有 owner | **指定维度（内存池）**后按 owner 聚合（模块/组件/自定义标签维度） |
| 无 owner | 按 Event Type（HAL/PTA/MindSpore/ATB/HOST_PINNED）与 pool 维度分析 |
| 无 owner 且无 call stack | 只能看总量曲线与事件分布，明确告知用户信息缺失 |

**维度与层级关系（重要）**：
- **HAL 事件 = 驱动层全集**：HAL 事件由 hook 驱动接口（`halMemAlloc` 等）产生、无过滤，**包含 PTA/ATB/MindSpore 等框架池向驱动申请的大段**（这些段在 HAL 事件中 owner=`CANN@APP`）；HAL 事件活跃总量 ≈ 进程驱动显存（与 `process_used` 同源量级）。
- **池事件 = 池内子视图**：PTA/ATB/MindSpore 池事件是同一物理内存的库层视图（同地址同时出现在 HAL 与池事件中，dump 实测同尺寸双记）。**两类数值嵌套、不可相加**（`device_used ≥ process_used ≥ HAL used ≥ 池 total ≥ 池 used`），不存在"池并列汇总"。
- **拆解是级联单维度操作**：默认从驱动层全集（HAL）拆出组件（HCCL/APP/GE/RUNTIME…，owner=`CANN@<组件>`）→ `CANN@APP` 即框架池段 → 再下钻池内用途（weight/optimizer_state/fsdp2…）。注意 **HCCL 没有独立池**：通信内存分配在 HAL 维度的 `CANN@HCCL` owner 下。
- **HOST（锁页/主机侧）当前暂不纳入显存拆解分析**（其走 `aclrtHostRegisterV2` 通道，非 NPU 显存池事件）。

### 6.2 owner 分类解读

owner 为多级分类，`@` 分隔（框架@组件@流程@细化@…，空段跳过、**同一维度内深度天然不一致**）：

- **首段（FRAMEWORK 级）= 分配器来源名，不是池名也不是分析归口**：PTA 池 → `PTA`；HAL 池 → `CANN@<组件>`（非 `HAL@`！如 `CANN@HCCL`/`CANN@APP`/`CANN@GE`/`CANN@RUNTIME`/`CANN@UNKNOWN`）；ATB → `ATB`；MindSpore → `MINDSPORE`；少量直标块无首段（如 `weight@ops`）
- **维度内一级归口 = 丢弃框架段后的首段**：PTA 池 → `weight`（权重）、`gradient`（梯度）、`optimizer_state`（优化器状态）、`fsdp2`、`aten`（算子中间量；需 PyTorch ≥ 2.3.1 且 level=op）；HAL 池 → `HCCL`（通信内存）、`APP`、`GE`、`RUNTIME`（驱动侧组件），聚合脚本用该归口（`--at-timestamp --pool <池>` / `--group-by owner --pool <池>` / `--leak-candidates --pool <池>`）
- **推理（vLLM-Ascend 11.0）**：`vllm@weights@lora|drafter`、`vllm@kv_cache@attn|mamba|hidden_state`、`vllm@profile|serve|warmup`
- **FSDP**：`fsdp2@all_gather_output@ops`（组件@流程/细化@ops）、分片权重、激活值、梯度等
- **自定义标签**：用户 `describe` 接口添加（默认落在 USER_DEFINED 槽，如 `PTA@my_label`）

### 6.3 聚合方法

- 先定层级再聚合：驱动层全集默认拆 `--pool HAL`；池内用途 `--pool PTA`（池事件与 HAL 数值嵌套不可相加）。
- 峰值口径：同一 owner 在活跃状态下的最大占用（`--metric peak`）。
- 聚合时注意：MALLOC 事件为申请后值，FREE 事件为释放后值，统计"占用"应以**申请事件**（MALLOC）为准累加，或直接使用 used 序列在各 owner 间的分解（若 owner 未覆盖全部，差值归入未知）。
- 占比 = 该 owner 峰值占用 / 总体峰值（或指定时刻总占用），报告标注口径（池内占比应标明分母为该池总量）。

## 7. 峰值点定位与占比

### 7.1 峰值定位

| 途径 | 方法 |
| --- | --- |
| 事件曲线 | `used`（或 `device_used`）序列最大值对应时间戳——脚本 `--peak [--key used\|device_used\|process_used]` 自动定位（输出时间戳可直接衔接 `--at-timestamp`） |
| SNAPSHOT | `peak_allocated`/`peak_reserved` 字段给出 torch 视角的峰值；`device_utilization` 给出整卡水位 |

### 7.2 峰值时刻占比

```bash
# 默认拆驱动层全集（HAL 事件，含框架池段 CANN@APP；一级归口 + 层级树，占比默认输出）
python3 scripts/aggregate_dump.py <dump.csv> --at-timestamp <ns>
python3 scripts/aggregate_dump.py <dump.csv> --at-timestamp <ns> --pool PTA   # HAL 之下第二层：池内用途
```

- 峰值时刻 = 曲线最大值时间戳附近（取事件时刻）。
- 拆解是**级联单维度操作**（见 §6）：HAL 为驱动层全集（≈process_used，含框架池段），池事件是嵌套子视图，**两类数值不可相加**，无"池并列汇总"；HOST 暂不分析。
- 若 dump 无 decompose，降级为按 Event Type 占比（HAL vs 池，仅作层次参考）。

### 7.3 峰值成因

- 结合峰值时间戳前后的 OP_LAUNCH / KERNEL_LAUNCH 事件与 Call Stack，识别峰值由哪类算子/流程引起（如大 batch 前向、梯度同步、KV cache 增长）。
- 结合 python_trace（若采集）定位峰值对应代码段。

## 8. 常见数据口径陷阱

| 陷阱 | 说明 |
| --- | --- |
| 第一 Step 波动 | 第一个 Step 内存尚未稳定，波动不可作为泄漏证据（在线泄漏分析从第二 Step 起） |
| 存量显存 | start() 前已分配未释放的显存块仅记录（总量统计用），**不参与**泄漏/拆解等分析 |
| 事件驱动曲线 | 曲线仅事件时刻有点，不做中间态推断 |
| device_used 缺失 | 环境不支持时为 -1/省略，分析中不应引用 |
| alloc/free 不成对 | 采集时 events 未同时含 alloc/free 会导致配对信息缺失，泄漏分析不准确——解读前先核对 config |
| 幽灵释放一概当未释放 | attr 含 `shadow:true` 有两类：非采集期正常释放（工具 stop 状态期间的真实释放行为）与进程退出时统一释放（未释放内存，工具补齐）；按采集状态（config.json 的 start/stop 记录）与时间戳判别（见 [[leak_diagnosis_guide]] §1/§4），只有进程退出时统一释放才是泄漏分析关注对象。**反向误区同样存在：MALLOC/FREE 必然配对（幽灵机制补发 shadow FREE），"有配对"不能证明无泄漏** |

## 9. 碎片分析口径

### 9.1 碎片率定义与阈值

- **池碎片率** =（池 `total` − 池 `used`）/ 池 `total` × 100%——用**同一条池事件**的 total/used（保证同一时刻）。
- **torch 视角碎片率** = 1 − `pt_utilization` =（`reserved` − `allocated`）/ `reserved`（SNAPSHOT 字段）。
- **阈值分级**（借鉴 Ascend NPU Snapshot Analyzer 口径，与其 `(total_size − allocated_size)/total_size` 定义一致）：<5% 正常；5%~15% 偏高（关注）；>15% 严重（碎片是主要矛盾）。
- **一键画像脚本**：`--fragmentation [--pool <池>]` 直接输出池扩容清单（时间戳/扩容前后 total/增量/**扩容前 used/total 有效使用率**/伴随 HAL 段）、碎片率统计（当前/峰值/均值/最差时刻）与有效使用率最低 TOP5。

### 9.2 观测边界（分析前务必明确）

- **msmemscope 无段级/块级存量视图**：不能做逐段碎片率、块状态分布（`active_allocated`/`active_pending_free`/`inactive`）、假性碎片判定（pending 占比 >50%）。这些需引用 **ascend-npu-snapshot-analyzer** skill（补采 `torch_npu.npu.memory._record_memory_history` + `_dump_snapshot()` 后分析）。
- **⚠️ 池 `used` 在框架 free_requested 时点扣减**：跨流延迟释放（pending）的块空间被计入"碎片"部分但无法识别——多流场景（HCCL、多线程推理）碎片率可能虚高，存在三种候选解释（真碎片 / 流池残留 / 跨流延迟假性碎片），msmemscope 无法区分，需 snapshot 块状态确认。
- **池 total 只增不减为框架正常行为**（释放≠归还，见 `pta_memory_management.md` §2.2）："total 高、used 低"本身不是异常，需结合使用率（扩容前 `used/total`）与增长趋势判断；total 回落（HAL 归还/empty_cache/GC）才是回收行为。