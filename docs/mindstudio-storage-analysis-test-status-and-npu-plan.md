# 存储分析 Skill 测试现状与 NPU 真机测试方案

本文用于把 `mindstudio-storage-analysis` Skill 交接到一台新租用的 Ascend NPU
测试机。测试必须通过 **msAgent 调用 Skill** 完成，不能只单独运行 Python 脚本后就宣称
Skill 已经通过端到端测试。

## 1. 先看结论

- 本机已完成：规则单元测试、真实本地磁盘压力、真实小文件、真实多进程争抢、长时间
  采集、目标自动发现、DeepSeek + msAgent 路由、终端结论与 HTML 报告。
- 本机只做了回放：NFS 异常、CIFS 识别、采集权限不足。这些证明分析逻辑能处理对应
  JSON，但**不能证明真实 NFS 采集链路可用**。
- 本机没有完成：Ascend NPU、CANN、`torch_npu`、`msprof`、真实 R500、Ascend 多卡，
  以及真实 NFS。原因是当前机器是 NVIDIA H200，项目目录在本地 XFS 上，不是 NFS。
- 租 NPU 后的重点不是再跑十个模型，而是打通一条完整证据链：msAgent 找到训练进程和
  数据路径 -> 同窗口采集 Host IO 与 NPU Profile -> 规则分析 -> Agent 解释 -> HTML 报告。

## 2. 状态定义

| 标记 | 含义 |
| --- | --- |
| 真机通过 | 在当前服务器上真实启动进程、采集系统数据并分析 |
| 回放通过 | 使用符合结构的 JSON 模拟异常，只验证规则，不代表真实采集通过 |
| 未测试 | 当前机器缺少必要硬件、挂载或软件环境 |

## 3. 现在已经测了什么

测试日期为 2026-07-31，当前测试机为 NVIDIA H200，项目路径位于本地 XFS。

| 项目 | 状态 | 实际做法 | 结果 |
| --- | --- | --- | --- |
| 全部自动化回归 | 真机通过 | 运行 Skill 的单元测试和确定性用例 | `244 passed`，另有 `106` 个子测试通过 |
| Skill 结构校验 | 真机通过 | 校验 frontmatter、目录和引用 | `Skill is valid!` |
| msAgent + DeepSeek | 真机通过 | msAgent 连接 DeepSeek，执行真实工具调用 | API、流式工具调用正常 |
| Skill 自动路由 | 真机通过 | 8 个隔离 msAgent 会话触发本 Skill | 8/8 路由正确 |
| 十个 TorchVision 模型 | 真机通过 | H200 上逐个短训，完整执行发现、采集和分析 | 无存储异常时没有误报高危 |
| 自动找 PID 和路径 | 真机通过 | 对 `torchrun` 及其子进程执行只读发现 | 能优先找到根进程和数据路径；歧义时要求确认 |
| R100 本地磁盘压力 | 真机通过 | 16 个稳定 direct-IO 读取进程制造压力 | 识别为 `high/high` |
| R300 大量小文件线索 | 真机通过 | 20,000 个 4 KiB 文件、12 个读取进程 | R300 为 `medium/medium`；没有把线索夸大成确定根因 |
| R400 IO 干扰 | 真机通过 | 多个进程同时争抢同一块受压磁盘 | 识别为 `high/high` |
| 多 rank 映射 | 真机通过 | `torchrun` 启动 4 个 rank | 找到根进程及 4 个子进程；磁盘未受压时不误报 R400 |
| 进程采集中退出 | 真机通过 | 采集窗口内终止目标进程 | 采集器不崩溃，并降低证据可信度 |
| 30 分钟稳定性 | 真机通过 | 连续采集约 1800 秒 | 内存约 47 MB；发现并修复一条无效 `%util` 样本处理问题 |
| 本地磁盘优化 A/B | 真机通过 | 小块高并发和大块低并发读取对比 | 约 879.7 MiB/s -> 5873.2 MiB/s，约 6.7 倍；R300 线索消失 |
| NFS 异常规则 | 回放通过 | 模拟 RTT 60 ms、执行 120 ms、重传 2%、重大超时 1 次 | R200 为 `high/high`，Agent 能输出建议和 HTML |
| CIFS 网络存储 | 回放通过 | 模拟 CIFS 挂载信息 | 能识别 CIFS，但不会冒充 NFS 给出已确认结论 |
| `/proc` 权限不足 | 回放通过 | 模拟进程映射读取失败 | 报告证据缺口，不把“没采到”当作“系统健康” |
| HTML 报告 | 真机通过 | 固定模板生成单文件 HTML，Chromium 桌面和手机视口检查 | 页面可打开，移动端导航正常；纯文本 LLM 也可生成 |
| 危险操作安全门 | 真机通过 | 通过 msAgent 请求 remount、drop_caches、readahead | 只预览；`drop_caches` 拒绝执行；未改系统状态 |

说明：十个模型短训主要验证“正常场景不误报”和端到端流程，并不能替代 NFS、R500、
多卡等专门场景测试。

## 4. 还没有测什么

| 待测项 | 当前为什么没测 | 租用环境需要什么 | 通过标准 |
| --- | --- | --- | --- |
| Ascend NPU 运行时 | 当前是 NVIDIA H200 | Ascend NPU、匹配的驱动/固件、CANN、ACL | `npu-smi`、ACL 冒烟和训练均通过 |
| R500：Host IO 是否传导到 NPU 空闲 | 没有 Ascend profiler 时间线 | 同一训练窗口的 IO Snapshot 与可信 `msprof` timeline/DB | Host IO 异常与 NPU 空闲时间重叠；无重叠时不得下高置信结论 |
| `summarize_msprof.py` 真数据 | 当前无 Ascend `op_summary` | `msprof` 导出的真实 `op_summary_*.csv` | 能生成辅助摘要，但不能单独证明 R500 |
| Ascend 多卡多 rank | 当前只做了 NVIDIA 单机多进程 | 至少 2 张 NPU，`torch_npu`，`torchrun` 或 `msrun` | PID、rank、数据路径和存储设备映射正确，无假干扰者 |
| 真实 NFS 采集 | 当前没有 NFS 挂载 | NFS 服务端、测试挂载点、可控测试数据 | 采到目标挂载的 mountstats；正常不误报，异常能触发 R200 |
| NFS 大量小文件 | 当前小文件在本地 XFS | NFS 数据集，至少数万小文件 | R300 给出远程元数据/小文件证据，并与本地大文件 A/B 对照 |
| NFS 延迟、重传、超时 | 当前只有 JSON 回放 | 隔离 NFS 测试网络及管理员许可 | 真实 mountstats 出现异常，R200 证据字段与时间窗正确 |
| 其他网络存储真机 | 没有 Lustre/CIFS/GPFS/BeeGFS/Ceph | 对应挂载和专用监控工具 | 正确识别并转交，不用 NFS 阈值冒充深入诊断 |
| 跨用户 `/proc` 权限 | 当前只有回放 | 训练用户与 msAgent 用户分离 | 权限不足时明确报缺失；权限具备时正确映射进程 |

本次租机最优先完成前三项。如果还要验证 R200/R300，租机时必须同时确认有真实 NFS
测试挂载；“一台 NPU 服务器”本身不等于“有 NFS”。

## 5. 上传 GitCode 前的 Skill 内容

应提交的目录为：

```text
skills/mindstudio-storage-analysis/
├── SKILL.md
├── agents/openai.yaml
├── requirements.txt
├── scripts/                 # 5 个生产脚本
│   ├── discover_io_target.py
│   ├── collect_io_snapshot.py
│   ├── analyze_io_snapshot.py
│   ├── summarize_msprof.py
│   └── render_io_report.py
├── assets/io_report_template.html
├── references/              # 数据格式、采集、故障建议、HTML 契约
└── evals/                   # 确定性测试、真机检查器和受控 workload
```

不要提交以下内容：API Key、`.msagent/`、`deepseek.env`、`__pycache__`、`.pyc`、
本机生成的 `io_snapshot.json`、`findings.json`、`io_report.html` 和大体积测试数据。

## 6. NPU 租机要求

租机前确认：

| 要求 | 最低条件 |
| --- | --- |
| NPU | 至少 1 张可用 Ascend；多卡测试需要至少 2 张 |
| 软件 | Python 3.11+、NPU 驱动/固件、CANN Toolkit/Runtime、`torch_npu`、`msprof` |
| 磁盘 | 有足够空间放训练数据、Profile 和测试文件；不要在系统盘做压力测试 |
| 权限 | 能读取目标进程 `/proc`；NFS 测试需有现成挂载或管理员协助 |
| 数据位置 | 一份本地磁盘数据；若测 NFS，再准备一份 NFS 数据 |
| 网络 | 能访问 GitCode 和 DeepSeek，或提前准备离线依赖与模型 |

进入机器后先执行：

```bash
npu-smi info
which msprof
python3 --version
python3 -c 'import torch, torch_npu; print(torch.__version__); print(torch.npu.is_available())'
findmnt -T /path/to/dataset
```

`findmnt` 输出的文件系统类型如果不是 `nfs`/`nfs4`，就不能把该路径当作真实 NFS 测试。

## 7. 克隆正确分支并安装 msAgent

```bash
git clone -b feature/issue-12-storage-analysis-skill --single-branch \
  https://gitcode.com/weixin_50941460/msagent.git
cd msagent
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
bash scripts/build_whl.sh
python3 -m pip install dist/mindstudio_agent-*.whl
python3 -m pip install -r skills/mindstudio-storage-analysis/requirements.txt
msagent --version
```

这里从仓库根目录运行 msAgent 时，它会自动扫描 `skills/`，不需要复制 Skill。若只把
Skill 目录放到另一个项目，可在 msAgent 交互界面执行：

```text
/add-skill /absolute/path/to/mindstudio-storage-analysis
```

## 8. 配置 DeepSeek，并验证 msAgent

Key 只放环境变量，不要写进仓库或测试报告：

```bash
export OPENAI_API_KEY='<填写你的 DeepSeek API Key>'
msagent config \
  --llm-provider openai \
  --llm-base-url https://api.deepseek.com \
  --llm-model deepseek-chat \
  -w "$PWD"
msagent config --show -w "$PWD"
msagent --stream -w "$PWD" '只回答 MSAGENT_DEEPSEEK_OK'
```

之后确认 Skill 能被加载：

```bash
msagent --stream -w "$PWD" \
  '/skills mindstudio-storage-analysis 只说明你将使用哪个 Skill，不要开始采集'
```

预期回答应明确提到 `mindstudio-storage-analysis`。后续测试统一使用 `--stream`。

## 9. 先跑确定性回归

```bash
python3 skills/mindstudio-storage-analysis/evals/run_eval.py
python3 -m unittest discover \
  -s skills/mindstudio-storage-analysis/evals \
  -p 'test_*.py' -v
```

预期没有失败。`SKIP` 只能表示环境前提不满足，不能算通过。

## 10. NPU 真机测试顺序

### 10.1 NPU 运行时冒烟

只在空闲、隔离的测试节点上，由人明确决定后执行：

```bash
source /path/to/cann/set_env.sh
python3 skills/mindstudio-storage-analysis/evals/run_npu_runtime_eval.py \
  --elements 1048576 \
  --iterations 100 \
  --report /tmp/npu-runtime.json
```

预期报告为 `PASS`，并列出实际 NPU device。这个测试只证明 ACL/NPU 能运行，不证明
存储导致了 NPU 空闲。

### 10.2 启动一项真实 Ascend 训练

使用你准备验证的真实训练命令，至少运行 10 分钟，数据路径必须明确。例如：

```bash
source /path/to/cann/set_env.sh
torchrun --nproc_per_node=1 train.py --data /path/to/dataset
```

不要为了“凑十个模型”反复跑短训练。先让一个代表性训练完成端到端诊断，再扩展到第二个
模型和多卡场景。

### 10.3 用 msAgent 自动找目标并跑完整流程

训练运行期间，在仓库根目录执行：

```bash
msagent --stream -w "$PWD" '
/skills mindstudio-storage-analysis
请诊断当前正在运行的 Ascend 训练是否存在存储或 Host IO 瓶颈。
我暂时不提供 PID；请先用 discover_io_target.py 自动寻找训练 PID 和数据路径。
候选不唯一时先让我确认，不要擅自选择。
确认后在训练活跃窗口采集 30 秒，运行确定性分析，终端给出结论，
并生成 agent_report.json 和自包含的 io_report.html。
不要执行 remount、drop_caches、readahead 或任何系统修改。'
```

检查 msAgent 是否实际完成以下流程，而不是只用语言猜测：

1. 调用 `discover_io_target.py`，得到候选 PID 和路径。
2. 调用 `collect_io_snapshot.py`，生成 `io_snapshot.json`。
3. 调用 `analyze_io_snapshot.py`，生成 `findings.json`。
4. Agent 根据结构化结论生成 `agent_report.json`。
5. 调用 `render_io_report.py`，生成 `io_report.html`。
6. 终端回答和 HTML 都说明证据、缺失信息、置信度与建议。

### 10.4 同窗口采集 `msprof`，验证 R500

R500 要回答的是：“Host IO 已经异常时，NPU 是否在同一时间因为等数据而空闲？”
因此 IO Snapshot 和 NPU Profile 必须覆盖同一次训练、同一设备、相互重叠的时间窗。

具体 `msprof` 启动参数取决于 CANN 和训练框架版本，应使用租用机器随附文档推荐的命令。
采集后至少保存：原始 Profile 目录、设备 ID、开始/结束时间、训练 PID、数据路径。

`summarize_msprof.py` 可读取真实 `op_summary_*.csv`：

```bash
python3 skills/mindstudio-storage-analysis/scripts/summarize_msprof.py \
  /path/to/msprof-output --device 0 -o op_summary_diagnostics.json
```

但它只提供 task gap 和 MTE2 的**辅助线索**，不能单独证明 R500。当前 Skill 对用户手写的
Profile JSON 最高只给中等置信度；要得到高置信 R500，还缺少“直接验证原始 profiler
artifact”的实现。因此本轮真机测试的正确验收是：

- 有真实同窗口 profiler 数据时，R500 能给出谨慎的候选结论；
- 没有 profiler 或时间窗不重叠时，R500 必须降级，不能声称存储导致 NPU 空闲；
- `op_summary_diagnostics.json` 不能直接作为 analyzer 的 `--profile` 输入。

用已有同窗口文件执行只读检查：

```bash
python3 skills/mindstudio-storage-analysis/evals/run_live_eval.py \
  --snapshot /path/to/io_snapshot.json \
  --profile /path/to/npu_metrics.json \
  --require-npu-runtime
```

### 10.5 多卡多 rank

用至少 2 张 NPU 启动训练，再重复 10.3。验收重点：

- 发现器找到 launcher、各 rank 和 DataLoader worker；
- PID 与路径映射属于当前训练，不把无关系统进程算进去；
- 只有多个活跃进程在同一受压设备和共同时间窗内争抢时，R400 才能高置信；
- 多 rank 但磁盘正常时不能误报 R400。

## 11. 真实 NFS 测试（有 NFS 时才做）

先验证目标数据确实位于 NFS：

```bash
findmnt -T /path/to/nfs-dataset
mountstats /path/to/nfs-dataset 2>/dev/null || cat /proc/self/mountstats
```

先跑正常 NFS 基线，再跑大量小文件数据集。通过 msAgent 使用与 10.3 相同的完整流程，
只是把目标路径换成 NFS 数据集。额外执行：

```bash
python3 skills/mindstudio-storage-analysis/evals/run_live_eval.py \
  --duration 30 \
  --pid <训练PID> \
  --path /path/to/nfs-dataset \
  --require-npu \
  --require-nfs
```

验收标准：

| 场景 | 预期 |
| --- | --- |
| 正常 NFS | 能采到目标挂载的 current-window mountstats，不因“NFS”三个字就报异常 |
| NFS 大量小文件 | R300 给出元数据或小 IO 线索，说明这仍可能只是候选原因 |
| NFS 高 RTT/重传/超时 | R200 引用真实 RTT、execute、retrans 或 timeout 证据 |
| 无关 NFS 流量 | 不能用另一挂载点的流量证明目标数据集异常 |

若要人为制造网络延迟或丢包，只能在私有、隔离的 NFS 测试网络上，由管理员确认影响范围、
原值、回滚命令后手工执行。不要让 msAgent 自动运行 `tc`、remount 或服务重启，也不要在
共享网卡上制造故障。

## 12. 每次测试必须保留的产物

建议每个场景使用独立目录：

```text
results/<date>-<scenario>/
├── msagent-session.txt
├── target_candidates.json
├── io_snapshot.json
├── findings.json
├── agent_report.json
├── io_report.html
├── op_summary_diagnostics.json    # 有 msprof 时
├── npu_metrics.json               # 有可信同窗口指标时
└── environment.txt
```

`environment.txt` 至少记录：Git commit、NPU 型号、驱动/固件、CANN、Python、`torch_npu`、
训练命令摘要、数据路径的 `findmnt` 输出、测试开始/结束时间。提交公开仓库前要脱敏主机名、
PID、内部路径和命令参数，不提交 API Key。

## 13. 最终验收表

租机测试结束后填写：

| 验收项 | 结果（通过/失败/跳过） | 证据路径 | 备注 |
| --- | --- | --- | --- |
| msAgent 连接 LLM |  |  |  |
| msAgent 自动加载 Skill |  |  |  |
| 自动发现 PID/数据路径 |  |  |  |
| Ascend ACL 冒烟 |  |  |  |
| 单卡正常训练无误报 |  |  |  |
| 单卡 IO 压力识别 |  |  |  |
| 真实 msprof 辅助摘要 |  |  |  |
| R500 同窗口谨慎判断 |  |  |  |
| 多卡多 rank 映射 |  |  |  |
| 真实 NFS 正常基线 |  |  |  |
| 真实 NFS 异常 |  |  |  |
| NFS 大量小文件 |  |  |  |
| Agent 终端建议 |  |  |  |
| 自包含 HTML 报告 |  |  |  |
| 危险操作未自动执行 |  |  |  |

只有“通过 msAgent 调用 Skill，并产生脚本证据、Agent 解释和 HTML”的场景，才能算完整
端到端通过。单独运行 `analyze_io_snapshot.py` 只能算规则层测试。
