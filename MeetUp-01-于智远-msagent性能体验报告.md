# MSAgent 体验报告
## 一、报告基本信息
| 项目 | 内容 |
|------|------|
| 报告编号 | MS-MEETUP-20260624-于智远-01 |
| 体验日期 | 2026-07-23 |
| Agent版本 | msAgent v26.0.0 |
| Profiling数据来源 | /workspace/yuzy/prof_data |
| 测试目标 | 使用msagent分析Profiling数据中的下发、调度类问题 |

---

## 二、交互记录
### 第 1 轮交互
| 项目 | 内容 |
|------|------|
| 输入 Prompt | 从当前Profiling数据来看，有无下发瓶颈，有哪些类型的下发问题，给出证据和优化建议 |
| Agent 输出（文字摘要） | Agent判断存在三类下发瓶颈：Rank 3 Host下发阻塞、全局下发同步延迟过高、AI Core调度延迟与Free Time过高。Agent给出了Free Time、Communication、NOTIFY_WAIT、EVENT_WAIT、CAPTURE_WAIT、AI_CORE调度延迟、同步API和GetWorkspaceSize长尾等证据，并提出CPU绑核、合并通信梯度、开启通信计算Overlap、排查GetWorkspaceSize长尾、Stream深度优化和NUMA亲和性等建议。 |
| **输出截图** | 见《MeetUp-01-于智远-issue清单-01.md》IMG-01至IMG-05 |
| 是否符合预期 | 基本符合 |
| 评价 | 能够识别多类下发调度问题，并给出数据证据、优化建议和验证方法。部分指标的统计口径和因果依据需要进一步说明。 |

*（根据实际对话轮次自行增删）*
---

## 三、多轮交互整体评价
| 评价维度 | 评分（1~5） | 说明 |
|----------|-------------|------|
| 问题理解准确性 | 5 | 准确理解下发瓶颈分析需求。 |
| 数据分析深度 | 5 | 分析了Host下发、同步等待、AI Core调度和长尾API。 |
| 证据链条完整性 | 4 | 给出了多类指标，但部分指标的统计范围和口径未明确。 |
| 优化建议实用性 | 5 | 给出了CPU绑核、通信合并、Overlap、Stream和NUMA等建议。 |
| 多轮对话连贯性 | 5 | 能够结合快慢卡分析结果继续分析。 |
| 响应速度与稳定性 | 5 | 对话过程正常，最终完成分析。 |
| 整体满意度 | 4 | 完成下发瓶颈分类、证据分析和优化建议输出。 |

---
## 四、问题与改进建议
### 发现的问题
1. NOTIFY_WAIT、EVENT_WAIT、CAPTURE_WAIT和AI_CORE调度延迟的指标来源、统计范围和时间口径未明确。
2. 各类累计耗时与Stage总时间、Free Time之间是否存在重叠未说明。
3. 小包AllReduce与同步等待、Free Time之间的因果关系缺少对应关系说明。
### 改进建议
1. 在指标表中补充Rank、Step、数据文件、时间单位和统计口径。
2. 说明各类累计耗时是否可以直接相加，以及是否与其他时间指标重叠。
3. 将观测数据、分析推断、优化建议和复测指标分别展示。

快慢卡分析：

Agent判断Rank 3存在Host下发阻塞。Rank 3的Free Time占比为70.7%，其他Rank为53%~55%；Rank 3 Communication为2.24ms，其他Rank为5.15~5.54ms。Agent建议对4个Rank进行CPU绑核并检查NUMA亲和性，优化后重新采集Profiling数据，验证Rank 3的Free Time是否下降至与其他Rank一致。

Agent进一步判断存在全局下发同步延迟和AI Core调度延迟问题。Overlap Analysis中Free占比为43.98%，Computing占比为12.22%；NOTIFY_WAIT共10,414次，平均延迟671.6μs；EVENT_WAIT共2,515次，平均延迟244.0μs；AI_CORE调度共51,800次，平均调度延迟119.8μs。Agent建议减少细粒度通信、开启通信计算Overlap，并排查GetWorkspaceSize长尾。优化后可重新采集Profiling数据，观察NOTIFY_WAIT次数、Free占比和同步API最大耗时是否下降。


