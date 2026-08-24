# Ascend C · RegBase / SIMD VF 优化（`__simd_vf__` + `AscendC::Reg::*`）

> 适用：Ascend 950（A5, dav-3510）的寄存器级向量计算范式。MemBase 层机制（分核、合并搬运、删冗余同步）仍然有效且应先做；本文档覆盖 VF 语义层的迁移判断与优化技术。
> 来源：[昇腾 950 Simd-VF 编程系列](https://cann.csdn.net/6a5f15b6662f9a54cb92a013.html)（[高性能技术篇](https://hwcomputing.csdn.net/6a6002df662f9a54cb92fc5b.html)，CANN 开发者社区）+ cann-samples VF 样例实测。

## 1. 迁移判断：什么时候值得 MemBase→RegBase

| 维度 | MemBase（`LocalTensor`） | RegBase（`RegTensor`） |
|---|---|---|
| 数据载体 | UB 内存 | VF 寄存器（VL=256B/寄存器） |
| 处理粒度 | 任意长（硬件内部 tiling） | 一次一个 VL，软件显式分块循环 |
| 搬运 | DataCopy（MTE2/MTE3） | LoadAlign/StoreAlign（UB↔Reg） |
| 调用层级 | `__aicore__` 直接调 | 必须 `__simd_vf__` + `asc_vf_call` |
| Mask | count 参数自动 | 显式 `MaskReg`（32B=256bit） |

**值得迁移**（按收益排序）：

1. **多步融合计算链**（Sigmoid/LayerNorm/Softmax/GELU）：中间结果留在寄存器，UB 流量从 2×步数×VL 降到 2×VL（4 步算子约 -75% UB 往返）——这是 RegBase 的主收益来源
2. 计算指令周期长的算子（Exp/Ln/Sqrt/Div），UB 往返占比高
3. 非连续访问（Gather/Scatter/Squeeze/Block-Strided）MemBase 难高效表达
4. MemBase 优化已尽（vec_ratio 高、无冗余搬运可删）后的最后一档

**不值得迁移**：简单单步算子（纯 Add/Sub，MemBase 已足够）；瓶颈在 MTE2 带宽（mte2_ratio>90%）的算子——RegBase 不解决带宽问题。实测 Add 级单步算子 MemBase→RegBase 收益不明显（计算链太短）。

## 2. 调用层级（违反即编译报错）

```
__global__ __aicore__ kernel → __aicore__ 函数
  → asc_vf_call<VFFunc>(args...)   // 唯一进入 VF 上下文的方式
    → __simd_vf__ 函数              // Reg API 的唯一执行环境
      → __simd_callee__（所有 AscendC::Reg::*）
```

```cpp
template <typename T>
__simd_vf__ inline void AddVF(__ubuf__ T* dst, __ubuf__ T* src0, __ubuf__ T* src1,
                              uint16_t repeatTimes, uint32_t oneRepeatSize) {
    AscendC::Reg::RegTensor<T> r0, r1, r2;
    AscendC::Reg::MaskReg mask = AscendC::Reg::CreateMask<T>();
    for (uint16_t i = 0; i < repeatTimes; ++i) {
        AscendC::Reg::LoadAlign(r0, src0 + i * oneRepeatSize);
        AscendC::Reg::LoadAlign(r1, src1 + i * oneRepeatSize);
        AscendC::Reg::Add(r2, r0, r1, mask);
        AscendC::Reg::StoreAlign(dst + i * oneRepeatSize, r2, mask);
    }
}
// __aicore__ 侧：asc_vf_call<AddVF<T>>(dst, s0, s1, repeats, oneRepeatSize);
```

## 3. 性能技术清单（单变量迭代候选）

| # | 技术 | 收益原理 |
|---|---|---|
| 1 | 寄存器复用减少 UB 往返 | 多步算子中间结果留 RegTensor，只在循环头尾访问 UB（Sigmoid 例 UB 流量 -75%） |
| 2 | VALU 三发射友好编排 | 相邻指令用不同 dstReg 制造独立性；Div/Sqrt/Exp（10+ 拍）后跟独立短指令填充流水；多 VL 块双路软件流水 |
| 3 | 标量二元 `*s` 后缀 + Duplicate | `Muls(dst,src,scalar,mask)` 省一次 LoadAlign+UB 存放+发射槽；常量直接 `Duplicate` 不落地 UB |
| 4 | 融合指令 | `Axpy`/`MulAddDst`/`MulsCast`/`AbsSub`/`ExpSub`：1 拍顶 2 拍 + 省 tmp UB |
| 5 | Post-Update / AddrReg 地址外提 | `LoadAlign<T, POST_MODE_UPDATE>(reg, ptr, stride)` 硬件自增地址，省循环内地址计算指令 |
| 6 | Hardware Loop 友好 | 最内层循环 trip count 编译期可知、循环体无分支、循环变量 uint16_t |
| 7 | L2Cache Hint | 流式数据 `SetL2CacheHint(CACHE_MODE_DISABLE)` 避免污染 L2 |
| 8 | 非对齐尾段 | `UpdateMask(remainder)`+`MaskMergeMode::MERGING`（防越界写 0 污染），或 `StoreUnAlign`+`StoreUnAlignPost` |
| 9 | 64 位整型 | `RegTensor<T, RegTraitNumTwo>` 拼 2×VL 逻辑寄存器（int64 必须） |
| 10 | 同地址 RAW | VF 内连续写后读同一 UB 地址：`LocalMemBar<VEC_STORE, VEC_LOAD>` |
| 11 | constexpr 最大化 | MaskPattern/LoadDist/StoreDist/MergeMode/RoundMode、oneRepeatSize、trip count 全部编译期决定 |

## 4. 瓶颈诊断表（VF 语境）

| 现象 | 含义 | 方向 |
|---|---|---|
| aiv_mte2_ratio > 90% | 带宽 bound | 增大 dataCopyLen、多核切分、L2Cache Hint（RegBase 无效） |
| aiv_vec_ratio > 50% 且 mte2 低 | 计算 bound | Bank Conflict、融合指令、寄存器复用 |
| aiv_scalar_ratio > 20% | 标量开销 | 地址计算外提（post-update/AddrReg）、减少循环内条件 |
| RegBase 收益不明显 | 链太短 | 回到 §1 重新评估是否值得；考虑把更多步融进同一 VF 函数 |
| vec_time 异常高 | UB Bank Conflict | dataCopyLen 错开 256B、调整 UB 布局 |

## 5. 常见编译/运行错误

| 错误 | 原因 | 解决 |
|---|---|---|
| Reg API called outside `__simd_vf__` | `__aicore__` 里直接调 Reg API | `asc_vf_call<VFFunc>(args...)` 包裹 |
| `__simd_vf__` called directly | 直接调用 VF 函数 | 必须经 `asc_vf_call` |
| MaskReg type mismatch | Mask 与 RegTensor 的 T 不一致 | `CreateMask<T>` 显式指定 |
| REG_NUM mismatch | int64 用了 RegTraitNumOne | 改 RegTraitNumTwo |
| LoadAlign address not aligned | 首地址非 32B 对齐 | LoadUnAlign 或调整 UB 偏移 |
| StoreAlign dst overflow | dst 长度不足 VL | MaskReg 或 StoreUnAlign |

## 6. 与其他文档的关系

- UB 容量/搬运粒度、DataCopy 合并：[optimize-data-copy.md](optimize-data-copy.md)
- Double Buffer / 流水重叠（MemBase 层先行）：[optimize-pipeline.md](optimize-pipeline.md)
- 标量削减通用原则：[optimize-api-usage.md](optimize-api-usage.md)
- 案例路由：cann-samples `vector_function_add`（最小 VF 样例）、`simd_vf_story`（Broadcast/Elemwise/Reduce 三族 VF 调优，含 cannsim VF cycles 口径）
