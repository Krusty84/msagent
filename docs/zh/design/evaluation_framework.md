# 通用 Agent 评测框架设计

本文档定义一套面向 `msagent`、`opencode` 等 agent 的通用评测框架。目标不是统一底层存储结构，而是统一评测输入输出与指标口径，使不同 agent 可以在同一套 benchmark 下横向对比。

## 设计目标

- 兼容不同 agent 的原始运行记录格式
- 支持同一 agent 的版本前后对比
- 支持不同 agent 间横向对比
- 将 profiling 任务视为一个可插拔任务域，而不是整套框架的唯一中心

## 分层模型

统一评测拆成四层：

1. `Task`
   定义任务内容、输入、约束与期望输出。
2. `Run`
   某个 agent/model 对一个 task 的一次实际执行。
3. `Trace IR`
   将不同 agent 的原始日志、checkpoint、数据库、事件流统一映射到同一种中间表示。
4. `Evaluation`
   基于统一 Trace IR 计算结果质量、过程效率与稳定性。

关键原则：

- 统一 `IR`，不统一底层存储
- 统一指标接口，不统一所有任务域的判分细节
- 任务域可插拔，agent exporter 可扩展

## 统一 Trace IR

统一 Trace IR 是框架锚点。`msagent` 与 `opencode` 都应先导出到该结构，再进入后续指标计算与质量判分。

### 1. Run 元数据

```json
{
  "run_id": "unique-run-id",
  "task_id": "task-001",
  "agent_name": "msagent",
  "agent_version": "x.y.z",
  "model_name": "deepseek-v4-flash",
  "dataset_name": "profiling-basic",
  "started_at": "2026-06-16T07:44:51Z",
  "finished_at": "2026-06-16T07:45:20Z",
  "duration_ms": 29000
}
```

### 2. 输入与输出

```json
{
  "input": {
    "prompt": "...",
    "attachments": [],
    "context": {}
  },
  "output": {
    "final_answer": "...",
    "status": "completed",
    "finish_reason": "stop"
  }
}
```

### 3. 对话轨迹

```json
{
  "messages": [
    {
      "id": "m1",
      "role": "user",
      "content": "...",
      "timestamp": "..."
    },
    {
      "id": "m2",
      "role": "assistant",
      "content": "...",
      "reasoning": "...",
      "tool_calls": ["t1", "t2"],
      "timestamp": "...",
      "usage": {
        "input_tokens": 100,
        "output_tokens": 50,
        "reasoning_tokens": 20,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0
      }
    }
  ]
}
```

### 4. 工具轨迹

```json
{
  "tool_calls": [
    {
      "id": "t1",
      "name": "execute_sql",
      "args": {
        "query": "SELECT ..."
      },
      "result": "...",
      "status": "success",
      "started_at": null,
      "finished_at": null,
      "duration_ms": null
    }
  ]
}
```

### 5. 运行状态

```json
{
  "state": {
    "todos": [],
    "files": {},
    "summaries": [],
    "interrupts": []
  }
}
```

### 6. 聚合统计

```json
{
  "stats": {
    "message_count": 0,
    "user_message_count": 0,
    "assistant_message_count": 0,
    "tool_call_count": 0,
    "tool_success_count": 0,
    "tool_failed_count": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "reasoning_tokens": 0,
    "cache_read_tokens": 0
  }
}
```

## 字段兼容策略

为兼容不同 agent，字段分为三类：

### 通用必选字段

- `task_id`
- `run_id`
- `agent_name`
- `model_name`
- `prompt`
- `final_answer`
- `messages`
- `tool_calls`
- `tool_success_count`
- `tool_failed_count`
- `input_tokens`
- `output_tokens`
- `started_at` / `finished_at` 或 `duration_ms`

### 通用可选字段

- `reasoning`
- `reasoning_tokens`
- `cache_read_tokens`
- `tool_duration_ms`
- `todos`
- `interrupts`
- `summary/compression events`

### agent 扩展字段

不同 agent 的特有字段进入 `extensions`，避免污染通用 schema。

```json
{
  "extensions": {
    "msagent": {},
    "opencode": {}
  }
}
```

## 指标体系

指标体系不绑定 profiling，应适用于任意任务域。

### 1. Outcome

评最终任务是否完成。

- `success_rate`
- `partial_success_rate`
- `failure_rate`

单次 run 推荐标签：

- `success`
- `partial`
- `failure`
- `timeout`
- `tool_error`
- `invalid_output`

### 2. Quality

评答案质量。推荐统一四个维度：

- `correctness`
- `completeness`
- `evidence`
- `clarity`

不同任务域可用不同 evaluator 计算这些分数，但输出接口保持一致。

### 3. Efficiency

评执行代价与路径长度。

- `duration_ms`
- `message_count`
- `assistant_turns`
- `tool_call_count`
- `tool_success_rate`
- `input_tokens`
- `output_tokens`
- `reasoning_tokens`
- `cache_read_tokens`

### 4. Robustness

评多次运行的一致性与恢复能力。

- `rerun_success_rate`
- `score_variance`
- `tool_path_variance`
- `token_variance`
- `error_recovery_rate`

## 统一评分接口

统一评分输出不依赖具体任务域。

```json
{
  "label": "success",
  "overall_score": 0.86,
  "subscores": {
    "correctness": 0.9,
    "completeness": 0.8,
    "evidence": 0.7,
    "clarity": 1.0
  }
}
```

上层分数字段固定，下层 evaluator 可按任务域实现。

## 通用 Task Schema

任务定义不应服务单一任务域。

```json
{
  "task_id": "profiling_dispatch_001",
  "domain": "profiling",
  "category": "analysis",
  "prompt": "...",
  "inputs": {
    "workspace": "...",
    "artifacts": []
  },
  "expected_output": {
    "type": "structured"
  },
  "evaluation": {
    "evaluator": "profiling_dispatch_chain",
    "success_threshold": 0.8
  },
  "tags": ["profiling", "dispatch", "cross-layer"]
}
```

这里与具体任务域绑定的是 `domain` 与 `evaluation.evaluator`，不是整个 benchmark 框架。

## Profiling 作为任务域适配器

profiling 不应主导整个框架，而应作为 `domain=profiling` 的一组 evaluator 存在。建议先定义以下适配器：

- `profiling_topk_aggregates`
- `profiling_shape_comparison`
- `profiling_comm_summary`
- `profiling_anomaly_detection`
- `profiling_cross_layer_chain`

这些 evaluator 共享统一 Trace IR 和统一评分接口，只在领域判分细节上不同。

## 待完成部分的通用化定义

当前需要推进的三部分，应按通用框架重新定义。

### 1. 统一统计提取

目标不是只补 `msagent profiling` 指标，而是实现 `run-level unified metrics extraction`。

首批建议统一提取：

- `duration_ms`
- `message_count`
- `assistant_turns`
- `tool_call_count`
- `tool_success_count`
- `tool_failed_count`
- `input_tokens`
- `output_tokens`
- `reasoning_tokens`
- `cache_read_tokens`
- `final_status`

### 2. Trace Exporter

目标是将原始运行记录导出为统一 Trace IR。

建议目录：

```text
benchmark/
  exporters/
    msagent_exporter.py
    opencode_exporter.py
  ir/
    trace_schema.json
```

### 3. Domain-aware Evaluation

目标是基于统一 Trace IR、Task 定义与 Gold 数据做任务域感知判分。

- 输入：`trace + task spec + gold`
- 输出：`score object`

profiling 只是其中一个 domain adapter。

## 建议目录结构

```text
benchmark/
  tasks/
    profiling/
    code/
    retrieval/
  gold/
    profiling/
  ir/
    trace_schema.json
    score_schema.json
  exporters/
    msagent_exporter.py
    opencode_exporter.py
  evaluators/
    base.py
    profiling/
      topk.py
      comm_summary.py
      dispatch_chain.py
  metrics/
    aggregate.py
    compare.py
  reports/
    leaderboard.py
```

## 实施顺序

建议按以下顺序落地：

1. 定义统一 `Trace IR`
2. 实现 `msagent exporter`
3. 实现 `opencode exporter`
4. 实现统一基础指标抽取
5. 在 `profiling` 域下先落 3 到 5 个 evaluator
6. 生成横向对比报表

## 核心原则

- 统一 IR，而不是统一底层存储
- 统一指标接口，而不是统一所有任务判分细节
- 任务域可插拔，而不是 profiling 特化到底
- exporter 可扩展，而不是只围绕某一个 agent
