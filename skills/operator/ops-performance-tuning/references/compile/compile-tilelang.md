# TileLang-Ascend 算子工程：编译、运行、验证、单测方法

> 本文档目的：整理 tilelang-ascend 框架的算子工程方法（简介与仓库地址、依赖版本、编译命令、运行/验证方法、单测方式）。多框架总览与环境速查见 [算子类型路由](../case-routing.md)。

## 3. tilelang-ascend

### 3.1 简介与仓库

基于 TVM 的 Python tile DSL 的昇腾后端，两条路线：

- Ascend C & PTO 后端：<https://github.com/tile-ai/tilelang-ascend>（ascendc_pto 分支；GitCode 镜像 <https://gitcode.com/runnee-wwl/tilelang-ascend>）；
- AscendNPU IR (MLIR) 后端：<https://github.com/tile-ai/tilelang-mlir-ascend>（npuir 路线）。
- 上游 tile-ai/tilelang 已于 2025-09-29 宣布支持 AscendC 与 Ascend NPU IR 两条后端分支（PyPI 页面 Latest News：<https://pypi.org/project/tilelang/>）。

### 3.2 依赖与后端选择

先确定 `ascendc_pto` 或 `npuir` 路线，再读取该分支的 CANN、torch_npu、Python 和硬件兼容要求。两条路线的工具链不能混用；历史最低版本不等同于当前分支完整兼容关系。

```bash
git branch --show-current && git rev-parse HEAD
rg -n "CANN|torch.npu|torch_npu|Python|ascendc_pto|npuir" README* docs/ 2>/dev/null
python3 -c 'import torch_npu; print(torch_npu.__version__)'
```

### 3.3 安装（编译）

```bash
export ASCEND_HOME_PATH=<target-cann-path>/ascend-toolkit/latest
# 方式一：预构建 wheel
pip install tilelang-*.whl
# 方式二：源码构建 wheel
git clone --recursive https://github.com/tile-ai/tilelang-ascend.git && cd tilelang-ascend
./build_wheel_ascend.sh [--enable-llvm]
pip install dist/tilelang-*.whl
# 方式三：源码直接安装
bash install_ascend.sh && source set_env.sh
# npuir 路线：bash install_npuir.sh
```

### 3.4 运行/验证方法

```bash
cd examples/gemm
python example_gemm.py    # 成功打印 "Kernel Output Match!"
```

kernel 用 Python DSL + `@tilelang.jit` 编写；`generate_source()` 可 lower 成 Ascend C 源码（xLLM 等项目即按此集成，见 <https://docs.xllm-ai.com/zh/dev_guide/tilelang_ascend_kernel_dev/>）。

### 3.5 模式与编程模型

- 环境变量 `TILELANG_ASCEND_MODE`：`Developer`（自动 C/V 作用域分离、自动插同步）/ `Expert`（手动 `T.Scope("C")/T.Scope("V")` + `T.set_cross_flag/wait_cross_flag`）。
- 向量化两种风格：`T.Parallel(M, N)` 自动向量化；`T.tile.add(...)` 显式 tile 原语。
- 软件流水 `T.Pipelined`（2026-01-15 引入）；片上内存 `alloc_L1/alloc_ub/...`；`T.printf`/`T.dump_tensor` 调试。

### 3.6 单测方式

examples 下 `test_*.py`（精度 + `--level perf` 性能档，见 PR #1494）；CI 有 ci_performance.py。
