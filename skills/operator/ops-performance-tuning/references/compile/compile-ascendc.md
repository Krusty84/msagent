# Ascend C 算子工程：编译、运行、验证、单测方法

> 本文档目的：整理 Ascend C 框架的算子工程方法（简介与文档、依赖、编译命令、运行/验证方法、单测方式）。多框架总览与环境速查见 [算子类型路由](../case-routing.md)。

## 导航

- [msopgen 工程](#42-方式一msopgen-工程化流程)
- [cann-samples 工程](#43-方式二cann-samples-直接-cmake-工程)
- [CANN 官方算子仓](#45-cann-官方算子仓编译ops-nnops-cvops-mathops-transformer)
- [asc-devkit 独立工程](#46-asc-devkit-独立工程编译)
- [正确性与调测](#47-单测调试)

## 4. Ascend C

### 4.1 简介与文档

CANN 原生算子编程语言。官方文档：Ascend C 算子开发指南 <https://www.hiascend.com/document>（CANN Community Edition → opdevg/ascendcopdevg）；Ascend C 最佳实践 <https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/850alpha002/opdevg/ascendcbestP/>；性能优化实践指南见 asc-devkit 仓 docs/guide（<https://gitcode.com/cann/asc-devkit>）。

### 4.2 方式一：msopgen 工程化流程

（asc-tools 样例原文，<https://gitcode.com/cann/asc-tools>）

```bash
source /usr/local/Ascend/cann/set_env.sh      # 或 $HOME/Ascend/cann/set_env.sh
# 1. 生成自定义算子工程（soc_version 用 npu-smi info 查，Name 前加 Ascend）
msopgen gen -i ./op_dev/add_custom.json -f <framework> -c ai_core-<soc_version> -lan cpp -out ./custom_op
# 2. 拷贝 kernel/host 实现
cp -rf ./op_dev/op_kernel custom_op && cp -rf ./op_dev/op_host custom_op
# 3. 构建并安装自定义算子包
cd custom_op && bash build.sh
cd build_out && ./custom_opp_<target_os>_<target_arch>.run
# 4. 编译运行 aclnn 调用验证程序
cd ../../aclnn_invocation && mkdir -p build && cd build
cmake .. && make -j && ./execute_add_op      # 成功打印 "test pass"
```

### 4.3 方式二：cann-samples 直接 CMake 工程

（来源：<https://gitcode.com/cann/cann-samples>；MR !208 引入 NPU_ARCH 机制；!369 新增 RegBase/VF 样例）

```bash
source /usr/local/Ascend/cann/set_env.sh
# 全量编译
cmake -S . -B build -DNPU_ARCH=dav-3510   # Ascend950=dav-3510；Ascend910B/C=dav-2201
cmake --build build --parallel

# 单样例编译（推荐）
cmake --build build --target vector_add   # MemBase 编程模型 (TQue/TPipe)
cmake --build build --target vector_function_add  # RegBase/VF 编程模型（仅 dav-3510）
cmake --build build --target matmul       # Cube 编程模型 (Tensor API)
```

- `vector_add`：MemBase 模型，使用 TQue/TPipe 实现向量加法
- `vector_function_add`：RegBase 模型，`__simd_vf__` + `AscendC::Reg::*` API，寄存器上完成计算（仅 Ascend 950）
- `matmul`：Tensor API 实现矩阵乘法，学习 Cube Core 编程
- `cann_sample_check_arch(dav-3510)`：架构校验宏，dav-2201 的样例在 dav-3510 自动跳过

#### 4.3.1 案例脱离 cann-samples 仓独立构建（盲测/抽离场景）

单案例目录（如 `Samples/2_Performance/<case>`）抽出仓库后不能直接 cmake——它依赖仓顶层设施。最小重建配方（在产物目录建构建树，源仓只读复制）：

1. 复制仓根 `CMakeLists.txt`、`cmake/`（`ascend.cmake`、`sample_common.cmake`、`tensor_api.cmake` 等）到构建树；
2. **打补丁跳过 git 依赖**：`tensor_api.cmake` 在非 git 目录会执行 `git submodule update` 导致构建失败——ops-tensor 已存在时删掉/跳过该 custom target；`third_party/ops-tensor` 整体复制或软链；
3. 构建树顶层 CMakeLists 改为只 `add_subdirectory` 目标案例；
4. SHMEM 类案例还需 `cmake/shmem.cmake`，它会**现场编译** `third_party/shmem`（`-DSOC_TYPE=Ascend950`），耗时长；已有 `install/` 产物时优先复用而不是重建；从别的构建树复制的 `third_party/shmem/build` 含旧路径的 CMakeCache，重配前必须清掉。

另有一种自包含形态（单 `.asc` + 案例自带 `cmake/ascend.cmake` + `include/sample_common.h`）：直接 `cmake -S . -B build -DNPU_ARCH=dav-3510` 即可，不依赖 cann-samples 顶层。

**数据脚本依赖**：cann-samples 性能案例的 `scripts/gen_data.py` 常需 `torch`/`en_dtypes`/`ml_dtypes`（e8m0/e4m3fn 等），基础 CANN 环境和常见 conda env 没有这些包——Step 1 环境检查时用 `python3 -c "import <mod>"` 逐个核对脚本 import，缺失则在产物目录建隔离 venv 安装。注意 host 二进制可能用 `system("python3 verify_result.py")` 隐式依赖 PATH 中的 python3，跑验证前确认 PATH 指向装了依赖的解释器。

### 4.4 方式三：gitee ascend/samples AddCustom

FrameworkLaunch/AclNNInvocationNaive：改 CMakeLists 中 toolkit 路径 → `export NPU_HOST_LIB=.../lib64` → `cmake .. && make && ./execute_add_op` 或 `bash run.sh`。

### 4.5 CANN 官方算子仓编译（ops-nn/ops-cv/ops-math/ops-transformer）

CANN 官方算子仓（`ops-nn`、`ops-cv`、`ops-math`、`ops-transformer`）使用统一 `build.sh` 工程体系。

#### 4.5.1 自定义算子包（`--pkg`，完整编译+打包）

```bash
source /usr/local/Ascend/cann/set_env.sh
cd <repo_root>

# 单算子编译（推荐日常开发使用）
bash build.sh --pkg --soc=ascend950 --ops=<op_name> --vendor_name=custom -j16

# 多算子批量编译
bash build.sh --pkg --soc=ascend950 --ops=op1,op2,... --vendor_name=custom -j16

# experimental 贡献目录的算子需加 --experimental
bash build.sh --pkg --experimental --soc=ascend950 --ops=<op_name> -j16

# 离线环境需指定第三方依赖路径
bash build.sh --pkg --soc=ascend950 --ops=<op_name> --cann_3rd_lib_path=/path/to/deps -j16
```

成功标志：`Self-extractable archive "cann-ops-<repo>-custom_linux-x86_64.run" successfully created.`

> **大算子说明**：conv、reduce 等类别使用 `--pkg` 时可能拉入大量关联内核。快速迭代先用 §4.5.2 的 `--opkernel`，最终接口回归再执行 `--pkg`。
>
> **Python 环境依赖**：ES wheel 打包依赖 `pip`、`setuptools`；部分工程依赖 `packaging`。缺失时报 `ModuleNotFoundError`，用当前构建解释器执行 `python3 -m pip install <module>`。详见 [troubleshooting](../troubleshooting.md)。

#### 4.5.2 仅编译 AI Core 内核（`--opkernel`，推荐大算子使用）

仅编译算子内核 `.o` 文件，跳过关联算子和 ES/打包，适合大算子和快速迭代：

```bash
bash build.sh --opkernel --soc=ascend950 --ops=<op_name> --build-type=Release
```

#### 4.5.2.1 `--pkg` 编译后如何运行与 profiling

`--pkg` 产出的是 `.run` 安装包（位于 `build_out/`），需要安装后才能调用：

```bash
# 1. 安装算子包
cd build_out && ./cann-ops-<repo>-custom_linux-x86_64.run
# 2. 用 ACLNN 调用 + msOpProf 采集（当前官方常见位置参数形式）
msprof op --output=./prof_baseline \
    --kernel-name=<kernel_name> --launch-count=20 \
    <aclnn_demo> <args>
```

不同 CANN/msOpProf 版本可能改用独立 `msopprof`、`--application="..."` 或不同指标参数名。运行前必须查看 `msprof op --help`/`msopprof --help`，只使用本机声明支持的形式；`--warm-up` 不存在时在 demo 内部做固定 warmup。完整兼容模板和 A2/A3/A5 指标矩阵见 [算子类型路由 §3](../case-routing.md)。

> `--opkernel` 产出 `.o` 内核二进制，常见位置为 `build/binary/`。本机 `msprof op --help` 支持 `--config` 时可直接采集，无需安装自定义算子包。

#### 4.5.3 添加调试信息 `-g`（源码映射/Profiling 用）

需要 msOpProf 源码映射时，**保持优化不降级**，只加 `-g`：

```bash
bash build.sh --opkernel --soc=ascend950 --ops=<op_name> \
    --build-type=Release --bisheng_flags=ccec_g
```

- `ccec_g` 在统一构建系统中映射为 Device 编译参数 `-g`
- **禁止使用 `-O0`**（msOpProf 不支持 `-O0` 调优）
- Host 编译命令中有 `-g` 不能证明 Kernel 包含调试信息，需验证 Device 编译命令

#### 4.5.4 平台映射

| 机器代际 | `--soc` 参数 |
|----------|-------------|
| A2 (Ascend910B) | `ascend910b` |
| A3 | `ascend910_93` |
| A5 (Ascend950) | `ascend950` |

### 4.6 asc-devkit 独立工程编译

asc-devkit 不使用 `build.sh --pkg` 体系，为独立 CMake 工程，**必须显式设置 `ASCEND_CANN_PACKAGE_PATH`**：

```bash
source /usr/local/Ascend/cann/set_env.sh
cmake -S . -B build \
  -DNPU_ARCH=dav-3510 \
  -DASCEND_CANN_PACKAGE_PATH=$ASCEND_HOME_PATH
cmake --build build --parallel 4
```

- 未设置 `ASCEND_CANN_PACKAGE_PATH` 会报 `FATAL_ERROR`
- `ASCEND_CANN_PACKAGE_PATH` 必须指向有效 CANN 目录（如 `/usr/local/Ascend/cann-9.1.0`）
- 需要 `-g` 时，通过 CMake `ascendc_compile_options(<target> PRIVATE -g -O2)` 注入

### 4.7 单测/调试

CPU 孪生调试（孪生调试器）、msDebug、printf/ascendc_dump 打点；性能用 msprof（aic_mte2_ratio、cube/vector 利用率、流水图）。

### 4.8 核心编程范式

SPMD 多核 + 核内流水（CopyIn(MTE2)→Compute(V/M)→CopyOut(MTE3) 三阶段异步队列）；`TPipe`/`TQue` + `InitBuffer(queue, 2, size)` 开启 double buffer；Vector 向量化 API（Add/Mul/ReduceSum...）、Cube Mmad/Fixpipe；Tiling 由 host 侧 tiling 函数计算并传入 kernel。
