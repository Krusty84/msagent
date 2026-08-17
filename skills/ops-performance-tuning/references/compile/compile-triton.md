# Triton-Ascend 算子工程：编译、运行、验证、单测方法

> 本文档目的：整理 triton-ascend 框架的算子工程方法（简介与仓库地址、依赖版本、编译命令、运行/验证方法、单测方式）。多框架总览与环境速查见 [算子类型路由](../case-routing.md)。

## 2. triton-ascend

### 2.1 简介与仓库

Triton 语言的 Ascend NPU 后端。仓库：<https://github.com/triton-lang/triton-ascend>（README 含完整安装文档）；GitCode 镜像：<https://gitcode.com/Ascend/triton-ascend>。

### 2.2 环境与版本对齐

Python、CANN、PyTorch、torch_npu 与 Triton-Ascend 必须采用目标 release 兼容表中的同一组版本；不得固化某个历史 README 快照的推荐版本。

```bash
git rev-parse HEAD
rg -n "CANN|torch_npu|Python|compatib|version" README* docs/ 2>/dev/null
python3 -c 'import torch, torch_npu, triton; print(torch.__version__, torch_npu.__version__, triton.__version__)'
```

### 2.3 安装（编译）

```bash
# 方式一：兼容表指定的 wheel
python3 -m pip install triton-ascend==<compatible-version> --extra-index-url=https://mirrors.huaweicloud.com/ascend/repos/pypi
# 方式二：源码
python3 -m pip install ninja cmake wheel pybind11
git clone https://github.com/triton-lang/triton-ascend.git && cd triton-ascend
python3 -m pip install -e .
# LLVM、clang/lld 和容器基础镜像按该分支安装文档准备。
```

### 2.4 运行/验证方法

与上游 Triton 一致——`@triton.jit` 写 kernel，配合 torch/torch_npu 张量直接调用；首次调用时 JIT 编译（Triton IR → Ascend 后端 → CCE 二进制）。典型下游用法见 sgl-kernel-npu、vllm-ascend。

### 2.5 单测方式

（PR 模板原文）`/test` 跑 lit 测试、`/unittest` 跑 C++ 单测、`/python/test` 跑端到端测试；仓库根目录有 `test/`（lit）、`unittest/`（C++ gtest）目录。

### 2.6 性能分析入口

docs/en/debug_guide/profiling.md（<https://github.com/triton-lang/triton-ascend/blob/main/docs/en/debug_guide/profiling.md>）：board profiling 分析 tiling（如 block dim 超过 48 个 vector core 导致 host 调度开销）、仿真流水图定位 MTE2/VECTOR 流水中断、代码热点（scalar 指令占比过高 → 优化标量计算/向量化）。

### 2.7 环境修复与 SoC 配套（实测：Ascend950PR / CANN 9.1）

`import torch_npu` 成功不代表可用——**真正的可用性门禁是"最小 triton kernel 上板跑通"**（见 §2.4，vector add 级别）。环境修复按以下顺序排查：

1. **`Unsupported soc version: Ascend950PR 9579`**：torch_npu 版本太旧不认识 950PR。实测 torch_npu 2.6.0.post5 报错，2.11.0/2.12.0 可用；公开 issue 中 950PR 可用组合为 torch 2.10 + torch_npu 2.10 + triton-ascend 3.2.1（同代组合均可先试）。torch 用 `+cpu` wheel（`--index-url https://download.pytorch.org/whl/cpu`）可同时满足 torch-npu 的 `torch==X+cpu` 依赖钉。
2. **triton-ascend wheel 与 CANN 头文件不兼容**：pypi 的 triton-ascend 3.2.0 的 `npu_utils.cpp` 引用已被 CANN 9.1 改名的枚举（`RT_LIMIT_TYPE_SIMT_WARP_STACK_SIZE`→`RT_LIMIT_TYPE_SIMT_STACK_SIZE`），运行时 JIT 编译 npu_utils 失败；更新版本（3.2.1+）或按报错改名打补丁。
3. **kernel 启动 207000（"This feature is not supported"，plog 中 `Custom hand fail! name=<kernel>`）**：import 和 JIT 编译都成功但 launch 被运行时拒绝。实测区分两类：① 后端整体不支持该 SoC（简单 add kernel 也挂）→ 换配套组合；② 仅复杂 kernel 挂（开发中 fork 的 codegen 不完整）→ 换正式 release 后端。**不要在这种状态下调性能**。
4. **新 API 缺失**（如 `tl.insert_slice`）：kernel 用了比本机 triton-ascend 更新的语言 API，属于工具链版本不足，只能升级/源码构建后端——按任务边界这属于开发工作，不是性能调优，标记 `BLOCKED` 并记录所需 API。
5. **triton-ascend 的元数据依赖**：`torch-npu` 在 pypi 上钉 `torch==X+cpu`；triton-ascend 对 scipy/pytest/psutil/pandas 等有版本钉，用 venv 隔离安装避免污染系统环境；`import triton` 需要 `pybind11`、`yaml`（pyyaml）等隐式依赖，缺失时逐个补装。
6. 版本配套查询入口：`pip index versions torch-npu` / `pip index versions triton-ascend`（pypi 只发布到 3.2.0，更新版本在 GitCode release 或源码构建）。
