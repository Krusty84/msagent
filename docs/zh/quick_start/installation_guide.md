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

## Web UI 运行时

Web UI 仍处于 Beta 阶段。通过 wheel 安装后，运行时需要本机已安装 `node`；源码运行时同样可以使用 `uv run msagent web`。

```bash
msagent web
```

默认地址：

```text
UI:  http://127.0.0.1:3000
API: http://127.0.0.1:2024
```

常用参数：

```bash
msagent web --host 127.0.0.1 --port 2024 --ui-port 3000
msagent web --port 2025 --ui-port 3001
msagent web --no-open
msagent web --no-ui
```

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

完成安装后，继续阅读 [快速入门指导](op_tool_quick_start.md)，配置 LLM 并启动第一个 Agent 会话。
