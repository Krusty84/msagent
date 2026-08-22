# §4 Triton-Ascend

> 本文件覆盖 Triton-Ascend 的优化技术（Vector 类深度优化、30 优化点、SIMT→SIMD 原子操作、constexpr）。

## 4.1 Vector 类算子深度优化

| 技术 | 理论说明 | 代码/参数模式 |
|---|---|---|
| **多 Token 批量处理（首要优化点）** | NPU 访存弱计算强；单 Kernel UB 总容量 192KB，留余量仅用其 50%（85KB）以保证 Double Buffering | `N ≤ 85×1024 / S_token`（S_token=单 Token UB 峰值占用，如 BF16 一次 load+store 时 `hidden_size×2`）；用整数除法 `//` 而非 `tl.cdiv` 防 UB 溢出 |
| 减少 kernel 内 Scalar 运算 | 与 pid/循环变量无关的计算外提 | 文字模式：循环不变量外提 |
| 非连续访问逐行读 | `index_select` 类只能逐行读（否则二维 mask 引入大量 Scalar） | 文字模式 |
| 加载与计算交织 | "加载一次、计算一次"隐藏访存延迟；多写入流边算边写 | 文字模式 |
| tl.load 规则 | 合并相同 load/计算/store；避免 `other` 参数（内部触发 `tl.where`，导致 load 无法并行） | 推荐先无掩码 load 再 `tl.where` 组合；连续访问用 `tl.insert_slice` |
| 索引与分支规范 | 用 `tl.arange` 生成二维索引；避免 `tl.where`；避免对同一 tensor 多次 `insert_slice`；if-else 分支同名变量 Shape 必须一致；tensor 必须 `.contiguous()`；不变入参声明 `tl.constexpr` | `tl.arange`、`tl.constexpr` |
| 规约维度选择 | 规约优先选最大维度 | 文字模式 |

## 4.2 30 个优化点

按 1→30 顺序命中式检查，一次迭代只用一个优化点：

- **通用类**：1 入参静态化（`tl.constexpr`）、2 Tiling（连续轴向量化）、3 分核（Grid 匹配核数）、4 离散访存、5 Scalar→Vector、6 避免向量 API 标量降级、7 Pass 消除合并、8 维度合并、9 libdevice、10 循环不变量外提、11 Load 重排序、12 Grid 多路径特化、13 Autotune、14 混合策略、17 冗余边界运算消除、18 Kernel 分裂（多 case 且 speedup<2.0 必查）、30 IR 分析（每轮最后必执行）。
- **结构类**：19 Cube/MTE3 分阶段解耦、20 Host 侧张量拼接、21 Workspace 物化解耦、22 Latency-Bound Tile 合并、23 Device-side Gather 连续化、24 Matmul 链中间 buffer dtype、25 输出预初始化。
- **算子专用**：15 归一化大 BLOCK、16 连续拷贝聚合、26 Interpolate、27 Pooling、28 Matmul Transpose、29 CV 融合（含 pingpong/tiling 3 个子文档）。
- **最终步骤**：Block Size Scaling 必做；多 case 不达标再做 Kernel 分裂。
- 各优化点的实现时只使用目标版本公开 API，并在修改记录中附源码位置与验证命令。

## 4.3 SIMT→SIMD 原子操作向量化（PR 案例）

- **理论说明**：SIMT 模式下带离散掩码的原子操作（atomic add/max/min/and/or/xor）逐元素串行发射，改用 SIMD 向量化路径。
- **性能对比**：shape 16,16,16，`atomic_add_3d INT32`：SIMD **3.309µs** vs SIMT **81.163µs**（约 24.5 倍）；`atomic_and_3d INT32` 4.114 vs 96.141；FLOAT `atomic_min_3d` 3.372 vs 92.389；ROLLBACK 路径 3.5~4.3µs（triton-ascend PR #218：<https://github.com/triton-lang/triton-ascend/pull/218>； 该改动后被 revert，见 [cases/triton/pr_triton_atomic_simd_24x.md](cases/triton/pr_triton_atomic_simd_24x.md)）。
- **代码示范**：**未提供可直接复用的版本无关实现；执行时必须核对目标版本 API、附源码位置并实测验证**。

## 4.4 constexpr 特化粒度控制

去除不必要的 `tl.constexpr` 修饰，避免运行时参数变化触发 Triton 重复 JIT 编译（vllm-ascend PR #7483：<https://github.com/vllm-project/vllm-ascend/pull/7483>；未附量化数字；案例全文见 [cases/triton/pr_triton_constexpr_recompile.md](cases/triton/pr_triton_constexpr_recompile.md)）。
