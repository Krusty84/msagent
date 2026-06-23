# msagent 模型验证用例

## 目的

验证 msagent Profiler Agent 在真实场景下的分析能力和正确性，覆盖 **ascend-communication-analysis**、**ascend-computation-analysis**、**ascend-schedule-analysis**、**cluster-fast-slow-rank-detector**、**msprof-analyze-cli** 等 skill 的核心功能。

## 快速开始

```bash
# 1. 生成 mock profiling 数据（7个场景，单卡+集群）
python tests/model/generate_mock_data.py all --out tests/model/mock_data/

# 2. 运行所有验证用例
pytest tests/model/scenarios/test_scenarios.py -v

# 3. 运行特定场景
pytest tests/model/scenarios/test_scenarios.py -v -k "TC04"
```

## 目录结构

```
tests/model/
├── README.md                       # 本文件
├── generate_mock_data.py          # Mock 数据生成器
├── mock_data/                     # 生成的 mock profiling 数据
│   ├── clean/                     #   基线：无瓶颈
│   ├── good_overlap/              #   重叠良好：84% overlap
│   ├── poor_overlap_no_compute/   #   重叠差：<10% overlap
│   ├── host_bound/                #   Host Bound：Free Time >20%
│   ├── lane_degradation/          #   降Lane：NPU3→NPU7 7→3 lane
│   ├── wait_caused/               #   Wait等待：Rank 3 wait高
│   └── slow_rank/                 #   慢卡：Rank 7 compute 1.5x
└── scenarios/
    ├── __init__.py
    └── test_scenarios.py          # 18个测试用例
```

## Mock 数据说明

Mock 数据生成器按 Ascend PyTorch Profiler 的数据格式生成合成数据：

| 文件 | 内容 |
|------|------|
| `profiler_info_{rank}.json` | Profiler 元数据（level、设备数、框架版本） |
| `profiler_metadata.json` | 并行策略（TP/DP/PP 分组） |
| `step_trace_time.csv` | 每 step 的计算/通信/重叠/空闲/等待时间 |
| `op_statistic.csv` | 算子耗时统计（MatMul、FlashAttention等12个算子） |
| `kernel_details.csv` | Kernel 执行明细（100个kernel） |
| `api_statistic.csv` | CANN API 耗时统计 |
| `communication.json` | 通信算子详情（含带宽、等待时间） |
| `communication_matrix.json` | 链路矩阵（rank pair + lane 数 + 带宽） |
| `ascend_pytorch_profiler_{rank}.db` | SQLite DB（COMMUNICATION_OP、StepTraceTime等表） |
| `cluster_analysis.db` | 集群分析DB（ClusterCommunicationTime等4个表） |
| `trace_view.json` | Chrome trace 格式 Timeline |

## 测试用例清单

### TC01–TC03: 通信计算并行掩盖分析

| 编号 | 场景 | 输入 | 验证点 |
|------|------|------|--------|
| TC01 | 重叠良好 | step_trace: 84% overlap | Overlap ratio > 0.8 → exposed < 5% → 不需要深度分析 |
| TC02 | 重叠差-无计算可重叠 | step_trace: <10% overlap | Overlap ratio < 0.3 → 四大根因分类 → "No compute to overlap" |
| TC03 | 重叠差-带宽争抢 | 中等 overlap + 带宽下降 | 识别 compute-comm 带宽争抢模式 |

### TC04–TC05: 降Lane慢链路诊断

| 编号 | 场景 | 输入 | 验证点 |
|------|------|------|--------|
| TC04 | Lane降解检测 | matrix: NPU3→NPU7 3/7 lane, ~8 GB/s | Lane 数/带宽比例一致（3/7 ≈ 43%） |
| TC05 | Cluster DB一致性 | cluster_analysis.db | 4个必要表存在，SDMA 链路带宽离散度 |

### TC06–TC07: Wait诊断

| 编号 | 场景 | 输入 | 验证点 |
|------|------|------|--------|
| TC06 | Wait导致慢通信 | cluster DB: Rank 3 wait高 | wait_ratio > 15% → 分类为 wait-caused → 停止 |
| TC07 | 关键 rank 识别 | 同一 op 的跨 rank 时序 | 识别 longest/shortest/earliest/latest rank |

### TC08: 慢卡检测

| 编号 | 场景 | 输入 | 验证点 |
|------|------|------|--------|
| TC08 | 计算型慢卡 | Rank7 compute 1.5x Rank0 | 识别 Rank7 为慢卡，分类为 计算型慢卡 |

### TC09: Host Bound分析

| 编号 | 场景 | 输入 | 验证点 |
|------|------|------|--------|
| TC09 | Host下发瓶颈 | Free Time >15% step | 识别 Host Bound → 建议 dispatch/TASK_QUEUE_ENABLE |

### TC10–TC12: 端到端流水线

| 编号 | 场景 | 输入 | 验证点 |
|------|------|------|--------|
| TC10 | 通信分析全流水线 | cluster DB + SKILL.md + scripts | summarize.py → collect_wait_evidence.py → fault mode → report |
| TC11 | Overlap 集成 | SKILL.md | 四大根因完整存在 |
| TC12 | Output Contract | SKILL.md | 11个报告章节全部存在 |

### TC13–TC14: 参考文件完整性

| 编号 | 场景 | 验证点 |
|------|------|--------|
| TC13 | hccl_params.md | 含 HCCL_OP_EXPANSION_MODE、HCCL_BUFFSIZE、HCCL_DETERMINISTIC 等参数 |
| TC14 | lane_degradation.md | 含 hccn_tool、910B、7 lane、Recovery 等关键内容 |

### TC15: Agent 配置

| 编号 | 场景 | 验证点 |
|------|------|--------|
| TC15 | Skill注册 | Profiler.yml 中 ascend-communication-analysis 在 skills.patterns |

### TC16–TC18: Mock 数据完整性

| 编号 | 场景 | 验证点 |
|------|------|--------|
| TC16 | 必要文件 | 每个场景有 step_trace_time.csv、op_statistic.csv、profiler_info |
| TC17 | Cluster DB | lane_degradation/wait_caused/slow_rank 均有 cluster_analysis.db |
| TC18 | 数据合理性 | 所有时间值 ≥ 0 |

## 如何与真实 Agent 集成测试

Mock 数据可以直接作为 msagent 的输入路径使用：

```bash
# 启动 msagent，选择 Profiler agent
msagent

# 在会话中测试
"分析这个路径的 profiling 数据：tests/model/mock_data/lane_degradation/cluster_data"
"帮我看看通信隐藏率怎么样"
"这个集群有没有慢卡"
```

Agent 应该：
1. 调用 `get_skill("ascend-communication-analysis")` 读取 SKILL.md
2. 按照 SKILL.md 的 SOP 执行分析
3. 生成符合 Output Contract 的诊断报告

## 添加新的测试场景

```bash
# 1. 在 generate_mock_data.py 的 BOTTLENECK_SCENARIOS 添加配置
# 2. 重新生成数据
python tests/model/generate_mock_data.py all --out tests/model/mock_data/

# 3. 在 test_scenarios.py 添加验证用例
# 4. 运行验证
pytest tests/model/scenarios/test_scenarios.py -v
```
