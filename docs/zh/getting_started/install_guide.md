# msAgent安装指南

## 1. 安装说明

本文面向第一次使用 MindStudio-Agent 的用户，帮助您完成 msAgent 安装。

当前支持两种安装方式：[在线安装](#31-在线安装)、[源码安装](#32-源码安装)。

## 2. 环境要求

- `Python >= 3.11`

- `glibc >= 2.34`

  用于满足 `msprof-mcp` 中 `trace_processor` 二进制依赖（建议操作系统：`Ubuntu >= 21.10`、`openEuler >= 21.09`，其他操作系统请自行查询）

- 使用本工具前需要安装CANN，具体操作请参见《[CANN 快速安装](https://www.hiascend.com/cann/download)》安装昇腾NPU驱动和CANN软件（包含Toolkit和ops包），并配置环境变量。

## 3. 安装方式

### 3.1 在线安装

要求设备具备互联网访问能力，可通过如下命令完成工具的下载与安装。

```shell
pip install mindstudio-agent
```

执行如下命令提示msAgent版本即安装成功。

```shell
msagent --version
```

### 3.2 源码安装

#### 3.2.1 环境准备

源码编译统一使用 MindStudio 标准构建环境。

- 日常开发或使用已发布镜像，请参考《[MindStudio工具开发环境安装指导](https://gitcode.com/Ascend/msot/blob/master/docs/zh/common/dev_env_setup.md)》。
- 需要从基础操作系统复现环境、执行源码构建验证或单元测试验证时，必须参考《[MindStudio统一构建镜像制作指南](https://gitcode.com/Ascend/msot/blob/master/docs/zh/common/docker_image_build_guide.md)》，从openEuler基础镜像现场构建环境镜像。

本文档后续的源码编译和单元测试命令，均在上述指定镜像容器或现场构建的环境镜像的容器中执行，CANN 软件包版本、GCC 版本和 Python 版本以统一镜像制作指南为准，本仓库不重复维护。

镜像构建完成后，必须使用统一镜像制作指南第 7 章给出的 `ctr_in.py` 命令，在交互式终端中启动并进入容器。不得使用普通 `docker run` 创建容器，也不得使用 `docker exec <容器名> bash -c '<命令>'` 替代交互式环境；否则可能跳过 Python、GCC 和 CANN 环境初始化。

#### 3.2.2 编译并安装

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

#### 3.2.3 执行单元测试（可选）

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

命令能够输出版本信息和帮助信息，表示安装成功。若提示命令不存在，请确认当前终端使用的是安装 `mindstudio-agent` 的 Python 环境。

## 5. 升级与卸载

`msagent` 会在当前工作目录下生成 `.msagent/` 本地目录，用于保存缓存、会话历史、日志和运行时配置等内容。

- 升级前，先删除当前工作目录下的 `.msagent/` 文件夹，避免旧缓存影响新版本行为。
- 卸载时，如果后续不再使用 `msagent`，也建议一并删除 `.msagent/` 文件夹。

常见操作示例：

- 升级

  ```shell
  rm -rf .msagent
  pip install mindstudio-agent
  ```

  从 **26.1.0-alpha.2** 起，Web UI 依赖 `langgraph-cli[inmem]` 已改为可选 extra `[web]`。`pip install -U` 升级时**不会自动卸载**旧版已安装的 web 相关包；若不再使用 Web UI，可手动执行：

  ```shell
  pip uninstall -y langgraph-cli langgraph-api langgraph-runtime-inmem
  ```

- 卸载

  ```shell
  rm -rf .msagent
  pip uninstall mindstudio-agent
  ```
