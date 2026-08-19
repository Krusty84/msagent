# MSAgent 体验报告

## 一、报告基本信息

| 项目 | 内容 |
|------|------|
| 报告编号 | MeetUp-01-gcw_4v0NFXza-msagent性能体验报告 |
| 体验日期 | 2026-08-19 |
| Agent版本 | msAgent v26.1.0a2 |
| Profiling数据来源 | Qwen3-32B 4卡集群推理（设备651/654/657/664） |
| 测试目标 | 验证 msagent 辅助分析 profiling 数据中下发、调度相关问题的能力 |

---

## 二、交互记录

### 第 1 轮交互 — 环境验证

| 项目 | 内容 |
|------|------|
| 输入 Prompt | `msagent --version` |
| Agent 输出（文字摘要） | 返回 msAgent v26.1.0a2，启动后显示 Profiler Agent + 9 个 skills（ascend-cluster-fast-slow-rank-detector、ascend-communication-analysis、ascend-computation-analysis、ascend-msprof-analyze-cli、ascend-profiler-data-validation、ascend-profiler-db-explorer、ascend-schedule-analysis、github-raw-fetch、op-mfu-calculator） |
| **输出截图** | （见附件 msagent_welcome.png） |
| 是否符合预期 | ✅ 是 |
| 评价 | 环境搭建正常，Agent 类型识别正确（Profiler），skills 列表完整，覆盖集群快慢卡检测、通信分析、计算分析、调度分析等性能调优场景 |

### 第 2 轮交互 — 启动会话

| 项目 | 内容 |
|------|------|
| 输入 Prompt | `你好，请介绍一下你自己，当前有哪些能力，能做什么？` |
| Agent 输出（文字摘要） | 正常对话，介绍 Profiler Agent 能力，支持性能分析、精度调试、算子开发等场景 |
| **输出截图** | （见附件 msagent_hello.png） |
| 是否符合预期 | ✅ 是（首次发送报 Connection error，重试后正常） |
| 评价 | 会话启动正常，响应速度可接受。首次连接报错 `IndexError: pop from an empty deque` 是已知问题，已提交 Issue #218 |

### 第 3 轮交互 — 深度性能分析（核心）

| 项目 | 内容 |
|------|------|
| 输入 Prompt | `请分析 /workspace/user_data/ 目录下的 Profiling 数据，这是一个4卡集群（设备651、654、657、664）的Qwen3-32B推理数据。` |
| Agent 输出（文字摘要） | 执行完整的多轮分析，调用 30+ 次工具（msprof-analyze advisor、集群DB SQL查询、trace_view.json时间线解析、通信矩阵分析），输出最终报告包含5条核心结论： |
| | **结论1 [P0]**：通信时间=同步等待（Notify_Wait）主导，而非带宽。258次AllReduce累计425.9ms，传输仅42MB（~2.5ms即可传完） |
| | **结论2 [P0]**：通信与计算零重叠（99.98%未重叠），通信暴露448ms占31.5% |
| | **结论3 [P0]**：Host下发瓶颈+主动同步放大空闲。Event::synchronize 17次共384.6ms，空闲640ms占45% |
| | **结论4 [P1]**：无计算型快慢卡，但通信等待分配不均（rank0/3=425ms vs rank1/2=200-231ms） |
| | **结论5 [P2]**：通信配置质量问题（17个未对齐算子、动态shape、小包100%<16MB） |
| | 核心一句话：不是带宽/计算瓶颈，而是"小包全同步集体通信+零重叠+Host主动同步与下发开销"三者叠加，单步1414ms中约76%时间没有真正算力工作 |
| **输出截图** | （见附件 msagent_analysis_report.png） |
| 是否符合预期 | ✅ 是。经 MindStudio Insight 26.1.0 验证，Summary 页面数据（计算333.7ms/通信447.8ms/空闲632.4ms）与 msagent 结论完全一致 |
| 评价 | 分析深度足够，证据链完整，结论经独立工具验证准确。优化建议方向明确（合并小包、打开重叠、消除Host同步、融合算子） |

---

## 三、多轮交互整体评价

| 评价维度 | 评分（1~5） | 说明 |
|----------|-------------|------|
| 问题理解准确性 | 5 | 准确识别4卡集群推理场景，自动选择Profiler Agent，分析方向正确 |
| 数据分析深度 | 5 | 从全局诊断→集群通信→Rank下钻→Timeline交叉验证，层次清晰，每个结论都有数据支撑 |
| 证据链条完整性 | 5 | kernel耗时、传输量、Notify_Wait次数、Event::synchronize分布等多维度证据交叉验证 |
| 优化建议实用性 | 4 | 建议方向明确（合并通信、打开重叠、消除同步、融合算子），但部分需结合vLLM侧代码进一步确认 |
| 多轮对话连贯性 | 4 | 单轮深度分析能力强，自动调用多工具完成全链路分析 |
| 响应速度与稳定性 | 3 | 首次连接报错需重试；完整分析耗时5-10分钟，期间无进度提示 |
| 整体满意度 | 4.5 | 核心分析能力优秀，结论准确，体验细节（稳定性、进度提示）有待提升 |

---

## 四、问题与改进建议

### 发现的问题

1. **[Bug #218] 首次启动报 Connection error**：`IndexError: pop from an empty deque`，重试后正常，疑似初始化时序问题
2. **[Bug #219] msprof-analyze advisor CWD 依赖**：在数据目录下执行报 `Failed to make directory: log`，需切换到/tmp执行
3. **[Enhancement #220] 欢迎页 Model 显示不一致**：配置deepseek-v4-flash但显示gpt-4o-mini
4. **[Enhancement #221] 长时分析无进度反馈**：5-10分钟分析过程仅显示工具调用，无整体进度百分比或预计剩余时间

### 改进建议

1. 优化首次连接稳定性，避免 `pop from an empty deque` 错误
2. msprof-analyze advisor 自动处理工作目录，不依赖 CWD
3. 欢迎页正确显示实际配置的模型名称
4. 增加分析过程的实时进度提示（当前阶段、进度百分比、预计剩余时间）
5. 分析报告支持一键导出为 Markdown/PDF 格式
