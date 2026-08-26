# FAQ

## 1. msagent 第一次启动时会生成哪些本地文件？

`msagent` 使用项目本地配置。首次在某个工作目录启动时，会自动生成：

```text
.msagent/
```

这个目录里通常会包含：

- LLM 配置
- Agent / SubAgent 配置
- MCP 配置
- Prompt 模板
- Skills
- 日志、缓存、会话历史和 checkpoint 数据

更完整的目录说明见 [配置与扩展](configuration-and-extension.md)。

## 2. 如何打开或关闭 MCP 服务？

有两种常见方式：

- 在会话里通过 `/mcp` 查看和切换已配置的 MCP 服务
- 直接编辑 `.msagent/config.mcp.json`

默认模板会启用 `msprof-mcp`。如果你要接入新的本地或远程 MCP 服务，建议先参考 [配置与扩展](configuration-and-extension.md) 里的字段说明。

## 3. 如何确认 Skill 是否被识别到了？

进入会话后，可以执行：

```text
/skills
```

如果要查看某个具体 Skill：

```text
/skills my-skill
```

如果你的 Skill 带分类目录，建议写全路径：

```text
/skills profiler/my-skill
```

如果看不到，通常优先检查：

- 路径是否正确
- 文件名是否为 `SKILL.md`
- 当前 Agent 的 `skills.patterns` 是否允许该 Skill
- 是否被更高优先级目录中的同名 Skill 覆盖

加载自定义 Skill，可参考[添加自定义 Skill](configuration-and-extension.md#添加自定义-skill)

## 4. 运行日志在哪里看？

启用后，日志会写到当前工作目录下：

```text
.msagent/logs/app.log
```

如果你想看到更详细的日志，可以开启：

```bash
export MSAGENT_LOG_LEVEL=DEBUG
msagent -v
```

## 5. 终端界面颜色偏灰/看不清怎么办？

msagent 会自动检测终端主题（深色/浅色）来匹配配色方案。如果检测不准（比如 SSH 连接时误判为浅色主题，导致深色终端上文字偏灰），可通过环境变量手动指定：

```bash
export MSAGENT_BACKGROUND_THEME=dark
```

可选值：

| 值 | 含义                               |                                     |
|----|----------------------------------|-------------------------------------|
| `dark` | 强制使用背景为深色主题（Tokyo Night），对应字体为浅色 | <img src="../figures/theme_dark.png" width="400">  |
| `light` | 强制使用背景为浅色主题（Tokyo Day），对应字体为深色   |  <img src="../figures/theme_light.png" width="400">                                   |

## 6. MobaXterm 终端背景为白色/无彩色怎么办？

MobaXterm 默认未设置 `COLORTERM`，导致 prompt_toolkit 无法识别真彩色支持，界面可能显示为白色背景或无彩色。设置以下环境变量即可恢复：

```bash
export COLORTERM=truecolor
```

## 7. pip 安装 mindstudio-agent 时提示依赖冲突或安装失败怎么办？

直接 `pip install mindstudio-agent` 会写入当前 Python 环境，容易与已有依赖（如 torch、mindstudio_monitor 等）冲突，或在 Ubuntu 22.04+ 上遇到 PEP 668（externally-managed-environment）限制。

推荐改用一键安装（隔离环境，不影响现有环境）：

```shell
# Linux / macOS / WSL
curl -LsSf https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.sh | bash

# Windows（PowerShell 5.1+）
irm https://raw.gitcode.com/Ascend/msagent/raw/master/scripts/install.ps1 | iex
```

常见报错对照：

| 报错现象 | 原因 | 处理方法 |
| --- | --- | --- |
| `externally-managed-environment` | 系统 Python 受 PEP 668 保护 | 使用一键安装，或先创建虚拟环境再 pip 安装 |
| `Requires-Python >=3.11` 或版本过低 | 当前 Python 版本低于 3.11 | 一键安装会自动下载 Python 3.11，无需手动升级 |
| 依赖冲突（如 pydantic、langchain 版本不匹配） | 与现有环境包版本冲突 | 使用一键安装（uv 独立工具环境） |
| 安装超时或下载失败 | 网络访问 PyPI 不稳定 | 一键安装默认使用国内镜像；或设置 `MSAGENT_INDEX` 指定镜像 |

## 8. 如何升级或卸载 msagent？

- 升级：重新执行一键安装命令，安装器会自动升级到最新版本（`uv tool upgrade mindstudio-agent` 亦可）；
- 卸载：`uv tool uninstall mindstudio-agent`，并删除当前工作目录下的 `.msagent/` 文件夹。
