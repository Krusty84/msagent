# CATLASS 算子工程：编译、运行、验证、单测方法

> 本文档目的：整理 catlass 框架的算子工程方法（简介与仓库地址、依赖版本、编译命令、运行/验证方法、单测方式）。多框架总览与环境速查见 [算子类型路由](../case-routing.md)。

## 1. catlass

### 1.1 简介

CATLASS（CANN Templates for Linear Algebra Subroutines）：聚焦高性能矩阵乘类算子基础模板，抽象分层（Gemm/Block/Tile 层）白盒化组装，支持 Flash Attention 等复杂流水排布。官方称"定制 shape 下性能可达相应算子标杆（aclnn）的 0.98~1.2 倍"（README 原文，CANN8.2.RC1 环境）。

### 1.2 依赖与版本对齐

硬件范围、GCC/CMake/Python 下限和 CANN 兼容关系以目标 CATLASS tag/branch 的 README、发布说明和本机 CANN 为准。不要把其他 release 出现过的错误当作当前版本不受支持的证据。精度测试依赖的 `cann-*-ops` 包必须与 SoC、CANN 配套。

```bash
git rev-parse HEAD
git describe --tags --always 2>/dev/null || true
rg -n "CANN|Python|GCC|CMake|Ascend 950|Atlas A2|Atlas A3" README* docs/ 2>/dev/null
cmake --version && python3 --version
```

### 1.3 编译与运行

```bash
# 1. 使能 CANN 环境
source /usr/local/Ascend/ascend-toolkit/set_env.sh
# 2. 查看当前分支参数，再编译指定样例
bash scripts/build.sh --help 2>/dev/null || true
bash scripts/build.sh 00_basic_matmul
# 3. 运行
cd output/bin
./00_basic_matmul 256 512 1024 0   # 可执行文件名 m n k [device_id]
# 成功标志：打印 "Compare success."（精度比对通过）
```

### 1.4 单测与调测

- `tests/` 目录存放单元测试（MR !449 "增加unittest单元测试"引入）；v1.6.0 新增 Ascend950 Tile 层组件全量单元测试与算子级测试框架 optest（README Latest News）。
- 调测工具链（docs/evaluation_collections.md）：msDebug（类 gdb）、printf、ascendc_dump；性能侧 msProf（单算子）、Profiling（整网）、**msTuner_CATLASS**（Tiling 自动寻优工具）。

### 1.5 关键编程概念

`DispatchPolicy` 调度策略决定流水排布（docs/dispatch_policies）：`MmadAtlasA2Pingpong`（L1/L0 双缓冲）、`MmadAtlasA2Preload`（+ ShuffleK + Block 间预加载）、`MmadAtlasA2PreloadAsync`（nBuffer 机制，用于 Grouped Matmul）。调优指引：docs/catlass_optimize_guidance.md；swizzle 策略文档 swizzle_explanation。
