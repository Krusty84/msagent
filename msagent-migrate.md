# msAgent 配置目录兼容检测设计

## 1. 背景

旧版本以工作目录为边界，在每个 workspace 下创建一套 `.msagent/`：

```text
<workspace>/.msagent/
├── config.llms.yml
├── config.mcp.json
├── config.approval.json
├── agents/
├── subagents/
├── prompts/
├── skills/
├── memory.md
├── .history
├── config.checkpoints.db
├── conversation_history/
└── audit_log/
```

新版本将安装包默认值、用户全局配置和 workspace 状态分离：

```text
resources/configs/default/                  # wheel 内只读默认值

~/.msagent/
├── metadata.json                           # 全局存储布局元数据
├── config/                                 # 全局用户覆盖配置
├── prompts/                                # 全局用户 Prompt
├── skills/                                 # 全局用户 Skill
├── cache/
├── oauth/
├── logs/
└── state/projects/<project-id>/            # workspace 独立状态
    ├── project.json
    ├── memory.md
    ├── history
    ├── checkpoints.sqlite
    ├── conversation_history/
    └── audit_log/
```

其中 `<project-id>` 由 workspace 的 canonical path 生成。工作目录仅作为 Agent、shell 和文件工具的执行根目录，新版本不再创建 `<workspace>/.msagent/`。

## 2. 本次方案

本次不实现旧配置或旧状态的自动迁移，采用以下兼容策略：

1. 用户仍通过 `pip install -U msagent` 升级 Python 包。
2. 新版 msAgent 启动时，在写入任何用户文件之前检查全局 msAgent home。
3. 如果检测到旧版全局布局，停止启动并列出检测依据。
4. 提示用户备份并删除或重命名旧目录，然后重新运行 msAgent。
5. 程序不自动删除、移动或覆盖旧目录。
6. 本次不检测、不处理普通 workspace 下残留的 `.msagent/`。

该方案的目标是避免新版静默忽略旧全局配置，同时保持实现简单、行为明确。用户选择删除旧目录即表示接受旧配置和旧状态不再保留；因此提示中必须优先建议备份或重命名。

## 3. 为什么不能在 pip 安装阶段处理

wheel/PEP 517 没有可靠的 post-install hook。`pip install -U` 只负责替换 Python 环境中的包文件，不应假设安装进程有权访问或修改用户的 home、workspace、容器挂载目录或远程文件系统。

因此检测发生在新版 msAgent 第一次运行时，而不是安装时：

```text
pip install -U msagent
        ↓
用户执行 msagent
        ↓
启动前只读检测全局目录
        ↓
旧布局：停止并提示用户处理
新版或空目录：正常启动
```

## 4. 检测路径

检测路径不能写死为 `~/.msagent`，必须使用实际解析结果：

```python
AppPaths.resolve().home
```

默认值为 `~/.msagent`。如果用户设置了：

```bash
MSAGENT_HOME=/custom/path
```

则检测 `/custom/path`。所有错误提示必须展示解析后的绝对路径。

## 5. 存储布局版本

新版在全局目录中保存：

```text
~/.msagent/metadata.json
```

初始内容保持最小：

```json
{
  "storage_layout_version": 2
}
```

采用 `storage_layout_version`，而不是含义模糊的 `version` 或范围过窄的 `config_layout_version`，因为该版本同时描述 config、state、skills、cache、oauth 和 logs 的存储位置。

该字段与以下版本相互独立：

- 应用版本：当前安装的 msAgent 版本；
- 配置 Schema 版本：Agent、LLM 等单个配置文件的字段格式；
- 存储布局版本：全局配置和 workspace 状态在文件系统中的组织方式。

`metadata.json` 的主要作用：

- 明确目录已经完成新版布局初始化；
- 避免以后继续依赖目录内容猜测布局；
- 支持未来按 `2 → 3` 的方式升级存储布局；
- 防止旧版程序写入由更高布局版本创建的目录。

本次旧版检测不依赖该文件才能工作。由于已经存在的新版目录可能没有 `metadata.json`，缺少该文件不能直接判定为旧版。

## 6. 旧版特征

不能因为全局目录存在就认定它是旧版，因为新版同样使用该目录，用户也可能提前创建空目录。

以下顶层内容是旧版的强特征：

```text
config.llms.yml
config.mcp.json
config.approval.json
config.agents.yml
config.subagents.yml
config.checkpointers.yml
config.checkpoints.db
langgraph.json
.history
memory.md
agents/
subagents/
llms/
checkpointers/
sandboxes/
```

只要存在其中任意一项，就认为目录包含旧版布局。

以下顶层目录不能单独作为旧版依据，因为新版也使用：

```text
skills/
prompts/
cache/
oauth/
logs/
```

正常的旧版完整目录通常还包含 `config.llms.yml`、`agents/` 等强特征，因此能够被检测。如果目录中只剩 `skills/` 或 `prompts/`，无法可靠判断它是旧副本还是用户主动创建的新版全局内容，本次按新版用户内容处理。

## 7. 新版特征

以下内容表示目录已经采用或开始采用新版布局：

```text
metadata.json，且 storage_layout_version == 2
config/
state/projects/
```

`skills/`、`prompts/`、`cache/`、`oauth/` 和 `logs/` 不能单独证明是新版布局。

## 8. 判定规则

### 8.1 全局目录不存在

正常启动：初始化新版目录，原子写入 `metadata.json`，然后继续启动。

### 8.2 全局目录存在，但没有旧版特征

如果 `metadata.json` 不存在：

1. 检查路径类型和写权限；
2. 初始化缺失的新版目录；
3. 原子写入 `storage_layout_version = 2`；
4. 继续启动。

该情况覆盖空目录、仅包含新版公共目录，以及当前版本已经创建但尚未带版本文件的新版目录。

### 8.3 `storage_layout_version == 2`

按当前新版布局继续启动。如果同时出现旧版强特征，则判定为混合布局，不能静默继续，也不能笼统提示删除整个目录。

### 8.4 只有旧版特征

停止启动，返回非零退出码。程序不得在该目录下创建 `config/`、`state/`、`logs/` 或 `metadata.json`。

提示用户检测到的绝对路径、命中的旧版特征，以及新版本不会读取这些旧配置；建议先备份或重命名，删除或移走旧目录后重新运行。

### 8.5 新旧内容混合

例如：

```text
~/.msagent/config/                  # 新版
~/.msagent/state/projects/         # 新版
~/.msagent/config.llms.yml         # 旧版
```

此时删除整个目录可能同时删除已经生成的新版状态。程序应停止启动并明确提示“检测到混合布局”，分别列出新版和旧版证据。

本次不自动整理混合布局。用户需要先备份目录，再根据实际需要清理旧文件或重建整个目录。

### 8.6 `storage_layout_version > 2`

当前程序不支持更高版本布局，应停止启动并提示用户升级 msAgent。不得降级或覆盖 `metadata.json`。

### 8.7 `storage_layout_version < 2`

表示该目录使用更早的、当前版本不支持的已标记布局。由于本次不提供自动迁移，应停止启动，提示用户备份并移走旧目录后重新运行。不得直接把版本号改写为 2，因为版本号变化不等于实际目录结构已经完成转换。

### 8.8 元数据损坏或字段非法

以下情况均停止启动：

- `metadata.json` 不是合法 JSON；
- 根节点不是对象；
- `storage_layout_version` 缺失；
- 版本不是正整数。

程序不得自动覆盖损坏文件，错误信息应包含文件路径和修复建议。

## 9. 启动时机

检测必须发生在任何新版文件写入之前。建议启动顺序：

```text
处理 --help / --version
        ↓
解析 CLI 参数
        ↓
解析 AppPaths 和 MSAGENT_HOME
        ↓
只读检测全局存储布局
        ↓
检测通过后初始化目录和 metadata.json
        ↓
初始化日志
        ↓
加载配置并启动 Agent
```

当前日志初始化可能创建 `~/.msagent/logs/`，因此检测必须位于日志初始化之前，否则检测过程会先修改待检查目录。

以下命令不应被旧布局检测阻塞：

```text
msagent --help
msagent -h
msagent --version
msagent -V
msagent config --help
```

它们不需要加载配置或写入运行状态，可以帮助用户查看命令和版本。

## 10. 用户提示

### 10.1 旧版布局

建议提示：

```text
检测到旧版 msAgent 配置目录：

  /home/user/.msagent

检测到以下旧版内容：

  config.llms.yml
  config.mcp.json
  agents/
  memory.md

当前版本使用新的全局配置和 workspace 状态布局。旧目录中的配置和状态
不会被当前版本读取。为避免静默使用默认配置，本次启动未修改任何文件。

请先备份或重命名该目录，然后删除或移走旧目录并重新运行 msAgent。
推荐先执行：

  mv /home/user/.msagent /home/user/.msagent.backup

确认新版运行正常后，再自行清理备份。
```

提示中不直接执行删除命令，也不自动确认用户是否愿意丢弃旧数据。

### 10.2 混合布局

建议提示：

```text
检测到新旧 msAgent 存储布局混合存在：

  /home/user/.msagent

新版内容：config/, state/projects/
旧版内容：config.llms.yml, agents/

删除整个目录可能丢失新版项目状态。本次启动未修改任何文件。
请先备份该目录，再手动整理或重建，并重新运行 msAgent。
```

## 11. `<workspace>/.msagent` 残留

本次不扫描、不阻止普通 workspace 下残留的 `.msagent/`。标准新版 CLI 的配置和项目状态均使用全局目录，因此旧 workspace 目录：

- 不会覆盖新版全局配置；
- 不会影响新版 memory、history 和 checkpoint 的写入；
- 不会导致标准 CLI 启动失败；
- 可以继续保留。

但需要在发布说明中明确：

- 旧 workspace `.msagent` 中的配置、Skill 和历史状态不会被新版自动读取；
- 用户继续编辑其中的配置不会生效；
- 旧目录仍占用磁盘空间；
- 如果 workspace 本身是 `~`，则 `<workspace>/.msagent` 与全局 `~/.msagent` 是同一路径，会被本方案检测。

部分绕过标准 CLI、直接调用内部低层 API 的外部集成仍可能触发兼容 fallback，本结论只保证标准 CLI 路径。

## 12. Skills 边界

旧 `~/.msagent/skills/` 与新版全局 Skills 使用同一路径。如果全局目录还包含其他旧版强特征，启动检测会阻止运行，用户移走整个旧目录后不会出现旧内置 Skill 遮盖新版 wheel Skill的问题。

如果 `~/.msagent` 中只剩 `skills/`，本次无法可靠判断其中是旧内置副本还是用户主动安装的全局 Skill，因此不会阻止启动。加载器仍按当前优先级处理它。这是本次简化方案明确接受的限制，不进行哈希识别、自动去重或自动删除。

## 13. 元数据写入约束

- `metadata.json` 使用 UTF-8 JSON。
- 先写同目录临时文件，再通过原子 replace 写入目标。
- 全局目录建议权限为 `0700`，元数据文件建议权限为 `0600`。
- 不跟随指向全局目录之外的符号链接。
- 写入前再次确认未出现旧版强特征，避免检测和初始化之间的竞争条件。
- 两个进程同时首次启动时，最终必须得到相同的 `storage_layout_version = 2`，不得产生部分 JSON。

## 14. 验收标准

- 全新安装可以正常初始化新版目录并写入 `metadata.json`。
- 设置 `MSAGENT_HOME` 时检测实际配置的目录。
- 旧版强特征存在时，启动失败且用户目录保持不变。
- 错误提示列出实际命中的旧版文件或目录。
- 仅存在 `skills/`、`prompts/`、`cache/`、`oauth/` 或 `logs/` 时不会误判为旧版。
- 已有新版目录但没有 `metadata.json` 时能够补写版本信息。
- 新旧布局混合时不会建议用户直接删除整个目录。
- 更高布局版本和损坏的 `metadata.json` 不会被当前程序覆盖。
- 检测发生在日志和项目状态目录创建之前。
- `--help` 和 `--version` 在旧布局存在时仍可执行。
- 普通 workspace 下残留 `.msagent` 不影响标准 CLI 启动。
- 所有拒绝启动场景均返回非零退出码。
