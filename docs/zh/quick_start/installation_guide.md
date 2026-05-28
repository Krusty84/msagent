# 入门安装指南

本文面向第一次使用 MindStudio-Agent 的用户，帮助你完成基础环境准备、安装和最小启动验证。

## 环境要求

- Python `3.11+`
- 推荐使用 `uv` 管理源码运行环境
- 至少准备一个可用的 LLM API Key
- glibc `>= 2.34`，用于满足 `msprof-mcp` 中 `trace_processor` 二进制依赖

## PyPI 安装

普通用户建议优先使用 PyPI 安装稳定发布版本：

```bash
pip install -U mindstudio-agent
msagent --version
msagent
```

## 源码运行

如果你需要跟踪最新源码、参与开发，或同步最新内置 Skills，可以使用源码运行方式：

```bash
git clone https://gitcode.com/Ascend/msagent.git
cd msagent
uv sync
uv run msagent --version
uv run msagent
```

源码运行时，后续示例中的 `msagent` 可以替换为 `uv run msagent`。

## 日志与版本检查

检查版本：

```bash
msagent --version
```

开启详细日志：

```bash
msagent -v
```

启用后，日志会写入当前工作目录下的 `.msagent/logs/app.log`。

通过 `MSAGENT_LOG_LEVEL` 环境变量可调整日志详细程度，默认值为 `INFO`：

```bash
export MSAGENT_LOG_LEVEL=DEBUG
msagent -v
```

支持的级别从低到高依次为：`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`。

## 下一步

🔹 **构建与打包**：需要生成 wheel 或检查构建产物时，请参见 [《编译与打包》](docs/zh/developer_guide/build-and-package.md)。  
🔹 **版本与兼容性**：Python 版本、Provider 支持和内置 MCP 版本说明，请参见 [《版本与兼容性》](docs/zh/developer_guide/version-and-compatibility.md)。  
🔹 **完整安装说明**：日志与版本检查等更多内容，请参见 [《入门安装指南》](docs/zh/quick_start/installation_guide.md)。