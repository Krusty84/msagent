# PTA（TorchNPU）显存管理行为模式

> 用途：为显存调优分析服务，结合模型代码深入分析时对照使用。本文只总结**行为模式**（何时申请/何时释放/何时扩容/什么配置影响什么行为），不关注代码实现细节。

## 1. 内存管理全景：五条显存通道

PTA 进程内存在**互不经过**的几条显存申请通道，分析数据时必须先分清来自哪条：

| 通道 | 底层接口 | 是否进 PTA 缓存池 | msmemscope 可观测性 |
| --- | --- | --- | --- |
| ① 缓存分配器（主通道，张量内存） | `aclrtMallocAlign32` / expandable VMM | 是（segment→block 两级） | PTA 池事件（MSTX `ptaCaching` 域）+ HAL 事件 |
| ② 算子 workspace | `AclrtMallocAlign32(HUGE_ONLY)` 直连，**绕过缓存** | 否 | HAL 事件（独立 WORKSPACE 组件） |
| ③ 锁页 host 内存（pin_memory） | `aclrtMallocHost`，host 侧 | 否 | **不占 HBM**，HOST_PINNED 事件 |
| ④ swap 交换内存 | `aclrtMallocHost` + HostRegister（SVM） | 否 | 不占 HBM |
| ⑤ 图捕获私有池 | 同①，但隔离的 PrivatePool | 是（独立池） | PTA 池事件（pool 独立） |

- 除 ① ⑤ 外，②③④ 都不进缓存池——对应 msmemscope 事件中 PTA/MindSpore/ATB 之外类别的来源。
- 锁页（③）与 swap（④）在 host 侧，不占 HBM，不影响 `device_used`。

## 2. 缓存分配器核心行为

### 2.1 两级结构与池语义

- **Segment（段）**：一次 `aclrtMalloc` 向驱动申请的大块连续内存。PTA 池的 **total = 段总量**（reserved 视角）。
- **Block（块）**：段被拆分出的分配单元，**used = 活跃块累计**（allocated 视角）。
- 按请求尺寸分两个池：**小池**（请求 ≤1MB，段 2MB）；**大池**（请求 >1MB，段默认 20MB，可配 20~512MB；≥10MB 的请求按 2MB 取整单独成段）。

### 2.2 释放 ≠ 归还（最重要的行为）

```
张量释放 → 块标记空闲、回池复用 → 仅以下 4 种情况才归还驱动(aclrtFree)：
  ① torch.npu.empty_cache() / empty_cached_mem（显式）
  ② OOM 重试链（自动）
  ③ garbage_collection_threshold 配置的自动 GC
  ④ expandable 模式 unmap 空物理页
```

- 块释放后**默认留在池中复用，绝不主动归还驱动**——这就是 PTA 池 total 只增不减、曲线阶梯上升的本质。
- **分析意义**：曲线上的"常驻平台"不一定是泄漏——可能是池缓存。判别：`used`（块级）降了但 `total`（段级）不降 = 正常缓存；`used` 也不降且持续增长 = 真问题。与 SNAPSHOT 的 `pt_utilization = allocated/reserved` 直接对应。

### 2.3 申请路径与扩容语义

一次张量分配依次尝试：
1. **尺寸取整**：最小 512B；旧 CANN（<9.1.0 或非 Ascend950）每次 +32B padding；可配 2 的幂分区取整（`roundup_power2_divisions`）
2. **best-fit 复用**：池内找同流、最小够用的空闲块；块比请求大且可分割则切块
3. 找不到 → 触发注册的 FreeMemoryCallback → 重试
4. 找不到 → 事件回收（跨流使用完毕的块）→ 重试
5. 找不到 → **申请新段（即扩容）**：
   - 普通模式：`aclrtMallocAlign32(HUGE_FIRST)` → hal 层 `halMemAlloc` → HAL MALLOC 事件（`alloc_type=alloc`）
   - expandable 模式：从预留虚拟地址段尾部映射新物理页（`AclrtMallocPhysical` → hal 层 `halMemCreate` → HAL 事件，`alloc_type=create`；2MB/20MB 粒度）
6. 驱动拒绝 → 依次：借用 opt-in 私有池 → 释放足够大的缓存块 → **设备同步 + 清空 workspace 缓存 + 释放全部非分割缓存块** → 最后一次重试
7. 仍失败 → **抛 OutOfMemoryError**（记录 OOM trace、通知 observer）

**扩容判定**：池事件 total 变大 = 发生了第 5 步"申请新段"。**两条池事件间必然伴随 HAL 事件**——普通模式为 `halMemAlloc`（alloc），expandable 模式为 `halMemCreate`（create，物理页）。判别 expandable 模式：看 HAL 事件 `alloc_type=create`（对应 dump 字段）。

**分析意义**：扩容前的 used/total = 池有效使用率——偏低提示预留过大或碎片多。

### 2.4 分割、合并与碎片

- 大池块分割条件：请求 < `max_split_size_mb`（默认**无上限**）且剩余 >1MB 才切；≥max_split_size 的块整体分配不拆（oversize block）。
- 释放时相邻空闲块自动合并。
- **碎片的量化指标**：`inactive_split_bytes`（池中已分割、不可归还的空闲块字节数）——torch 视角最直接的碎片量，可指导 `max_split_size_mb` 调参。

### 2.5 释放触发链：Python 对象生命周期 → 块回收

- tensor 的显存释放入口是 storage 上的 deleter（`local_raw_delete`）。Python 侧 tensor 对象被销毁（refcount 归零或被 gc 回收循环引用）→ Storage 析构 → deleter → 缓存分配器 `free()`。
- **释放是"对象级"而非"作用域级"**：`del` 不保证立即释放（其他引用仍持有）；全局变量/闭包/dataloader worker/缓存 dict 持有引用 → tensor 不销毁 → 块不释放 → 池 used 不降。
- **分析意义**：used 平台期不是"没释放"，而是"没触发释放"；`gc.collect()` 可强制回收循环引用。这是泄漏分析中"Python 侧引用残留"（见 [[leak_diagnosis_guide]] §5）的机制根源。

### 2.6 跨流访问与延迟回收（recordStream）

- 块被其他流使用时调用 `recordStream` 登记到该块的流集合（`stream_uses`）。
- **释放时**（不是算子下发时）在 `stream_uses` 的每条流上记录 Event；后续 malloc 时查询事件，**全部完成才真正回池**（free_requested → free_completed，中间为跨流等待）。
- **分析意义**：跨流多的程序（HCCL 通信、多线程推理）池 used 会短暂虚高；`multi_stream_lazy_reclaim:True` 会放大虚高（事件查询惰性化，省开销但瞬时占用偏高）。

## 3. 流池模型（多流视角）

### 3.1 每流一个内存池，跨流不复用

- Block 与分配流绑定，池按流隔离：best-fit 只在同流内找块，跨流块不复用（这是正确性约束——避免不同流任务竞争同一块内存）。
- 同流块复用高效：流上任务顺序执行，块生命周期与任务顺序对齐。
- **msmemscope 视角**：PTA 池事件**无 stream 字段**，按 device 聚合——所有流池合并上报为"PTA 池"；PTA `total` = 各流池段的总和，`used` = 各流池活跃块累计。分析时无法直接区分流，需结合 HAL 段申请事件（时间点/调用栈）侧面判断。

### 3.2 流池历史残留 = "逻辑碎片"

- 每流池 total 只增不减（无 empty_cache 不归还）；某流峰值过后实际使用变少时，其池内大量空闲块**只对本流可用**，其他流无法复用——等效于碎片效应（内存分配不可达），即使物理上连续。
- **分析意义**：多流场景下 PTA 池 used/total 偏低，不一定是"预留过大"，也可能是**流池残留**。缓解：`empty_cache()`（清全部流池）、流复用设计（同一流执行同型任务）。

## 4. 配置调优杠杆

环境变量 `PYTORCH_NPU_ALLOC_CONF`（与 `PYTORCH_ALLOC_CONF` 二选一，同设报错），格式 `k:v,k:v`。

### 4.1 缓存分配器相关（分析曲线形态必须先知道）

| 配置 | 默认 | 行为影响 | 调优意义 |
| --- | --- | --- | --- |
| `expandable_segments` | False | 段可扩展：预留 2GB(small)/10GB(large) 虚拟地址，物理页按需映射；**与 max_split_size_mb、garbage_collection_threshold 互斥**（同开报错） | 碎片更少、OOM 时可回收空页；初始稍慢；IPC 共享不支持 |
| `max_split_size_mb` | 无上限 | 可分割块尺寸上限 | OOM 提示"reserved >> allocated 时调小它"；调小减碎片但放大浪费 |
| `garbage_collection_threshold` | 0（关） | >0 且设了 fraction 时，超阈值自动按"空闲年龄"释放缓存块 | 把"只增不减"变"有顶的缓存" |
| `large_segment_size_mb` | 20 | 大池段大小（20~512MB） | 段越大，小块碎片越少但浪费越多 |
| `page_size:1g` | False | 大池用 1GB 大页（HUGE1G_ONLY），覆盖 large_segment_size | 曲线形态变为 1GB 台阶 |
| `per_process_memory_fraction` | 1.0 | 进程显存上限比例 | 设 <1 等于给 PTA 池设天花板 |
| `throw_on_npumalloc_oom` | False | 超 fraction 时申请前预拒（不触驱动） | 服务场景防驱动崩溃；OOM 形态变为"预拒" |
| `roundup_power2_divisions` | 1（关） | 2 的幂分区取整粒度 | 减少碎片 |
| `multi_stream_lazy_reclaim` | False | 跨流回收惰性化 | 性能 vs 瞬时占用 |
| `base_addr_aligned_kb` | 16 | 大块基址对齐（仅 expandable 时生效） | Ascend 特性 |
| `PYTORCH_NO_NPU_MEMORY_CACHING=1` | 关 | **完全绕过缓存分配器**，每次申请直连、释放即还 | ⚠️ 此模式下**没有 PTA 池**，msmemscope 只有 HAL 事件 |

### 4.2 锁页内存相关（不占 HBM，影响 host 侧）

- `pinned_max_round_threshold_mb` / `pinned_max_cached_size_mb`：默认 SIZE_MAX——**pin 内存申请后几乎永不归还系统**；pin 分配**总是向上取整到 2 的幂**（申请 100MB 实际占 128MB）。要归还只能 `torch_npu.npu.host_empty_cache()`。
- `pinned_reserve_segment_size_mb`：一次性预留 host 段，此后永不释放。
- `pin_memory_expandable_segments`：pin 内存也用 VMM 映射（CANN≥8.5.0），开启后上面两个阈值失效。
- `pinned_mem_register`：pin + 注册（SVM），提升 H2D 性能。

### 4.3 高水位 → 频繁 empty_cache → 性能问题

- 显存紧张时，每次新申请更可能走完整重试链——链中每次释放前都有**设备同步**（`npuSynchronizeDevice`，CPU 等待 NPU 全部任务完成），随后池被清空，后续申请又要重新走慢速驱动调用。
- **分析意义**：曲线出现**反复"陡降→冲顶"锯齿 + 运行期性能劣化**时，说明程序在频繁触发内部 empty_cache——这是显存不足的性能特征信号。调优方向是降水位（fraction/GC 阈值/优化峰值），而不是依赖自动回收。

## 5. 外围模块行为

### 5.1 workspace（算子临时工作区）

- 每个 stream 一块，**申请后只增不减**（等于该流历史最大 workspace），只有 empty_cache 或更大需求时被换掉。
- 底层直连，**绕过缓存分配器**（msmemscope 侧为独立 WORKSPACE 组件）。
- **OOM 时第一个被清空的就是它**（在清缓存分配器之前）。
- **分析意义**：曲线上的小台阶 + 大算子瞬间（如大 gemm）对应 workspace；workspace 大说明算子库选用了高 workspace 的算法。

### 5.2 插拔分配器（custom allocator）

- 整体替换分配策略：**每次张量分配直连用户 alloc_fn，无缓存、无 PTA 池**。
- **分析意义**：msmemscope 此时**收不到 PTA 池事件**（只有 HAL）；这是天然的"无缓存基线"实验手段——对比有/无缓存的曲线差异 = 池缓存放大效应。

### 5.3 图捕获（NPU Graph）

- 捕获期间分配路由到**私有池**，graph 释放前内存不回主池；`torch.npu.use_mem_pool` 可手动使用。
- **分析意义**：图捕获启动瞬间 total 会跳一次（一次性预留），回放期不再分配。

## 6. SNAPSHOT 统计语义（与 msmemscope 对照）

| SNAPSHOT 字段 | 视角 | 来源 | 更新时机 |
| --- | --- | --- | --- |
| total_mem / free_mem | **驱动** | `aclrtGetMemInfo` | 采样时刻 |
| reserved / peak_reserved | **torch 池** | memory_stats | **事件驱动**：段申请/扩容时涨 peak；释放只降 current 不降 peak |
| allocated / peak_allocated | **torch 池** | memory_stats | 事件驱动：块下发时涨 peak |
| device_utilization | 驱动 | (total-free)/total | 采样时刻 |
| pt_utilization | torch 池 | allocated/reserved | 采样时刻 |

关键点：
- **peak 是事件驱动的**（段申请时刻/块下发时刻更新），不是采样驱动的；释放、empty_cache、GC 都不降 peak。
- reserved（torch 池视角）与 total_mem/free_mem（驱动视角）对比 = "池缓存放大"效应。

## 7. 分析与调优速查（msmemscope 视角）

> 与 [[msmemscope_data]] 的字段口径配合使用；曲线形态通用识别（阶梯/锯齿/平台等）见 msmemscope_data §4，本节聚焦 PTA 池特有信号。

### 7.1 预期曲线形态（PTA 池事件视角）

```
权重/模型装载 ──> 训练稳态：step 锯齿（申请-释放）──> 扩容阶梯（total 跳变）──> 平台期
   ▲ 大段预留        ▲ 振幅≈工作集变化量            ▲ 每次触发 HAL 段申请       ▲ used 回落而 total 不降
```

**判别主轴是 used（块级）vs total（段级）两条线分开看**（§6）：

| 曲线形态 | PTA 侧信号 | 含义 |
| --- | --- | --- |
| `used` 跨 step 不回基线、单调攀升 | 池内 allocated（块级）只涨不降 | 块级引用残留/泄漏 → 按 [[leak_diagnosis_guide]] §5 |
| `total` 阶梯式上台阶 | 段级预留增加（伴随 HAL 段申请事件） | 池扩容 = 峰值需求/预留策略；台阶大小对照 `large_segment_size_mb` |
| step 锯齿（振幅稳定） | allocated 上下波动 | 正常申请-释放循环；关注振幅与回基线程度 |
| `used` 回落但 `total` 平台不降 | 段不归还 | **正常池缓存行为**（释放≠归还），不是泄漏 |
| 陡降→冲顶锯齿 + 性能劣化 | used 归零后立刻冲高 | 显存紧张触发内部 empty_cache（设备同步），显存/性能双重信号（§4.3） |
| 平台期 total 缓慢阶梯 | 偶发扩容 | 峰值略超池容量时自动扩段，正常 |

**实验验证法**（区分"缓存平台"与"真泄漏"）：对 `used` 持续不降的情形，观察 `total`——`empty_cache()` 后 `total` 大幅回落而 `used` 不变 = 池缓存占用（可配 GC 阈值/fraction 解决）；`total` 也不降 = 活跃块不释放，进入泄漏诊断。判别的关键始终是 **used 是否回落**。

### 7.2 异常点定位表

| 现象 | 检查顺序 |
| --- | --- |
| `used` 持续增长（训练/推理平台期） | 1) 排除正常工作集增长（KV cache 随序列长度逐块占用，见 [[vllm_ascend_memory_management]] §9.1）；2) 框架侧缓存/优化器累积；3) 按泄漏诊断流程 |
| `total` 频繁扩容 / 扩容前使用率低 | 扩容前 used/total 有效使用率：偏低 → 1) `large_segment_size_mb` 预留过大；2) 碎片（`inactive_split_bytes` 高）；3) 多流残留（§3.2） |
| OOM | 按 [[oom_diagnosis_guide]] 归因三选：真峰值超限 / 碎片（inactive_split 高 → max_split_size、expandable_segments）/ 池缓存占位（empty_cache 能救 → GC 阈值、fraction） |
| 峰值来源不明 | total 跳变时间戳附近的 OP_LAUNCH/HAL 事件：大段申请对应模型大张量/大算子 |
| 多流场景碎片率虚高 | 碎片三解释无法区分（真碎片/流池残留/跨流延迟假性碎片）→ 用 ascend-npu-snapshot-analyzer 核对池内块归属 |
| 性能劣化伴随陡降→冲顶 | 显存紧张触发的内部 empty_cache（设备同步）→ 降水位优先于依赖自动回收（§4.3） |
| 怀疑 `expandable_segments` 未生效 | 看 HAL 事件 `alloc_type`：为 `create`（halMemCreate）说明段按需扩展生效；全为普通 `malloc` 则未生效——检查互斥配置（max_split_size_mb/gc_threshold 冲突、IPC 共享场景不支持） |

### 7.3 优化杠杆速查

| 症状 / 目标 | 首选杠杆 | 方向 | 关联 |
| --- | --- | --- | --- |
| 碎片严重（inactive_split 高、多流） | `PYTORCH_NPU_ALLOC_CONF=expandable_segments:True` | 段可按需扩展，碎片最少；与 `max_split_size_mb`/`garbage_collection_threshold` 互斥 | §4.1 |
| 碎片 + 大申请效率 | `page_size:1g`（与 expandable 搭配） | 大页减少片段，但曲线呈 1GB 台阶、总占用略增 | §4.1 |
| reserved ≫ 使用量 / OOM 前余量不足 | `max_split_size_mb` 调小 | 允许回收大段、增空余块；调得过小增碎片与开销 | §4.1 |
| 池缓存不归还导致 total 虚高 | `garbage_collection_threshold` | 设置池水线，释放的段按水线保留 | §4.1 |
| 池占用过大 | `PYTORCH_NPU_ALLOC_CONF` 的 `fraction`（如 0.6） | 限制池申请上限；配合 `throw_on_npumalloc_oom` 可预拒避免驱动 OOM | §4.1 |
| 预留过大（有效使用率低） | `large_segment_size_mb` 调大 | 合并小段减少预留膨胀 | §4.1 |
| 峰值过高 | expandable_segments + 框架侧 gradient checkpointing | 降峰值申请；三板斧组合 | §4.1、§7.2 |
| 评估"池缓存放大"是否值得优化 | `NO_MEMORY_CACHING`/插拔分配器基线对比 | 关缓存后看 HAL/process_used 差 | §4.1 |
| 显存紧张引发的性能锯齿 | 降水位（fraction/GC 阈值）+ 降峰值，而非依赖自动 empty_cache | 避免设备同步 | §4.3 |

> 配置项完整行为说明见 §4；vLLM-Ascend 场景下 PTA 池的实际占用者（权重/KV cache/激活/图池）归因见 [[vllm_ascend_memory_management]]。
