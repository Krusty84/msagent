# MR !168（cann/ops-tensor）+ MR !7782（cann/ops-nn）RotateQuant Blaze 组件化迁移试点【PR】

## 基本信息
- 算子类别：matmul（rotate_quant，量化 matmul）
- DSL/框架：ascendc
- 类型：PR
- 来源可信度：一手 PR 原文

## 来源链接
- PR/出处链接：实现 PR <https://gitcode.com/cann/ops-tensor/pull/168>（ 已验证可达，验证日期 2026-08）；接入 PR <https://gitcode.com/cann/ops-nn/pull/7782>（依赖前一个 PR； 已验证可达，验证日期 2026-08）
- 优化代码查看：实现 PR 代码改动页 <https://gitcode.com/cann/ops-tensor/pull/168/files>（ 已验证可达，验证日期 2026-08）；接入 PR 代码改动页 <https://gitcode.com/cann/ops-nn/pull/7782/files>（ 已验证可达，验证日期 2026-08）

## 问题与瓶颈
`matmul/rotate_quant` 算子（FP16/BF16 输入，MX FP4 E2M1 / FP8 E4M3FN/E5M2 输出，K 32/64/128，2D/3D 旋转 + 分组截断量化）原为私有 CMCT 实现，需迁移为 ops-tensor Blaze 公共组件；原组件无法组合出 M/批次交错调度、A 行交错与 B 全载复用的 Fixpipe MMAD、单 AIC 对两个 AIV 的交替单消费者协议。环境：Ascend 950 / DAV_3510，CANN 9.1.0，BiSheng/CCEC clang 15.0.5。

## 优化方法（理论手段）
本案例价值在"性能不回归的组件化"与验收协议：
1. **Fixpipe MMAD + B 全载/A 交错**：B 常驻 L0 复用，A 按批次交错载入，MTE2 与 MMAD 流水重叠；逐层新增 4 个组件：`BlockSchedulerMatmulInterleavedBatch`、`BlockMmadMatmulBFullLoadInterleavedAFixpipe`、`KernelMatmulFixpipeAlternatingAiv`（1:2 MIX，AIC 每轮只通知一个交替 AIV）、`BlockEpilogueMxQuantGroupClamp`。
2. **AIC 对双 AIV 交替单消费者协议**：AIC 每轮只唤醒一个 AIV 消费 fixpipe 输出，避免双 AIV 争用导致的同步抖动。
3. **严格验收协议**：逐字节一致性比对（含独立 256 字节物理越界防护区检查、重复运行确定性）；性能采用同设备、同 CANN、同 tiling、正反交错采集顺序，每用例每进程预热 5 次、采集 20 条目标任务、3 个独立进程取中位数均值——可复现性能比对的方法论模板。

## 性能对比
| 指标 | 优化前（原实现） | 优化后（迁移实现） | 变化 |
|---|---|---|---|
| 任务中位数均值（稳定验收用例 BF16/E4） | 29.159 us | 29.305 us | 回退约 0.50% |

（pilot 记录 G3 节原文）MTE2/MMAD/Fixpipe 流水活跃时间无增加，性能负责人按例外验收通过（结论明确记录"不是'无差异'"）。

## 适用范围与警示
- 适用：Ascend 950 / DAV_3510，CANN 9.1.0；FP16/BF16 输入，MX FP4 E2M1 / FP8 E4M3FN/E5M2 输出，K 32/64/128。
- ** 风险说明**：本案例是"迁移性能持平"案例而非提速案例，引用时勿表述为优化收益。
