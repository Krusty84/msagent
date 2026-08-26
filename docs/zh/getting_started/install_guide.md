# msAgent安装指南

## 1. 安装说明

本文面向第一次使用 MindStudio-Agent 的用户，帮助您完成 msAgent 安装。

推荐使用**一键安装**（[3.1 一键安装（推荐）](#31-一键安装推荐)）：自动安装 `mindstudio-agent` 最新版本，不与现有环境冲突。

同时支持 [pip 安装（备选）](#32-pip-安装备选)与[源码安装](#33-源码安装)两种方式。

## 2. 环境要求

- **无需预先安装 Python**：一键安装会自动下载所需的 Python 版本（`>= 3.11`）。

- `glibc >= 2.34`

  用于满足 `msprof-mcp` 中 `trace_processor` 二进制依赖（建议操作系统：`Ubuntu >= 21.10`、`openEuler >= 21.09`，其他操作系统请自行查询）

- 使用本工具前需要安装CANN，具体操作请参见《[CANN 快速安装](https://www.hiascend.com/cann/download)》安装昇腾NPU驱动和CANN软件（包含Toolkit和ops包），并配置环境变量。

## 3. 安装方式

### 3.1 一键安装（推荐）

安装器默认使用国内 PyPI 镜像，自动回退官方源。

**Linux / macOS / WSL：**

```shell
curl -LsSf https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.sh | bash
```

**Windows（PowerShell 5.1 及以上）：**

```powershell
irm https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.ps1 | iex
```

执行如下命令提示msAgent版本即安装成功。

```shell
msagent --version
```

#### 常用环境变量（可选）

| 变量 | 说明 |
| --- | --- |
| `MSAGENT_VERSION` | 安装指定版本，如 `MSAGENT_VERSION=26.1.0`（默认安装最新版） |
| `MSAGENT_INDEX` | 指定 PyPI 镜像地址，如 `MSAGENT_INDEX=https://pypi.org/simple` |
| `MSAGENT_PYTHON` | 工具环境使用的 Python 版本（默认 `3.11`） |
| `MSAGENT_NO_MODIFY_PATH` | 设为 `1` 时跳过 PATH 修改（由用户自行配置 PATH） |
| `MSAGENT_YES` | 设为 `1` 时免交互自动执行（CI/无人值守场景） |

> 提示：国内镜像同步有延迟，需最新版可设 `MSAGENT_INDEX=https://pypi.org/simple`；内网环境可将镜像地址替换为内网 PyPI。

### 3.2 pip 安装（备选）

仅建议在**隔离的虚拟环境**中使用。直接向现有环境执行 `pip install` 可能与已有依赖冲突（如 torch、`mindstudio_monitor`），且在 Ubuntu 22.04 及以上系统会受 PEP 668（externally-managed-environment）限制。

```shell
python3 -m venv ~/.msagent-venv
~/.msagent-venv/bin/pip install mindstudio-agent
```

或使用 uv 工具安装（推荐替代 pip）：

```shell
uv tool install -U --python 3.11 mindstudio-agent
```

> 注意：需 `Python >= 3.11`；pip 安装前请确认当前环境为隔离虚拟环境，避免污染现有环境。

### 3.3 源码安装

#### 3.3.1 环境准备

源码编译统一使用 MindStudio 标准构建环境。

- 日常开发或使用已发布镜像，请参考《[MindStudio工具开发环境安装指导](https://gitcode.com/Ascend/msot/blob/master/docs/zh/common/dev_env_setup.md)》。
- 需要从基础操作系统复现环境、执行源码构建验证或单元测试验证时，必须参考《[MindStudio统一构建镜像制作指南](https://gitcode.com/Ascend/msot/blob/master/docs/zh/common/docker_image_build_guide.md)》，从openEuler基础镜像现场构建环境镜像。

本文档后续的源码编译和单元测试命令，均在上述指定镜像容器或现场构建的环境镜像的容器中执行，CANN 软件包版本、GCC 版本和 Python 版本以统一镜像制作指南为准，本仓库不重复维护。

镜像构建完成后，必须使用统一镜像制作指南第 7 章给出的 `ctr_in.py` 命令，在交互式终端中启动并进入容器。不得使用普通 `docker run` 创建容器，也不得使用 `docker exec <容器名> bash -c '<命令>'` 替代交互式环境；否则可能跳过 Python、GCC 和 CANN 环境初始化。

#### 3.3.2 编译并安装

1. 进入 `ctr_in.py` 打开的交互式容器 Shell 后，执行如下命令克隆本仓库：

   ```bash
   cd ~
   git clone https://gitcode.com/Ascend/msagent.git
   ```

2. 保持在 `ctr_in.py` 打开的同一个交互式容器 Shell 中，在仓库根目录执行以下命令，自动完成依赖下载与构建：

   ```bash
   cd ~/msagent
   python3 build.py
   ```

3. 构建成功后，安装包将生成在 `artifacts/` 目录下。使用如下命令进行安装：

   ```shell
   pip install artifacts/mindstudio_agent-{version}-py3-none-any.whl
   ```

   安装完成后，若显示如下信息，则说明软件安装成功。

   ```ColdFusion
   Successfully installed mindstudio-agent-{version}
   ```

   默认构建会安装 `uv`。如本地已安装 `uv`，可使用 `python3 build.py local` 跳过安装。

#### 3.3.3 执行单元测试（可选）

此步骤非安装必需。如需验证代码基本功能，可在仓库根目录执行：

```shell
python3 build.py test
```

该命令会安装 `uv` 并运行 `tests/ut` 和 `tests/skills` 下的用例，`scripts/run_ut.sh` 中的 `uv run` 会自动准备测试依赖。如本地已安装 `uv`，可执行 `python3 build.py test local` 跳过安装。命令返回码为 0，且测试用例均无失败，表示单元测试通过。

## 4. 验证安装

安装完成后，执行以下命令验证工具是否可用：

```shell
msagent --version
msagent --help
```

命令能够输出版本信息和帮助信息，表示安装成功。若提示命令不存在，请确认已重新打开终端（或执行 `source ~/.bashrc` / 重启 shell），使安装器添加的 PATH 生效。

## 5. 升级与卸载

`msagent` 会在当前工作目录下生成 `.msagent/` 本地目录，用于保存缓存、会话历史、日志和运行时配置等内容。

- 升级前，先删除当前工作目录下的 `.msagent/` 文件夹，避免旧缓存影响新版本行为。
- 卸载时，如果后续不再使用 `msagent`，也建议一并删除 `.msagent/` 文件夹。

常见操作示例：

- 升级（推荐：重新执行一键安装命令，自动升级到最新版）

  ```shell
  curl -LsSf https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.sh | bash
  ```

- 升级（uv 工具方式）

  ```shell
  uv tool upgrade mindstudio-agent
  ```

- 卸载（uv 工具方式）

  ```shell
  rm -rf .msagent
  uv tool uninstall mindstudio-agent
  ```

- 卸载（pip 方式）

  ```shell
  rm -rf .msagent
  pip uninstall mindstudio-agent
  ```
