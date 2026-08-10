# cann-samples 2_Performance 调优故事（13 个官方递进教程）

> 来源：<https://gitcode.com/cann/cann-samples/tree/master/Samples/2_Performance>（2026-08 验证可达）
> 类型：非 PR（官方递进教程，含可运行代码）

## 基本信息

| 属性 | 值 |
|------|-----|
| 算子类别 | vector-elementwise / matmul / attention / moe / norm / scatter / histogram |
| DSL/框架 | Ascend C |
| 平台 | Ascend 950 (dav-3510)，CANN 9.1.0 |
| 总案例数 | 13 |

## 案例索引

| # | 案例 | 类型 | 关键技法 | 路径 |
|---|------|------|---------|------|
| 1 | gelu_eltwise_regbase | vector | MemBase→VF融合→循环拆分→展开→常量外提(5 Case) | `gelu_eltwise_regbase_story/` |
| 2 | flash_attn_lite | attention | CV双槽→AIC双缓冲→task IO双缓冲(v0~v5) | `flash_attn_lite_story/` |
| 3 | scalar_story | scalar-bound | icache预取+静态LocalTensor+消除指针解引用 | `scalar_story/` |
| 4 | simd_vf_story | vector | Broadcast/Elemwise/Reduce SIMD VF 范式 | `simd_vf_story/` |
| 5 | grouped_matmul | matmul | 分组tiling+数据搬运+MXFP4/MXFP8 | `grouped_matmul_story/` |
| 6 | matmul_story | matmul | baseline→SWAT→尾轮均衡→UnitFlag | `matmul_story/` |
| 7 | rms_norm_quant | norm | 多核并行+预加载+带宽+流水+硬件适配 | `rms_norm_quant_story/` |
| 8 | full_quant_fused_infer_attention | attention | per-block 全量化 FIA | `full_quant_fused_infer_attention_score_story/` |
| 9 | moe_init_routing | moe | 多核+带宽+流水+SIMT+硬件适配 | `moe_init_routing_story/` |
| 10 | moe_dispatch_and_combine | moe | MoE dispatch/combine 通信优化 | `moe_dispatch_and_combine_story/` |
| 11 | kv_rms_norm_rope_cache | norm+attn | RMSNorm+RoPE+cache融合, MemBase→RegBase | `kv_rms_norm_rope_cache_story/` |
| 12 | simt_scatter | scatter | SIMT直接GM不规则写+分组+单写者冲突解决 | `simt_scatter_story/` |
| 13 | simt_histogram | histogram | MTE+Vec→SIMT grid-stride→float4→launch_bounds | `simt_histogram_story/` |

## 按 bound 类型归类

| Bound | 对应案例 |
|-------|---------|
| VEC BOUND | gelu_eltwise_regbase, simd_vf_story, rms_norm_quant |
| CUBE BOUND | grouped_matmul, matmul_story |
| Pipeline BOUND | flash_attn_lite, moe_init_routing |
| SCALAR BOUND | scalar_story |
| MEM BOUND | kv_rms_norm_rope_cache, moe_dispatch_and_combine |
| SIMT (A5专有) | simt_scatter, simt_histogram |

## 性能数据（实测）

| 案例 | 版本 | 优化点 | 提升 |
|------|------|--------|------|
| flash_attn_lite | v0→v1 | L0C→UB 优化 | **1.96x** |
| gelu_eltwise_regbase | Case0→Case4 | Membase→RegBase 全流程 | 渐进提升 |
- 优化代码查看：<https://gitcode.com/cann/cann-samples> 仓内 `Samples/2_Performance/` 目录（样例代码直接可看；佐证提交 MR !362 <https://gitcode.com/cann/cann-samples/pull/362>， 已验证可达，验证日期 2026-08）
- 参考：Ascend C 官方"流水优化"技巧华为云博客原文 <https://bbs.huaweicloud.com/blogs/433234>（ 已验证可达，验证日期 2026-08）

## 问题与瓶颈
`rms_norm_quant_story` 以渐进式版本演示优化路径——`3_vf`（simd_vf 向量化）、`4_double_buffer`（双缓冲）、`5_ub_utilization`（UB 利用率提升）、`6_binary_sum`（归约优化）；另有 `kv_rms_norm_rope_cache_story` 融合算子调优样例。原文未附量化 profiling 数据。

## 优化方法（理论手段）
1. **向量化**（simd_vf）；
2. **双缓冲**：`pipe.InitBuffer(inQueueX, 2, 256)` 开 double buffer 隐藏 MTE 搬运时间（与 Ascend C 官方"流水优化"技巧一致）；
3. **UB 容量复用 / UB 利用率提升**；
4. **归约优化**（binary_sum）。

## 性能对比
原文未附量化数字。

## 适用范围与警示
- 佐证提交 MR !362 修复上述样例在 dav-3510 的编译错误，其中提到"实测若删除某标量重赋值，NPU 上精度从 100% 降至 ~15%"——说明样例在真实 NPU 上持续验证；也提示删改样例代码需重验精度。
