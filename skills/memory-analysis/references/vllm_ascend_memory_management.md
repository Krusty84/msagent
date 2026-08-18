# vLLM-Ascend 显存管理行为模式

> 用途：**当用户的分析对象是 vLLM-Ascend 推理框架时**读取本文件，梳理其显存管理和使用逻辑，帮助定位异常点与优化点。聚焦**行为模式**（何时分配、多大、如何计算、怎么调优），不关注实现细节。

## 1. 显存全景：一切 NPU 显存都是 PTA 池的"普通张量"

**vLLM-Ascend 没有自己的显存池**（早期版本的 VramPool/orig_mem 一次性预留 + 切片机制已移除）。权重、激活、KV cache、输入缓冲全部是普通 `torch` tensor，经 **PTA 缓存分配器**（`PYTORCH_NPU_ALLOC_CONF`）向驱动申请：

```
vLLM-Ascend tensor ──> torch_npu ──> PTA 缓存池（segment/block）──> HAL（halMemAlloc / halMemCreate / halMemMalloc）
```

| 性质 | 对 msmemscope 分析的意义 |
| --- | --- |
| 无独立池 | 事件流中**看不到 vLLM 专属池类型**，全部表现为 PTA 池事件 + HAL 段申请；削减 HAL/PTA 曲线中的大头即可还原 vLLM 各组件占用量 |
| **默认 `expandable_segments:True`** | 启动时自动追加 `,expandable_segments:True` 到 `PYTORCH_NPU_ALLOC_CONF`（若用户未自设；已有 `max_split_size_mb` / `garbage_collection_threshold` 时**静默不追加**、不报错）。→ HAL 事件 `alloc_type=create`（`halMemCreate` 物理页映射）为常态 |
| 例外：sleep 模式 | RL/sleep 场景（`enable_sleep_mode`，配合 CaMemAllocator）**不**设置 expandable_segments，且权重加载走独立内存池（tag="weights"）以支持睡眠唤醒 |
| decompose 支持 | owner 分解（`analysis=decompose`）适用于 vLLM-Ascend：`load_weight`、`profile_run`、`kv_cache`、`activate` 等分类可直接归因 |

**启动前内存节奏**（NPUWorker 初始化）：
`gc.collect() + torch.npu.empty_cache()` → `init_snapshot`（记录设备 free/total）→ 权重加载 → profile（测 KV cache 可用量）→ KV cache 分配 → 图捕获 → 平台期。

## 2. KV cache 容量：profile 计算流程

### 2.1 容量公式

```
requested_memory      = init_snapshot.total_memory × gpu_memory_utilization   # 注意基于 total 而非 free
non_kv_cache_memory   = weights_memory + torch_peak_increase + non_torch_increase
available_kv_cache    = requested_memory − non_kv_cache_memory
```

- `torch_peak_increase`：profile 的 dummy forward 期间 torch 分配峰值（**图捕获前**记录——graph pool 内存不计入激活峰值）
- `non_torch_increase`：非 torch 通道的增量（驱动/AICPU/HCCL 等，vllm-ascend 用 `torch.npu.memory_stats` 之外的 free 差值法测量）
- 启动时若 `free < requested` 直接报错，提示降低 `gpu_memory_utilization` 或清理其他进程占用
- **Fast path**：显式指定 `--kv-cache-memory` 时跳过 profiling 计算（仍执行 profile_run 完成编译），且不理会 `gpu_memory_utilization`

### 2.2 通信 buffer 在 profile 阶段被预留（MoE + MC2 场景）

MoE 模型使用 MC2/fused-MC2 通信时，standard profile 前先以 `mc2_tokens_capacity` 个 token 跑一次**skip-attn 的 dummy run**，为 MC2 算子预留 HCCL buffer（通信内存不随 KV cache 计算预留会低估占用）：

```
mc2_tokens_capacity = ceil(max_num_tokens / tp_size) × tp_size
  max_num_tokens = max_num_batched_tokens（enable_prefill_mc2 时）或 max_cudagraph_capture_size 或 max_num_reqs × decode_query_len
  每 TP rank 上限受 mega_moe（131072/rank）或 MC2 每 rank token 上限约束
```

### 2.3 建议值输出（KV cache 内存换算）

完整 profiling 后引擎输出建议的 `--kv-cache-memory`：`weights + peak_activation + npugraph_memory + 150MiB 冗余`（冗余覆盖 ACL context、HCCL buffers、driver 层等难以精确测量的非 torch 占用）。

## 3. KV cache 结构与块大小

### 3.1 block_size

- 默认 **128**；`deepseek_v4` 强制 32（可 32/64/128）；prefix caching / chunked prefill 开启时强制 128；xlite graph 要求 ≤128。
- 块大小影响：块数 = KV 容量 / 块大小，块越小调度越细（碎片多、前缀复用率高）。

### 3.2 Ascend 特有 cache spec（vllm v1 KVCacheSpec 体系）

- **AscendMLAAttentionSpec**：MLA/SFA 压缩缓存——`page_size = block_size × num_kv_heads × (head_size × dtype_size + scale_dim × scale_dtype_size)`；支持压缩比 `compress_ratio`（`max_memory_usage = cdiv(max_model_len, block_size × compress_ratio) × page_size`，DCP 时按 `cdiv(max_model_len, dcp_world_size)` 分摊）、稀疏 SFA C8 打包（整块打包为 byte tensor）。
- **AscendSFAIndexerCacheSpec**：SFA 的索引 K/scale 缓存（与 MLA 缓存**共享 block id 但物理上独立分配**，可 DCP 复制 `sfa_dcp_replicated_indexer_size` 份）。
- **AscendSlidingWindowMLASpec**：滑窗 MLA（`storage_block_size`、`compress_ratio`、DeepseekV4 `alignment`）。
- Mamba 层按 list 注册、attention 层注册 (k, v) 独立 tensor——同一 KV cache 配置下按层分别分配。

**分析意义**：MLA/SFA 模型的 KV cache 实际显存 = 主缓存 + 索引缓存 + scale 数据，逐项核对时别按"传统 K/V 两倍"估算；KV cache 全部以 torch tensor 分配 → PTA 池事件可见、owner=`kv_cache`。

## 4. 通信内存（联动 [[hccl_memory_detail]].md）

- HCCL 通信域建立 → CCL Buffer **约 401MB/通信域**（in 200MB + out 200MB + winExp 1MB），图模型多卡每进程一份；通信 buffer 预留在 profile 阶段即发生，因此**计入 non_kv_cache_memory 的缺口**。
- **MC2 workspace 16MB** / fused-MC2（mega_moe）的 workspace 与 `mega_moe_max_tokens`（默认 131072）线性相关——长上下文场景此项不可忽视，调小可降显存但可能丢 token 精度。
- 通信内存在 HCCL 侧的完整行为明细（scratch 计算、AIV/MC2、共享机制）见 [[hccl_memory_detail]]。

## 5. 图捕获内存（npugraph）

- 非 eager 模式：warmup sizes（dummy runs）→ `capture_model()`（npugraph_ex 捕获，返回 `npugraph_memory_bytes`）→ **图池内存常驻不可释放**——曲线上的"图池平台"。
- `enable_static_kernel`（编译固定 shape 算子，批量大小变化小时性能更优，但编译时间变长、按 batch_size 缓存算子）。
- Fast path 建议值中的 `npugraph_memory_bytes` 即此项；图捕获 memory pool 从 torch 侧预分配，PTA 池事件中可见一次较大预留。

## 6. 不占 HBM 的内存（分析时可排除）

- **swap / KV offload**：v1 `kv_offload`（CPU/NPU 双通道）与 `simple_kv_offload`（SimpleCPUOffloadWorker + DMA copy backend）把 KV 块搬运到 **host 内存**（含 pinned），NPU 上只留缓冲窗口——不占 HBM、但增加 host RAM 与带宽占用。
- **kv_transfer**（跨实例 KV 共享）不走本卡 HBM。

## 7. 权重加载与格式

- 权重加载为**一次性大段**（PTA 池，owner=`load_weight`），`model_memory_usage` 计入 non_kv。
- `VLLM_ASCEND_ENABLE_NZ`（默认 1：仅量化场景转 FRACTAL_NZ；2：尽量转）——NZ 转换可能产生临时大块转换缓冲与时间开销。
- `VLLM_ASCEND_ENABLE_MLAPO`（默认 1）：DeepSeek W8A8 性能优化，**会消耗更多 NPU 显存**——显存紧张时第一优先级排查项。

## 8. 输入缓冲与激活（峰值来源）

- `AscendInputBuffers` 等输入缓冲按 `max_num_tokens`/`max_num_reqs` 预分配（token_ids、positions、seq_lens 等，量级小）；真正的激活峰值来自 forward 的中间张量（hidden_states 等），由 `torch_peak_increase` 在 profile 中测出。
- `max_num_batched_tokens` / `max_cudagraph_capture_size`（图捕获尺寸）直接决定激活峰值与 MC2 token 容量——**峰值过高时第一个检查项**。

## 9. 分析与调优速查（msmemscope 视角）

### 9.1 预期曲线形态（推理服务）

```
权重加载 ──> profile 峰值 ──> KV cache 一次性大分配 ──> 图捕获平台 ──> 运行平台期
   ▲ load_weight        ▲ activate/aten        ▲ kv_cache owner      ▲ 图池        ▲ kv_cache 块逐请求增长
```

- 平台期 `used` 的**缓慢增长 = KV cache 块随序列长度正常占用**（申请-释放循环、回池复用），不是泄漏；`total` 平台不降是池缓存正常行为（见 [[pta_memory_management]] §2.2）。
- 运行期出现"陡降→冲顶"锯齿 = 显存不足触发内部 empty_cache（设备同步），是**显存紧张的性能特征信号**（见 [[pta_memory_management]] §4.3）。

### 9.2 异常点定位

| 现象 | 检查顺序 |
| --- | --- |
| 启动报 free < requested | 其他进程占卡 / `gpu_memory_utilization` 过高 / 多卡 NCCL 保留空间 |
| KV 容量远小于预期 | 1) profile 峰值（大 `max_num_batched_tokens`/capture size）；2) 通信 buffer（MoE+MC2，检查是否预留在 profile 内）；3) 权重过大（NZ/MLAPO）；4) 图池 |
| OOM（推理中途） | 按 [oom_diagnosis_guide]：TOP_ALLOC 为 `kv_cache` → 序列长度/并发超配；`activate` → batch/token 配置；池扩容失败 → 卡上碎片 |
| 平台期持续攀升 | 先排除 KV cache 正常增长（9.1）；再按泄漏诊断流程 |

### 9.3 优化杠杆

| 杠杆 | 方向 | 关联 |
| --- | --- | --- |
| `gpu_memory_utilization` | 降 → KV 容量减少、激活余量增大 | §2.1 |
| `--kv-cache-memory` | 手动指定精确容量（跳过 profile 误差） | §2.1 fast path |
| `block_size` | 小 → 碎片多、前缀命中提升（prefix caching） | §3.1 |
| `PYTORCH_NPU_ALLOC_CONF` | expandable_segments 默认开；碎片严重时配合 `page_size:1g` 等（互斥项注意）。**验证是否生效：HAL 事件 `alloc_type=create`（halMemCreate）出现即生效；若用户自设了 `max_split_size_mb`/`garbage_collection_threshold` 等互斥项，vLLM 不再设置 expandable** | §1、[[pta_memory_management]] §4 |
| `HCCL_BUFFSIZE` | 调小省通信固定开销（可能降大报文性能） | [[hccl_memory_detail]] §2 |
| `VLLM_ASCEND_ENABLE_MLAPO=0` | DeepSeek W8A8 场景显存优先时关闭 | §7 |
| `mega_moe_max_tokens` | 调小减 workspace（长上下文 MC2 场景） | §4 |
| `max_num_batched_tokens` / capture size | 调小降激活峰值（吞吐换显存） | §8 |
| KV offload / simple_kv_offload | 把 KV 搬到 host 内存换 HBM | §6 |

> 关联：[[pta_memory_management]]（PTA 池是 vLLM-Ascend 所有显存的实际分配载体）、[[hccl_memory_detail]]（通信 buffer 明细）、[[msmemscope_data]]（HAL `alloc_type=create` 与 expandable 判定）、[[oom_diagnosis_guide]]、[[leak_diagnosis_guide]]。