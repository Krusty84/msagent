# msagent whl 自动化验证工程

## 1. 工程目标

本工程用于验证指定路径的 `mindstudio-agent` whl 是否满足对外发布要求。

验证分为两个阶段：

1. 安装门禁：在干净 Conda 环境中安装指定 whl，并检查三方依赖一致性。
2. 功能验证：通过真实 `msagent` CLI 下发指令，主要依据结构化
   `trace.jsonl` 和 DEBUG `app.log` 判断功能是否正常。

当前 `run_validation.sh` 只实现安装门禁的三个步骤：创建 Conda 环境、
安装指定 whl、执行 `pip check`。安装测试依赖和启动 pytest 尚未接入脚本。

## 2. 当前工程结构

```text
msagent-whl-validator/
├── config/
│   └── test_config.yaml          # LLM、msagent 和产物保留配置
├── scripts/
│   └── run_validation.sh         # Conda 创建、whl 安装、pip check
├── validator_core/
│   ├── agent_runner.py           # 执行单次 msagent 命令并采集产物
│   ├── artifacts.py              # pytest 用例产物目录命名
│   ├── assertions.py             # 通用会话和日志断言
│   ├── config.py                 # YAML 配置解析和类型校验
│   ├── llm_capture_server.py     # 本地 OpenAI 兼容捕获/脚本化 Mock 服务
│   ├── llm_payload_parser.py     # System Prompt 和 tools payload 解析
│   ├── msagent_runtime.py        # 用例级 workspace/home/runtime 管理
│   └── trace_parser.py           # trace 事件结构化提取
├── testdata/
│   └── workspace_seed/
│       ├── read_marker.txt       # Filesystem 测试固定输入
│       └── print_marker.sh       # LocalShell 测试固定脚本
├── tests/
│   ├── conftest.py               # pytest fixture 和失败产物生命周期
│   ├── test_01_llm_conn.py       # 真实 LLM 连通性和会话状态
│   ├── test_02_mcp_tools.py      # msprof-mcp 发现及 trace_view 工具执行
│   ├── test_03_skills.py         # Skill 路由、读取和动态加载占位
│   ├── test_04_sys_prompt.py     # Agent System Prompt 和环境信息
│   └── test_05_local_env.py      # Filesystem、LocalShell 和关键 CLI
├── artifacts/                    # 安装日志及失败用例诊断产物
├── pytest.ini
├── requirements-test.txt
└── README.md
```

## 3. 环境要求

安装门禁需要：

- Linux 或 WSL Bash
- Conda
- 能够访问 whl 依赖所在的 Python 包索引
- 待验证的 `mindstudio-agent` whl

当前包要求 Python `>=3.11`，脚本默认创建 Python 3.11 环境。

功能测试另外需要：

- `requirements-test.txt` 中的 pytest 和 PyYAML
- 真实模型用例所需的 API Key
- whl 运行时功能所需的外部程序或服务

## 4. 安装门禁

### 4.1 基本用法

```bash
cd msagent-whl-validator

./scripts/run_validation.sh \
  --wheel /absolute/path/to/mindstudio_agent-<version>-py3-none-any.whl
```

脚本按顺序执行：

```text
conda create
    ↓
python -m pip install <given-whl>
    ↓
python -m pip check
```

任意步骤返回非零退出码时脚本立即失败，并保留已经生成的日志。

### 4.2 可选参数

```text
--wheel PATH              必填，待验证 whl 的路径
--python-version VERSION  Conda Python 版本，默认 3.11
--conda PATH              Conda 可执行程序，默认使用 $CONDA_EXE 或 conda
--run-dir PATH            指定一个不存在或为空的运行目录
```

查看帮助：

```bash
./scripts/run_validation.sh --help
```

建议流水线显式指定运行目录，便于继续使用创建好的环境：

```bash
RUN_DIR="$PWD/artifacts/validation-001"

./scripts/run_validation.sh \
  --wheel /absolute/path/to/mindstudio_agent.whl \
  --run-dir "$RUN_DIR"
```

安装成功后，目标 Python 和 CLI 位于：

```text
$RUN_DIR/conda-env/bin/python
$RUN_DIR/conda-env/bin/msagent
```

### 4.3 安装产物

```text
artifacts/install-<UTC时间>-<进程号>/
├── conda-env/
├── conda-create.log
├── pip-install.log
└── pip-check.log
```

安装环境当前不会自动删除，便于继续运行功能测试和定位依赖问题。

## 5. 配置说明

运行配置位于 `config/test_config.yaml`。

### 5.1 LLM 配置

```yaml
llm:
  model: deepseek-v4-flash
  api_key_env: DEEPSEEK_API_KEY
  protocols:
    openai:
      base_url: https://api.deepseek.com
      base_url_env: OPENAI_BASE_URL
      provider_api_key_env: OPENAI_API_KEY
    anthropic:
      base_url: https://api.deepseek.com/anthropic
      base_url_env: ANTHROPIC_BASE_URL
      provider_api_key_env: ANTHROPIC_API_KEY
```

模型和 Base URL 固定从 YAML 读取。用户或流水线只需要提供：

```bash
export DEEPSEEK_API_KEY='<real-api-key>'
```

fixture 会根据协议把该 Key 映射到 `OPENAI_API_KEY` 或
`ANTHROPIC_API_KEY`，并注入相应的 Base URL 环境变量。

API Key 不会写入 trace、`command.json` 或本地 LLM 请求捕获文件。

### 5.2 msagent 配置

```yaml
msagent:
  executable: msagent
  timeout_seconds: 180
```

pytest 使用当前 Python 环境同级的 `msagent` 命令，避免误调用系统中其他
版本的 CLI。

### 5.3 产物保留配置

```yaml
artifacts:
  root_dir: artifacts
  retention: failed
  retain_workspace_on_failure: true
```

`retention` 支持：

- `failed`：默认，只保留失败用例产物。
- `all`：保留所有用例产物。
- `none`：测试结束后清理所有用例产物。

安装阶段的 Conda 环境和安装日志不受 pytest 的 retention 策略影响。

## 6. 功能测试运行方式

`run_validation.sh` 当前尚未自动安装测试依赖或启动 pytest。可以在安装
门禁成功后手工接续：

```bash
RUN_DIR="$PWD/artifacts/validation-001"
ENV_PYTHON="$RUN_DIR/conda-env/bin/python"

"$ENV_PYTHON" -m pip install -r requirements-test.txt
"$ENV_PYTHON" -m pip check

export DEEPSEEK_API_KEY='<real-api-key>'
export MSAGENT_VALIDATION_RUN_DIR="$RUN_DIR/pytest"

"$ENV_PYTHON" -m pytest tests
```

常用选择方式：

```bash
# 只验证真实 LLM 连通性
"$ENV_PYTHON" -m pytest tests/test_01_llm_conn.py

# MCP 使用 Mock LLM，但真实启动 msprof-mcp 和 Perfetto
"$ENV_PYTHON" -m pytest tests/test_02_mcp_tools.py

# 只验证 Skill
"$ENV_PYTHON" -m pytest tests/test_03_skills.py

# System Prompt 使用本地捕获服务，不访问真实 LLM
"$ENV_PYTHON" -m pytest tests/test_04_sys_prompt.py

# 运行所有真实模型用例
"$ENV_PYTHON" -m pytest -m llm
```

不建议直接使用系统环境中的 `pytest`。正式验证必须使用安装了目标 whl 的
Conda 环境执行。

## 7. 测试用例说明

### 7.1 `test_01_llm_conn.py`

对 OpenAI 和 Anthropic 两种协议分别发送：

```text
Ping. Please reply 'Pong' only.
```

主要断言：

- trace 中存在非空 `assistant_message`。
- stdout 包含 `Pong`。
- msagent 进程返回码为 0。
- trace 不存在 `error` 事件。
- `session_finished.exit_code == 0`。
- 本次 `app.log` 不包含致命 Traceback 或 Exception。

### 7.2 `test_02_mcp_tools.py`

MCP 用例使用脚本化本地 OpenAI 兼容服务，不访问真实模型。Mock LLM 只负责
在第一轮返回确定的工具调用，并在收到工具结果后结束会话；以下链路全部真实
执行：

```text
msagent → msprof-mcp stdio Server → trace_processor_shell → trace_view.json
```

当前覆盖：

- Profiler 初始化时能启动 `msprof-mcp`，并发现唯一的 `analyze_overlap` 和
  `find_slices` 工具。
- `analyze_overlap` 分析合成 trace 后，精确返回 Computing、Communication、
  Communication(Not Overlapped) 和 Free 的时长及占比。
- `find_slices` 使用 exact、process 和 main-thread 条件，只返回目标进程主线程
  上的两个 `MatMulValidation` slice，排除工作线程、近似名称和干扰进程。

合成 `trace_view.json` 会写在用例诊断目录的 `inputs/` 下。测试不会临时安装
Perfetto 或替换不兼容的 `trace_processor_shell`；缺失、启动失败或 glibc
不兼容都属于发布失败。

### 7.3 `test_03_skills.py`

当前覆盖 `op-mfu-calculator`：

- P0：通过 `/op-mfu-calculator` 显式指定 Skill。
- P1：通过自然语言中的 MFU 计算语义触发 Skill。

当前断言验证：

- trace 中调用了预期 `get_skill`。
- Skill 名称和 category 正确。
- `tool_call` 与 `tool_result` 可以通过 `item_id` 关联。
- `get_skill` 返回成功且包含目标 Skill 内容。
- 会话正常结束并产生最终回复。

当前没有断言 MFU 数值计算结果是否正确；现阶段验证的是 Skill 路由和读取
链路。

文件中还保留动态热加载占位用例：

```text
test_running_interactive_session_discovers_copied_skill_without_restart
```

目标能力是在同一个交互式 msagent 进程运行期间，通过外部 `cp` 添加 Skill，
随后无需重启即可发现。该用例目前标记为 skip，尚未实现交互进程控制。

### 7.4 `test_04_sys_prompt.py`

测试使用本地 OpenAI 兼容服务捕获 msagent 发出的最终请求 payload，不依赖
真实模型返回。

当前至少验证两个 Agent：

- `Profiler`
- `Accuracy`

主要断言：

- Agent 身份和领域提示词正确。
- System Prompt 包含对应 Skills。
- `get_skill` 和领域工具的暴露范围正确。
- Profiler 与 Accuracy 之间不存在错误的 Prompt 或工具串用。
- 工作目录、OS、Python 等运行环境已正确注入。
- System Prompt 不包含未替换的环境占位符。

这些断言表达发布要求。若当前 whl 的 Skill category 或 Prompt 内容不符合
要求，测试应当失败并暴露产品问题，而不是放宽断言使其通过。

### 7.5 `test_05_local_env.py`

当前包含四个真实模型驱动的本地环境用例：

- `read_file` 能读取 workspace 中的 `read_marker.txt`，并在 trace 和最终
  回复中返回唯一验证标记。
- `read_file` 读取不存在的文件时返回明确错误，且不会创建该文件或导致
  msagent 会话异常退出。
- `execute` 能运行 workspace 中的 `print_marker.sh`，并在 trace 中返回
  唯一输出标记及退出码 0。
- `execute` 能运行 `msprof-analyze --help`，且帮助内容包含 `cluster` 和
  `advisor` 等关键子命令。

`msprof-analyze` 是目标 whl 声明的运行依赖。测试不会在缺失时自动安装，
否则会掩盖 whl 依赖声明、包索引或 console entry point 问题。

## 8. msagent 执行与断言依据

`agent_runner.run_msagent()` 使用参数列表直接启动进程，不经过 shell：

```text
msagent -v --no-stream \
  --trace-jsonl <invocation-dir>/trace.jsonl \
  -w <workspace> \
  [--agent <agent-name>] \
  <prompt>
```

运行时强制注入：

```text
MSAGENT_LOG_LEVEL=DEBUG
```

各输出的定位如下：

- stdout：`--no-stream` 模式下的最终回答。
- stderr：CLI 错误及基座采集诊断。
- `trace.jsonl`：工具调用、工具返回、Agent 消息、会话结束等结构化事件。
- `$MSAGENT_HOME/logs/app.log`：DEBUG 应用日志。

核心功能断言优先使用 `trace.jsonl`。stdout 只用于确认最终文本，`app.log`
用于补充定位初始化、配置加载和运行时异常。

## 9. Workspace 与 MSAGENT_HOME 生命周期

隔离单位是一个 pytest 用例：

```text
一个测试用例
├── 一个临时 workspace
├── 一个临时 MSAGENT_HOME
└── 零到多次 msagent 调用
```

不同用例必须使用不同目录，原因包括：

- Filesystem 和 LocalShell 可能修改 workspace。
- `MSAGENT_HOME` 包含配置、Skill、日志、checkpoint 和项目状态。
- 共享 `app.log` 会造成并发或连续用例日志污染。
- 测试结果不应依赖用例执行顺序。

同一用例中的多次调用共享 workspace 和 `MSAGENT_HOME`，用于支持动态 Skill、
多轮会话和前后状态对比。

仓库中不保存运行生成的 `.msagent`。每个运行时必须由待验证 whl 自行初始化，
避免旧配置掩盖 whl 的打包或初始化问题。

## 10. 失败诊断产物

每次 msagent 调用都会写入独立目录：

```text
artifacts/<run-id>/cases/<pytest-node-id>/runtime-00/invocation-01/
├── trace.jsonl
├── app.log
├── stdout.txt
├── stderr.txt
└── command.json
```

其中 `app.log` 是本次调用新增的日志片段，不包含同一 `MSAGENT_HOME` 中更早
调用的历史日志。

System Prompt 测试还会保存：

```text
llm-requests.json
```

MCP 工具执行用例还会保存 Mock LLM 的完整多轮请求：

```text
scripted-llm-requests.json
scripted-llm-errors.json  # 仅 Mock 协议异常时生成
inputs/synthetic_trace_view.json
```

失败用例可额外保存：

```text
failed-workspaces/
```

默认不复制完整 `MSAGENT_HOME`，避免归档认证状态、checkpoint 和大量运行
数据。pytest 会在 session 结束时删除成功用例的 case 目录，保留失败用例，
并在控制台中打印对应 trace 路径。

trace、日志和请求 payload 可能包含用户 Prompt、工具参数或模型回答。流水线
上传这些产物时应使用受控访问权限并设置合理的保留周期。

## 11. 公共模块使用示例

测试优先使用 `msagent_runtime_factory` 或兼容名称
`llm_runtime_factory`：

```python
def test_example(msagent_runtime_factory):
    runtime = msagent_runtime_factory("openai")
    result = runtime.run("Ping")

    assert result.returncode == 0
    assert result.traces
    assert result.trace_path.exists()
    assert result.app_log_path.exists()
```

同一个测试需要多次调用时，应复用同一个 runtime：

```python
runtime = msagent_runtime_factory("openai")
before = runtime.run("first prompt")
after = runtime.run("second prompt")
```

不要在测试文件中重复创建 subprocess、解析 JSONL 或计算 `app.log` 增量。

## 12. 尚未实现的流水线能力

以下能力不应被视为当前脚本已覆盖：

- 在 `run_validation.sh` 中安装 `requirements-test.txt` 并启动 pytest。
- 从已安装旧版本升级到目标 whl。
- `uv tool install <given-whl>` 安装验证。
- 运行中的交互式 msagent 对外部复制 Skill 的热加载。

这些能力应在现有安装门禁和诊断产物机制稳定后逐步接入。
