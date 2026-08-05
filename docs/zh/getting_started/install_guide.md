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

1. 克隆本仓库。

   ```shell
   git clone https://gitcode.com/Ascend/msagent.git
   ```

2. 执行编译打包。

   ```shell
   cd msagent
   bash scripts/build_whl.sh
   ```

   适用场景：

   - Linux / macOS
   - Windows + Git Bash
   - Windows + WSL

   编译完成后在`dist`目录下生成 whl 包，名称格式为`mindstudio_agent-{version}-py3-none-any`。其中`version`为版本号。

3. 安装whl包。

   ```shell
   pip install dist/mindstudio_agent-{version}-py3-none-any.whl
   ```

   安装完成后，若显示如下信息，则说明软件安装成功。

   ```ColdFusion
   Successfully installed mindstudio-agent-{version}
   ```

## 4. 升级与卸载

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

- 卸载：

    ```shell
    rm -rf .msagent
    pip uninstall mindstudio-agent
    ```
