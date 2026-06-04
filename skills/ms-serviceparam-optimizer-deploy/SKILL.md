---
name: ms-serviceparam-optimizer-deploy
description: 当用户需要部署 msServiceProfiler 服务化参数自动寻优工具时使用。负责真机轻量化寻优模式的安装和验证。
---

# 服务化参数自动寻优部署

## 工作范围

本 skill 负责将工具装好并验证能用。

其他内容由对应 skill 负责：
- 运行环境检查：`ms-serviceparam-optimizer-env-check`
- 首次参数范围推荐：`param-recommend`
- 生成或修改 `config.toml`：`ms-serviceparam-optimizer-config`

## 支持的硬件产品

寻优工具仅支持以下昇腾推理产品：

|产品类型| 是否支持 |
|--|:----:|
|Atlas A3 训练系列产品/Atlas A3 推理系列产品|  √   |
|Atlas A2 训练系列产品/Atlas A2 推理系列产品|  √   |
|Atlas 200I/500 A2 推理产品|  √   |
|Atlas 推理系列产品|  √   |
|Atlas 训练系列产品|  x   |

> **注意**：
> - 目标运行环境必须是支持的 Linux 昇腾机器，不支持 Windows
> - Atlas 训练系列产品不支持，请确认使用的是推理系列产品

## 安装流程

用户说"安装""部署"时，直接执行以下步骤，**不要只给命令**。

### 0. 默认使用阿里云 PyPI 镜像

为减少安装等待时间，默认优先使用阿里云镜像：

```bash
export PYPI_MIRROR=https://mirrors.aliyun.com/pypi/simple/
```

后续 `pip install` 均默认带上：

```bash
python -m pip install -i "$PYPI_MIRROR" ...
```

若用户已有公司内网镜像、离线源或明确指定其他源，则尊重用户配置，不强制改成阿里云镜像。

### 1. 检查仓库是否存在

```bash
ls ms_service_profiler pyproject.toml
```

若仓库不存在，**直接克隆**：

```bash
git clone https://gitcode.com/Ascend/msserviceprofiler.git
cd msserviceprofiler
```

### 2. 判断当前目录和安装方式

msserviceprofiler 仓库结构：

```
msserviceprofiler/          ← 仓库根目录（ms_service_profiler 主包）
├── ms_service_profiler/   ← 主包源码
├── ms_serviceparam_optimizer/  ← 寻优工具子包（依赖主包）
└── ...
```

安装分两步：**先装主包，再装 optimizer 子包**。根据当前目录选择对应命令：

**在仓库根目录**（有 `pyproject.toml` 和 `ms_service_profiler/` 子目录）：

```bash
python -m pip install -i "$PYPI_MIRROR" -e .                           # 1. 先装主包
python -m pip install -i "$PYPI_MIRROR" -e ./ms_serviceparam_optimizer[real]  # 2. 再装 optimizer 子包
```

**在 `ms_serviceparam_optimizer/` 子目录内**（有 `pyproject.toml`）：

```bash
# 先确认主包是否已装
python -m pip show ms_service_profiler
# 若未装，需切回仓库根目录按上述方式安装
```

> 注意：直接 `pip install -e ./ms_serviceparam_optimizer[real]` 会因缺少 `ms_service_profiler` 依赖而失败。

### 3. 检查编译工具（主包含 C++ 扩展）

`ms_service_profiler` 主包含 C++ 扩展，缺少 CMake 和 MSVC 编译器会导致编译失败：

```bash
cmake --version && cl /?
```

若缺失，告知用户：
- **Linux 昇腾机器**：安装 CMake 和 GCC/CC 即可。
- **Windows/其他平台**：寻优工具仅支持指定的昇腾推理产品，请在支持的 Linux 昇腾机器上执行。

### 4. 安装

真机轻量化模式：

```bash
python -m pip install -i "$PYPI_MIRROR" -e ./ms_serviceparam_optimizer[real]
```

### 5. 验证

```bash
msserviceprofiler optimizer --help
```

### 6. 汇报结果

告诉用户安装是否成功。若编译失败，给出具体缺失项和修复建议；并给下一步建议：运行环境检查、配置 `config.toml` 或开始参数推荐。

## 卸载

卸载前说明会移除 Python 包，征得同意后执行：

```bash
python -m pip uninstall ms_serviceparam_optimizer
```

## 要求

- 始终中文回答。
- 用户同意安装后，要实际检查路径、执行安装并验证 CLI，不要只给命令。
- 不要把运行、配置、结果解读内容塞进本 skill；交给专项 skill 或文档。
- 涉及会创建目录、安装依赖、卸载包的动作，先说明影响并征得用户同意。