# Skill 开发部署排错

本文面向希望新增、调试或部署 `msagent` Skill 的开发者，提供最小开发流程和排错清单。完整 Skill 规范与清单见仓库根目录 `skills/README.md`，加载顺序和本地配置说明见 [配置与扩展](../user_guide/configuration-and-extension.md)。

## 最小开发流程

1. 在 `skills/` 下创建 kebab-case 目录，并提供 `SKILL.md`。
2. 在 `SKILL.md` frontmatter 中写清 `name` 和 `description`。
3. 如有确定性逻辑，将脚本放入 `scripts/`；较长背景资料放入 `references/`。
4. 手动放置 Skill 时，在目标 Agent YAML 的 `skills.patterns` 中放开该 Skill。
5. 启动 `msagent` 后通过 `/skills` 验证可见性。

最小目录结构：

```text
skills/
  my-skill/
    SKILL.md
```

最小 `SKILL.md`：

```md
---
name: my-skill
description: 用于处理某类固定任务的自定义 skill
---

# My Skill

当用户提出这类需求时使用这个 skill。
```

## 部署入口

常见部署位置如下：

| 场景 | 入口 | 说明 |
|---|---|---|
| 源码开发 | `skills/` | 仓库根目录下的内置 Skill 来源。 |
| 项目本地扩展 | `<working-dir>/skills` | 面向当前工作目录的 Skill。 |
| 运行时安装 | `.msagent/skills` | 通过 `/add-skill` 等方式安装到本地配置目录。 |

完整加载优先级、同名覆盖规则和源码 / wheel 差异见 [配置与扩展](../user_guide/configuration-and-extension.md)。

在交互式会话中安装单个本地 Skill：

```text
/add-skill /path/to/my-skill
```

该命令接受 Skill 目录或具体的 `SKILL.md` 路径，会校验 frontmatter，将文件复制到 `.msagent/skills`，并为当前 Agent 添加对应的 `skills.patterns`。安装成功后会话会自动重新加载，再使用 `/skills` 检查。

## 让 Agent 能看到 Skill

手动将 Skill 放入扫描目录时，目标 Agent 还需要在配置中允许它；使用 `/add-skill` 安装时会自动完成这一步：

```yaml
skills:
  patterns:
    - default:my-skill
  use_catalog: false
```

带分类目录时可使用：

```yaml
skills:
  patterns:
    - profiling:my-skill
  use_catalog: false
```

`skills.patterns` 的完整匹配规则见 [Agent / Tool / Skill 过滤规则](../user_guide/agent-tool-skill-filter-rules.md)。

## 本地验证

源码开发时先确认命令可用：

```bash
uv sync --dev
uv run msagent --version
```

进入会话后检查 Skill：

```text
/skills
/skills my-skill
/skills profiling/my-skill
```

## 常见排错

| 现象 | 优先检查 |
|---|---|
| `/skills` 看不到新 Skill | 路径是否在扫描范围内；文件名是否为 `SKILL.md`；目标 Agent 是否放开了 `skills.patterns`；是否被同名 Skill 覆盖。 |
| `/add-skill` 安装失败 | `name` 和 `description` 是否非空；名称是否只包含字母、数字、连字符或下划线；目标目录是否已存在；是否被更高优先级的同名 Skill 遮蔽。 |
| Skill 可见但不会自动使用 | `description` 是否过泛；任务是否匹配目标 Agent 领域；用户输入是否缺少数据路径或触发上下文。 |
| 修改 Agent 配置后不生效 | 检查当前 Agent 对应的 `.msagent/agents/<Agent>.yml` 和 `skills.patterns`，然后结束并重新启动会话。不要为此删除整个 `.msagent/`，其中还包含历史、记忆和自定义配置。 |
| 脚本或外部依赖不可用 | 在 `SKILL.md` 写清前置依赖；脚本放入 `scripts/`；示例路径使用占位符并说明需要替换。 |

## 相关文档

- [配置与扩展](../user_guide/configuration-and-extension.md)
- [Agent / Tool / Skill 过滤规则](../user_guide/agent-tool-skill-filter-rules.md)
- [添加自定义 Agent](add-agent.md)
- `skills/README.md`
