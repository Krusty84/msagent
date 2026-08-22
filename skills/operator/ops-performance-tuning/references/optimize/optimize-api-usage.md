# §1.3 Ascend C · API 使用

> 本文件覆盖 Ascend C 的 API 使用优化技术。

| 技术 | 理论说明 | 代码/参数模式 | 性能对比 |
|---|---|---|---|
| TPipe 外置 | TPipe 在 kernel 类外创建，避免重复构造开销 | 文字模式：TPipe 声明移到 kernel 类外 | **scalar_time 281→236us，−17%** |
| TQueBind 纯搬运算子 | 纯搬运算子用 `TQueBind<VECIN,VECOUT>` 绑定输入输出队列 | `TQueBind<VECIN,VECOUT>` | aiv_vec_time 降至约 0 |
| Counter 模式 mask | `SetMaskCount` 替代手工 mask 计算 | `SetMaskCount` | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |
| Matmul enAtomic 融合累加 | Matmul `enAtomic=1` 融合累加，省中间往返 | `enAtomic=1`（M=64,N=256,K=256 用例） | **cycle 154181→135054，−12.4%** |
| **RegBase 改写（GELU+ElementWise）** | MemBase→VF融合→循环拆分→展开→常量外提 5 步递进 | 参见 cann-samples `gelu_eltwise_regbase_story/` Case0~4 | 渐进提升 |
| **RegBase 融合（RMSNorm+RoPE+Cache）** | 多算子 RegBase 融合减少 GM 往返 | 参见 cann-samples `kv_rms_norm_rope_cache_story/` MemBase→RegBase | — |
| Scalar icache 预取 | `__builtin_prefetch` 预取指令减少 icache miss | 参见 cann-samples `scalar_story/` | — |
| 静态 LocalTensor 替代成员变量 | 消除运行时指针解引用开销 | `LocalTensor<T> static_tensor = ...` | — |
| 两级归约组合 | `BlockReduceSum` + `WholeReduceSum` 归约组合 | `BlockReduceSum` + `WholeReduceSum` | 未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证 |
