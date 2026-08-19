# §1.4 Ascend C · 内存层级利用

> 本文件覆盖 Ascend C 的内存层级利用优化技术。

- 存储层级表：GM ~1.6TB/s、L2 ~192MB/7TB/s、L1、L0A/L0B、L0C、UB、BT/FP Buffer。
- **UB Buffer 融合**：n 次连续 Vector 运算 GM 搬运从 2n 降为 2（文字模式：中间结果留 UB，不落 GM）。
- **L0C 原地累加矩阵乘**；**小矩阵长驻 L1**；**BT Buffer 放 bias**（Mmad 一步融合）；**FP Buffer 随路量化（Fixpipe）**。
- 反模式：FP16/BF16 先 Cast FP32、GM↔UB 用 DataCopyPad、kernel 内使用 std 数学函数、repeatTime>255。
