# 集成 msAgent Skills 与 MCP 服务

## 1. 概述

msAgent 提供一体化的昇腾领域调试调优能力，涵盖性能分析、精度调优、模型量化、算子优化、文档审查等核心开发场景。同时，msAgent 也提供两类可被外部 Agent 集成复用的资产：

- **Skill**：30+ 面向 Ascend 开发场景的领域知识包，覆盖性能分析、精度调优、模型量化、算子优化、文档审查等。每个 Skill 由 `SKILL.md`（执行流程）+ `scripts/`（辅助脚本）构成，agent 加载后自动按流程执行。
- **MCP 服务**：提供 `msprof-mcp` 服务，聚焦 Ascend profiling 数据分析领域。

适用人群：使用 `trae`、`claude`、`codex`、`opencode` 等 Agent，想接入 Ascend NPU 调试调优能力的开发者。

## 2. Skill：即装即用的领域知识包

msAgent 中的 Skills 实现遵循 Agent Skills 的通用约定，能在不同 agent 间复用、迁移。完整信息见 [Skill 列表](../../skills/README.md)。


### 2.1 方式一：npx skills（推荐）

适用于 Trae、opencode 等支持 `npx skills` 工作流的 agent。一行命令即可安装：

```bash
git clone https://gitcode.com/Ascend/msagent.git
cd msagent/skills

# 安装单个 Skill
npx skills add . --skill ascend-cluster-fast-slow-rank-detector -a trae -y

# 安装多个 Skill
npx skills add . --skill ascend-communication-analysis --skill ascend-computation-analysis -a opencode -y

# 安装全部 Skill
npx skills add . --all -a trae
```

### 2.2 方式二：手动拷贝

不依赖 `npx`，适用于任意 agent。克隆仓库后将目标 Skill 目录拷贝到 agent 的 skills 扫描路径下：

```bash
git clone https://gitcode.com/Ascend/msagent.git

# opencode
cp -r msagent/skills/ascend-profiler-db-explorer ~/.config/opencode/skills/

# claude
cp -r msagent/skills/ascend-profiler-db-explorer ~/.claude/skills/
```

安装后，在对话中输入匹配 Skill 描述的任务，agent 会自动读取 `SKILL.md` 并按照其中流程执行。

## 3. MCP：即插即用的工具底座

当前可用的 MCP 服务：

| 服务名称 | 领域 | 仓库                                                      |
|----------|------|---------------------------------------------------------|
| `msprof-mcp` | Ascend Profiling 数据分析 | [link](https://gitcode.com/kali20gakki1/msprof_mcp.git) |

### 3.1 msprof-mcp

专注于 Ascend profiling 数据分析领域，将 profiling 数据的多个数据载体（trace_view.json、CSV、SQLite DB）统一暴露为 MCP 工具。


#### 典型能力

| 维度 | 工具 | 数据载体 |
|------|------|----------|
| Timeline 分析 | `analyze_overlap`、`find_slices`、`get_flow_data`、`execute_sql_query` | trace_view.json |
| 算子分析 | `analyze_kernel_details`、`get_operator_details`、`analyze_op_statistic` | kernel_details.csv / op_statistic.csv |
| 通信分析 | `analyze_communication`、`analyze_communication_trace` | communication_matrix.json / communication.json |
| 配置查询 | `get_profiler_config` | profiler_info.json |
| 数据库查询 | `execute_sql`、`execute_sql_to_csv` | ascend_pytorch_profiler.db |

#### 快速接入

通过 stdio 传输协议运行，支持 `uvx` 一键启动或 `pip` 安装后直接运行：

```bash
# 方式一：uvx（推荐，无需显式安装）
uvx msprof-mcp

# 方式二：pip 安装
pip install msprof-mcp
msprof-mcp
```

在 agent 的 MCP 配置文件中添加：

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

环境要求：

- Python >= 3.11
- glibc >= 2.34（Perfetto TraceProcessor Shell 二进制依赖）

## 4. 案例：在 Trae 中完成一轮快慢卡分析

以下展示如何将 Skill + MCP 组合接入 Trae，完成一次 Ascend 集群快慢卡诊断。

### Step 1：安装 msAgent Skills

```bash
npx skills add . --skill ascend-cluster-fast-slow-rank-detector -a trae -y
```

### Step 2：安装 msprof-mcp

```bash
pip install msprof-mcp
```

### Step 3：配置 MCP Server

在项目根目录创建 `.trae/mcp.json`：

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

### Step 4：对话触发

重启 Trae 后，输入：

> 分析这个集群 profiling 目录里的快慢卡原因

agent 会自动：
1. 加载 `ascend-cluster-fast-slow-rank-detector` 的 `SKILL.md`
2. 按流程调用 `msprof-analyze advisor` 做全局诊断（通过 msprof-mcp）
3. 调用 `compare_op_stats.py` / `compare_api_stats.py` 做微观对比
4. 输出慢卡 Rank ID、瓶颈类型与判定依据

### Step 5：追问下钻

在已有结论基础上继续追问：

> Host 下发有没有瓶颈？

agent 基于已有证据调用 `compare_api_stats.py` 聚焦下发 API 维度，无需重新跑全部分析。
