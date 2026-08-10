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
