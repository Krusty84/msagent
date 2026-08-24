# MR !507 add matmul_fixpipe_opti —— FixPipe 随路搬出 + dualDstCtrl 双目标模式【PR】

## 基本信息
- 算子类别：matmul
- DSL/框架：catlass
- 类型：PR
- 来源可信度：一手 PR 原文

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/catlass/pull/507>（ 已验证可达，验证日期 2026-08）；合并提交信息见 <https://gitcode.com/cann/catlass/tree/v1.5.0/docs>
- 优化代码查看：<https://gitcode.com/cann/catlass/pull/507/files>（ 已验证可达，验证日期 2026-08）

## 问题与瓶颈
矩阵乘结果从 L0C 集中搬出到 GM 形成尾部瓶颈，单 Vector 核写出带宽受限。

## 优化方法（理论手段）
1. **FixPipe 随路搬出**：每完成一个基本块的计算，结果数据即通过 Fixpipe 搬出到 UB 上，而不是等待全部计算完成后集中搬出。
2. **dualDstCtrl 双目标模式**：启用双目标模式控制时，计算结果矩阵会被分成两部分，并行写入两个 Vector 核（一个 Cube 核对应两个 Vector 核）的专属 UB 中，提升写出并行度。
3. **UB Double Buffer**：每个 Vector 核的 UB 支持独立开启 Double Buffer 以加速流水效率。

（以上为 MR 描述原文：即 Cube/Vector 流水并行、随路量化/搬出（FixPipe）、UB 双缓冲、双 Vector 核并行写出。）

## 性能对比
MR 描述未附量化数字（标签为"新特性"）。

## 适用范围与警示
- 机制依赖 Cube 核与两个 Vector 核的配对（一个 Cube 核对应两个 Vector 核）及 FixPipe 通路；MR 未给出适用 shape/dtype 范围，使用前需实测。
