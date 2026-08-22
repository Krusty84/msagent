# MR !7420（ops-nn）Quantize A2 两轮微优化【PR】

## 基本信息
- 算子类别：vector-elementwise（quantization）
- DSL/框架：ascendc
- 类型：PR
- 来源可信度：一手 PR 合并描述（GitCode 仓首页提交记录）

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/ops-nn/pull/7420>（ 路由可达，合并描述已核验，验证日期 2026-08）
- 优化代码查看：<https://gitcode.com/cann/ops-nn/pull/7420/files>（ 路由可达，验证日期 2026-08；重点目录 `experimental/quant/quantize/`）

## 问题与瓶颈
Ascend 910B3 上的 Quantize 新实现采用统一 FP32 计算路径。初版包含 scale 广播 UB 缓冲与真 `Div`，且 `SetDeqScale`/尾部 `PipeBarrier<PIPE_V>` 存在可消除的重复开销。目标平台原部署包没有可运行的同款 builtin，因此只能做新 kernel 的前后自对比。

## 优化方法（理论手段）
1. **除法改标量倒数乘**：把 `Duplicate(scale) + Div` 改为 `Muls(1/scale)`，同时释放 scale 广播 UB 缓冲，减少指令与片上内存占用。
2. **循环不变量外提**：把 `SetDeqScale` 提到 `Init`，从热循环中移除重复状态设置。
3. **删除冗余同步**：去掉无数据依赖的尾部 `PipeBarrier<PIPE_V>`。
4. **保持运行时 dtype 分发正确**：`zero_points` 按运行时真实 dtype 读取，避免优化时破坏 per-channel 可选输入语义。

## 性能对比
| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 8 组代表性用例 device 时延合计 | 776.2 us | 766.7 us | 1.012× |
| int8 per-tensor 代表用例 | 原文未逐项列值 | 原文未逐项列值 | -2.7%~-3.1% |

## 适用范围与警示
- 环境：Atlas A2（Ascend 910B3）、CANN 9.0.0；两轮优化后 208/208 精度用例通过。
- 适用于 per-tensor/per-channel Quantize 及类似“标量参数广播后做逐元素除法”的算子。
- 无同款 builtin 基线，1.012× 仅表示该新 kernel 自身前后变化，禁止表述成相对系统算子加速。
