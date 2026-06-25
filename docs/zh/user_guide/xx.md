# Trae 快速上手

如果你使用 [Trae](https://www.trae.ai/)，无需安装完整的 `msagent`，也可以直接在本仓库中安装 Skills，并接入 `msprof-mcp` 来分析 Ascend Profiling 数据。

### 1. 安装 Skills

克隆仓库后进入 `skills` 目录，先查看可用技能列表，再一键安装到 Trae：

```bash
git clone https://gitcode.com/Ascend/msagent.git
cd msagent/skills

# 查看当前仓库包含哪些 skill
npx skills add . --list

# 安装全部 skill 到 Trae
npx skills add . --skill "*" -a trae -y
```

如果只想安装部分 skill，可将 `"*"` 替换为具体名称，例如 `ascend-cluster-fast-slow-rank-detector`。

### 2. 安装 MCP 依赖

Profiling 分析相关的 skill 会调用 `msprof-mcp` 工具，需提前安装：

```bash
pip install msprof-mcp
```

环境要求：

- `Python >= 3.11`
- `glibc >= 2.34`（`msprof-mcp` 中 `trace_processor` 二进制依赖）

### 3. 配置 MCP Server

在项目根目录创建 `.trae/mcp.json`（也可通过 Trae → 设置 → MCP 手动添加），写入：

```json
{
  "mcpServers": {
    "msprof-mcp": {
      "command": "msprof-mcp",
      "args": []
    }
  }
}
```

配置完成后重启 Trae，即可在对话中直接使用 Profiling 分析 skill 与 `msprof-mcp` 工具。示例触发语：

- “帮我检查这个 MindStudio profiler 数据是否完整可分析”
- “分析这个 Ascend 集群 profiling 目录里的快慢卡问题”
- “查一下这个 `msprof_*.db` 里最耗时的 TopK 算子和通信耗时”
