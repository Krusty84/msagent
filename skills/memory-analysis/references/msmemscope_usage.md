# msMemScope 工具使用速查（命令 / API / 场景选型 / 环境）

> 本文件承载 msMemScope 工具**本身的用法**：命令行与 Python 接口参数、各场景的采集配置选型、输出产物位置、环境变量管理。与官方《内存采集》用户指南对齐，以当前安装版本 `msmemscope --help` 输出为准，如与本文件不一致，以工具实际输出与官方文档为准。
> **下一篇**：采集产物的数据字段与通用解读见 `msmemscope_data.md`；具体问题诊断见 `{scenario}_diagnosis_guide.md`（`oom_diagnosis_guide.md` / `leak_diagnosis_guide.md`）。

## 1. 命令行参数全表

### 命令格式

```bash
# 方式一（推荐）：user.sh 为用户脚本
msmemscope [options] bash user.sh

# 方式二：直接指定程序
msmemscope [options] -- <prog_name> [prog_options]
```

> 内存对比功能（`--compare`）不需要 prog_name/prog_options。

### 参数表

| 参数 | 可选/必选 | 说明 |
| --- | --- | --- |
| `--help, -h` | 可选 | 输出帮助信息 |
| `--version, -V` | 可选 | 输出版本信息 |
| `--verbose, -v` | 可选 | 日志级别设为 debug（等价 `--log-level=debug`） |
| `--quiet, -q` | 可选 | 日志级别设为 error（等价 `--log-level=error`） |
| `--steps` | 可选 | 采集指定 Step 的内存信息，1 个或多个（最多 5 个），逗号（全/半角均可）分隔，须为实际 Step 范围内的整数。示例：`--steps=1,2,3`。不配置则采集所有 Step。**与分析功能互斥** |
| `--device` | 可选 | 采集设备：`npu`（默认，所有 NPU）、`npu:{id}`（指定卡，id∈[0,31]，可多个，示例 `--device=npu:2,npu:7`）、`cpu`（仅锁页内存）。可多选逗号分隔；同时含 npu 与 npu:{id} 时按所有 npu 处理 |
| `--level` | 可选 | 采集粒度：`op`（默认）/ `kernel`。旧值 0/1 兼容但已废弃（0=op，1=kernel） |
| `--events` | 可选 | 采集事件：`alloc`（内存申请）、`free`（内存释放）、`launch`（算子/kernel 下发）、`access`（内存访问，仅 ATB 与 Ascend for PyTorch 算子场景）、`traceback`（Python Trace，**仅 API 方式**）、`none`（不采集，与其他值同时出现时以 none 为准并告警）。默认 `alloc,free,launch`。注意：alloc/free 不成对配置会导致分配-释放配对信息缺失，影响可视化与泄漏分析 |
| `--call-stack` | 可选 | 采集调用栈：`python`、`c`，可同时选择逗号分隔；深度在选项后以英文冒号指定（`python:10,c:20`），范围 [0,1000]，默认 50 |
| `--collect-mode` | 可选 | 采集方式：`immediate`（默认，脚本开始即采集）/ `deferred`（等待 Python `msmemscope.start()` 后开始）。deferred 仅配合 Python 自定义采集接口使用；**deferred 模式下内存分析功能不可用** |
| `--analysis` | 可选 | 分析功能（多选逗号分隔）：`leaks`（泄漏识别，默认）、`decompose`（内存拆解）、`oom[:K]`（OOM 详细分析，K∈[1,1000] 默认 10，自动联动 alloc/free）、`none`（不启用，与其他值同时出现时以 none 为准并告警）。示例：`--analysis=leaks,decompose`、`--analysis=oom:50` |
| `--format` | 可选 | 输出格式：`db` / `csv`（默认 csv）。db 格式可用 MindStudio Insight 展示。旧参数名 `--data-format` 兼容但已废弃 |
| `--output-path` | 可选 | 输出路径（≤4096 字符），默认 `memscopeDumpResults`。旧参数名 `--output` 兼容但已废弃 |
| `--log-level` | 可选 | 日志级别：`debug`/`info`（默认）/`warning`/`error`。旧值 `warn` 兼容（=warning） |
| `--compare` | 可选 | 开启 Step 间内存对比，须与 `--input-path` 成对使用 |
| `--input-path` | 可选 | 对比文件目录（基线+对比两个，逗号分隔，≤4096 字符），仅 compare 开启时有效 |

### 命令行示例

```bash
# 基础采集（默认：alloc,free,launch + leaks 分析）
msmemscope bash train.sh

# OOM 分析，Top-50，含调用栈（默认 csv，无需指定 --format）
msmemscope --analysis=oom:50 --call-stack=python:10,c:20 --output-path=/home/user/out bash train.sh

# 显存拆解（自动使能框架钩子）
msmemscope --analysis=decompose --events=alloc,free bash train.sh

# 采集指定 Step（供对比），建议一次一个 Step
msmemscope --steps=5 --level=kernel bash train.sh

# Step 间对比
export TASK_QUEUE_ENABLE=0
msmemscope --compare --input-path=/home/user/step1,/home/user/step2 --level=kernel
```

## 2. Python 接口速查

### 环境变量注入（必须先 source，详见 §5 环境变量管理）

```bash
source msmemscope --load-api-env      # 设置 API 方式所需环境变量
source msmemscope --unload-api-env    # 清除 msMemScope 相关环境变量（保留其他工具的值）
```

### 核心接口

| 接口 | 说明 |
| --- | --- |
| `msmemscope.config(...)` | 配置采集参数，参数与命令行对应：`device`、`level`、`events`、`call_stack`、`analysis`、`output`、`data_format` |
| `msmemscope.start()` / `msmemscope.stop()` | 开启 / 退出采集。start 时自动记录调用前已分配且未释放的存量显存（保证总量统计准确；存量块不参与泄漏/拆解等分析，对用户透明） |
| `msmemscope.step()` | 输入 Step 信息，推荐 Python 场景下用于 Step 粒度采集 |
| `msmemscope.take_snapshot(device_mask=0, name="...")` | 采集一次内存快照。device_mask 支持 num / list / tuple，默认全部设备；**可独立调用**，不依赖 start/stop；与 config 同时使用时输出路径以第一次调用为准 |
| `msmemscope.RecordFunction("name")` | 自定义 Trace 打点，支持上下文（`with`）与装饰器两种模式 |
| `msmemscope.tracer.start()` / `msmemscope.tracer.stop()` | 开启/关闭 Python Trace（将于 MindStudio 26.0.0 下线，建议改用 `events="traceback"`） |
| `msmemscope.check_leaks(input_path=..., mstx_info=..., start_index=0)` | **离线**泄漏分析（见下） |
| `msmemscope.cleanup_framework_hooks()` | 清除之前所有已注册的一键分析补丁 |
| `msmemscope.init_framework_hooks(framework, version, component, type)` | 一键分析：初始化框架钩子（vLLM/FSDP/verl 等），type 支持 `decompose`/`snapshot`（支持组合见 §4） |

### config 参数对应关系

`msmemscope.config()` 支持参数与命令行参数的对应关系（均可选，示例为组合用法）：

```python
msmemscope.config(
    call_stack="c:10,python:5",     # 对应 --call-stack
    events="launch,alloc,free",     # 对应 --events
    level="0",                      # 对应 --level（op/kernel 或 0/1）
    device="npu",                   # 对应 --device
    analysis="leaks,decompose",     # 对应 --analysis
    data_format="csv",               # 对应 --format（默认 csv；仅可视化时改 db）
    output="/home/projects/output", # 对应 --output-path
)
```

### 采集模板（Python 接口）

```python
import msmemscope

msmemscope.config(
    events="alloc,free",
    level="op",
    device="npu",
    analysis="leaks,decompose",
    call_stack="c:10,python:5",
    data_format="csv",   # 默认 csv；仅在需要 MindStudio Insight 可视化时才改 db
    output="/home/user/output",
)
msmemscope.start()   # 开启采集
train()              # train() 为用户代码
msmemscope.stop()    # 退出采集
```

局部采集（推荐优先评估，见 SKILL「采集范围评估」）：用 `start()/stop()` 包裹目标代码段，配合多段采集与 `step()` 使用。

### 采集模板（mstx 打点，C/Python 均可）

```python
import mstx
for epoch in range(15):
    id = mstx.range_start("step start", None)   # 标识 Step 开始并开启内存分析
    ...                                          # 用户代码
    mstx.range_end(id)                           # 标识 Step 结束
```

离线泄漏分析用 mstx mark 打点标记三个点 A、B、C：A→B 范围内申请的内存需在 C 点前全部释放，否则判定为泄漏。打点文本在后续 `check_leaks(mstx_info=...)` 中作为输入。

### 离线泄漏分析（check_leaks）

1. 用 mstx mark 打点标记三个点 A、B、C（A→B 范围申请的内存需在 C 前释放，否则判定为泄漏）。
2. 运行采集命令获取落盘 csv：`msmemscope bash user.sh`。
3. 调用接口：

```python
import msmemscope
msmemscope.check_leaks(input_path="/user/memscope.csv", mstx_info="test", start_index=0)
```

参数说明：

- `input_path`：csv 文件所在路径（绝对路径）。
- `mstx_info`：mark 打点使用的 mstx 文本信息，标识泄漏分析范围。
- `start_index`：泄漏分析开始的打点位置编号（从第几个符合条件的 mstx 打点开始）。

> 离线方式目前仅支持 HAL 内存泄漏分析。

### Python 接口完整模板

见 `assets/collection_template.py`。

## 3. 输出文件速查

| 文件 | 默认位置 | 说明 |
| --- | --- | --- |
| `memscope_dump_{ts}.csv` | `msmemscope_{PID}_{ts}_ascend/device_{id}/dump/`（Python 接口）或 `memscopeDumpResults/`（命令行） | 内存事件数据 |
| `memscope_dump_{ts}.db` | 同 csv | db 格式，可用 MindStudio Insight 展示 |
| `memory_compare_{ts}.csv` | `memscopeDumpResults/compare/` | Step 间对比结果 |
| `python_trace_{TID}_{ts}.csv` | 同 csv 目录 | Python Trace 数据 |
| `config.json` | `msmemscope_{PID}_{ts}_ascend/` | 采集配置信息 |

字段详解见 `msmemscope_data.md`。

## 4. 场景与采集配置矩阵

> 与 msMemScope 官方《内存分析》用户指南对齐。版本号以当前工具支持为准，遇版本不符时以官方文档及 `msmemscope --help` 为准。

### 4.1 分析能力总览

| 分析能力 | 说明 | 主要支持场景 |
| --- | --- | --- |
| 内存泄漏（leaks） | 内存长时间未释放检测；在线回显 + 离线 `check_leaks`；kernel Launch 粒度内存变化分析 | 训练（在线/离线）；**VLLM-Ascend 不支持** |
| 内存拆解（decompose） | 按模块/组件拆解显存占用（权重、梯度、优化器状态、激活值、kv_cache 等） | 训练（原生/FSDP）、推理（vLLM-Ascend 11.0）；verl 暂不支持 |
| OOM 分析（oom[:K]） | OOM 发生时自动采集触发信息、最近未释放记录、最大未释放记录 | 通用 |
| 内存快照（snapshot） | 采集设备总内存/空闲内存、torch 预留/使用及峰值等 | 通用（Python 接口 `take_snapshot`）；vLLM/verl/mindspeed_llm 用一键分析 |
| 内存对比（compare） | 两个 Step 间内存使用差异（kernel 粒度） | 训练/推理（需两次采集） |

### 4.2 场景能力矩阵

| 场景 | 框架/版本 | 支持的采集与分析能力 | 采集方式 | 备注 |
| --- | --- | --- | --- | --- |
| 训练 | Ascend for PyTorch（原生，版本不限） | 泄漏、拆解、OOM、快照、对比、曲线 | 命令行 / Python 接口 | 拆解细分类：aten、weight、gradient、optimizer_state（weight/gradient/optimizer_state 需调用过 `optimizer.step()`；aten 需 PyTorch ≥ 2.3.1 且 `--level=op`） |
| 训练 | pytorch fsdp1（2.6+） | 拆解（自动使能） | 命令行 / Python 接口 | 拆解内容：激活值、分片权重、all-gather 缓冲、梯度等 |
| 训练 | pytorch fsdp2（2.6-2.9、2.10+） | 拆解（自动使能） | 命令行 / Python 接口 | 同上 |
| 推理 | vllm-ascend（11.0） | 拆解（decompose）、快照（snapshot） | **仅 Python 接口（一键分析）** | 命令行采集、泄漏分析、监测均不支持 |
| 强化学习 | verl（0.7.0） | 推理阶段快照（snapshot） | Python 接口（一键分析） | 拆解暂不支持；训练阶段可用常规接口（take_snapshot） |
| 训练 | mindspeed_llm（0.12.1） | 快照（snapshot） | Python 接口（一键分析） | — |

> 版本键说明："2.6+" 表示 2.6 及以上；"2.6-2.9" 表示 2.6~2.9 各分支全部版本（含补丁，不含 2.10）；"2.10+" 表示 2.10 及以上。

### 4.3 一键分析（init_framework_hooks）支持组合

| 场景 | 框架 | 版本 | 组件 | 功能 |
| --- | --- | --- | --- | --- |
| 推理 | vllm_ascend | 11.0 | worker | decompose、snapshot |
| 训练 | pytorch | 2.6+ | fsdp1 | decompose |
| 训练 | pytorch | 2.6-2.9、2.10+ | fsdp2 | decompose |
| 训练 | mindspeed_llm | 0.12.1 | training | snapshot |
| 强化学习 | verl | 0.7.0 | TaskRunner | snapshot |

使用要点：

- 先 `cleanup_framework_hooks()` 清空历史注册，再 `init_framework_hooks(...)` 注册。
- 拆解（decompose）场景钩子已自动使能（配置 `analysis=decompose` 即可，无需手动调用）；若手动调用且 type 为 decompose，自动使能将让位（互斥语义），以手动注册为准。
- 快照（snapshot）暂不支持自动使能，必须手动调用 `init_framework_hooks` 开启。

### 4.4 自动拆解（analysis=decompose）支持范围

配置 `analysis=decompose`（命令行或 Python 接口）后，msMemScope 自动遍历已注册框架劫持映射、全量注册拆解钩子；钩子惰性激活——仅在目标框架模块被实际导入时生效，未安装框架的钩子静默不生效。

| 场景 | 框架 | 版本 | 拆解内容 |
| --- | --- | --- | --- |
| 训练 | Ascend for PyTorch（python 原生训练） | 不限制 | 权重、梯度、优化器状态等内存申请 |
| 训练 | pytorch fsdp1 | 2.6+ | FSDP 分布式训练的激活值、分片权重、all-gather 缓冲、梯度等内存申请 |
| 训练 | pytorch fsdp2 | 2.6-2.9、2.10+ | 同上 |
| 推理 | vllm-ascend | 11.0 | 推理过程中的 load_weight、profile_run、kv_cache、activate 等环节的内存占用 |

### 4.5 采集配置速查（按诉求）

| 诉求 | 配置组合 | 说明 |
| --- | --- | --- |
| 常规分析（默认） | `--format=csv`（缺省即 csv） | **默认 csv**：采集快、体积小、便于脚本分析；仅 MindStudio Insight 可视化诉求时改用 db（采集慢且分析前须转回 csv） |
| 一般分析 | `--analysis=leaks` | 默认行为 |
| OOM 分析 | `--analysis=oom[:K]` | 自动联动 alloc/free；K∈[1,1000] 默认 10 |
| 显存拆解 | `--analysis=decompose` | 自动使能框架钩子，无需额外参数 |
| Step 间对比 | 采集 `--steps=N --level=kernel`（两次）+ `--compare --input-path=p1,p2` | 对比前 `export TASK_QUEUE_ENABLE=0` |
| 显存曲线 | `--events=alloc,free` | 曲线由 used/device_used 事件驱动 |
| 自定义标签拆解 | `--analysis=decompose` + `describe` 接口 | 标签最多 3 个不重复 |

### 4.6 不支持的场景与限制

| 限制 | 说明 |
| --- | --- |
| VLLM-Ascend | 不支持命令行采集、泄漏分析 |
| 命令行采集 | `--events=traceback` 不可用（仅 API） |
| deferred 模式 | 内存分析功能不可用；监测、拆解只对采集范围内数据可用 |
| `--steps` 与分析功能 | 互斥，不可同时使用 |
| mstx 打点采集 | 仅支持单卡局部内存数据 |
| 内存快照 | 训练场景用 `take_snapshot`；vLLM 推理/verl/mindspeed_llm 用一键分析 |

## 5. 环境变量管理与常见风险

> msMemScope 通过 LD_PRELOAD 等技术注入采集环境。环境变量处理不当可能污染用户后续操作，本文档汇总相关管理与风险规避。

### 5.1 API 方式环境变量注入与清理

Python 接口采集前必须注入环境变量，使用完毕后必须清理：

```bash
# 注入（必须通过 source 方式执行，仅在当前 shell 生效）
source msmemscope --load-api-env

# 清理（必须通过 source 方式执行；只清除 msMemScope 相关条目，保留其他工具的值）
source msmemscope --unload-api-env
```

**为什么必须清理**：注入的 `LD_PRELOAD` / `LD_LIBRARY_PATH` 等 LD 类环境变量会作用于之后启动的所有进程。若未及时清除：

- 后续执行 set_env 类操作时可能产生不可预知的错误；
- 手动设置 `LD_PRELOAD`、`LD_LIBRARY_PATH` 等变量时可能与其他工具（如其他 LD_PRELOAD 类工具）产生冲突；
- 残留的采集库可能被无关进程加载，造成采集行为错乱或性能影响。

**建议**：

- 在脚本/会话结束时立即清理，或在使用环境变量前记录原值（`echo $LD_PRELOAD`）以便恢复；
- 若发现清理不彻底，手动将 `LD_PRELOAD`、`LD_LIBRARY_PATH` 恢复为注入前状态；
- 使用命令行采集方式（`msmemscope bash user.sh`）时由工具自行管理环境，无需手动注入。

### 5.2 与 LD_PRELOAD 类工具的共存

LD_PRELOAD 方式注入存在作用域限制，多个 PRELOAD 工具同时启用时可能相互影响：

- 同一进程的 `LD_PRELOAD` 会被追加/覆盖，多个工具叠加时需确认加载顺序与符号冲突；
- 使用 msMemScope 期间，避免同时启用其他依赖 LD_PRELOAD 注入的工具（如部分 profiler/监控工具）；
- 排查问题时，可先确认当前环境变量中是否有其他工具的 PRELOAD 残留。

### 5.3 关键环境变量

| 环境变量 | 作用 | 建议 |
| --- | --- | --- |
| `TASK_QUEUE_ENABLE` | 算子下发队列优化开关。配置为 `2` 时开启 task_queue 下发队列 Level 2 优化，此时会采集 workspace 内存 | Step 间对比（compare）前需设置为 `0`；需要 workspace 内存数据时设为 `2` |
| `PYTHONMALLOC` | Python 内存分配器选择。`PYTHONMALLOC=malloc` 表示所有分配走 malloc | mstx 打点采集场景可配置；对小内存申请有一定影响 |
| `TOKENIZERS_PARALLELISM` | HuggingFace tokenizers 库并行开关 | `--level=kernel` 且使用 tokenizers 时可能出现"进程 fork 并行被禁用"告警；可设 `false` 规避，该告警本身不影响功能 |
| `ASCEND_LAUNCH_BLOCKING` | 算子下发阻塞模式 | 仅内存块监测（watch）场景需要，本 Skill 不覆盖 |
| `LD_PRELOAD` / `LD_LIBRARY_PATH` | msMemScope 注入的采集环境 | 使用后恢复原值（见上） |

### 5.4 其他注意事项

- **root 用户运行**：使用 root 用户运行 msMemScope 时会跳过文件权限校验并打印提示，存在安全风险，建议使用普通用户权限安装执行。
- **历史显存感知**：Python API 模式下 `msmemscope.start()` 会自动将调用前已分配且未释放的显存块（存量显存）记录到输出文件，保证 start~stop 区间显存总量统计准确；存量块仅落盘记录，不参与泄漏分析、拆解等分析流程。此机制对用户透明，无需额外配置。
- **输出路径**：默认 `memscopeDumpResults`；`--output-path` 最大长度 4096 字符。
- **告警提示**：使用废弃参数（`--data-format`/`--output`/`--level=0|1`/`--log-level=warn`）时会输出弃用告警，属正常现象，按提示改用新参数即可。