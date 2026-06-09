# 主 Agent ↔ Subagent 交互协议（MSAGENT_IO v1）

编排层通过 deepagents `task` 工具委派 subagent。`task.description` 与 subagent 最终回传内容须使用统一机器可读块，便于审计与复盘。

## 围栏格式

委派（写入 `task.description`）与回传（subagent 最终回复）均须包含**且仅包含一个**块：

````markdown
```msagent-io v1
{ ... JSON ... }
```
````

- 块外最多 3 行人类可读摘要
- 禁止在块外写长参数列表、SKILL 全文、完整 YAML/日志正文
- JSON 须可解析；字段名与 [量化配置调优](./quantization_tuning.md) 中各 subagent 字段表一致

## 委派 JSON 骨架（主 Agent 写入 task.description）

```json
{
  "protocol": "msagent.subagent_io",
  "version": "1",
  "subagent_type": "<与 task 参数 subagent_type 一致>",
  "input": { }
}
```

## 回传 JSON 骨架（Subagent 最终回复）

成功：

```json
{
  "protocol": "msagent.subagent_io",
  "version": "1",
  "subagent_type": "<本 subagent 名称>",
  "status": "ok",
  "output": { }
}
```

失败：

```json
{
  "protocol": "msagent.subagent_io",
  "version": "1",
  "subagent_type": "<本 subagent 名称>",
  "status": "failed",
  "error": {
    "code": "UNKNOWN_ERROR",
    "message": "简短错误描述"
  }
}
```

## 正例（practice-generator 委派）

````markdown
生成 Round 1 practice 配置。

```msagent-io v1
{
  "protocol": "msagent.subagent_io",
  "version": "1",
  "subagent_type": "quant-tuning-practice-generator",
  "input": {
    "model_type": "Qwen3-8B",
    "model_path": "/data/models/Qwen3-8B/",
    "save_path": "/path/to/record/",
    "device": "npu:2,3",
    "strategy": "standing_high",
    "max_iterations": 10,
    "round": 1,
    "prev_result": null,
    "anchor_practice": null
  }
}
```
````

## 反例

- 整段自然语言参数列表、无 `msagent-io` 块
- 块内缺少必填字段（如 `round`、`save_path`）
- 回传只有 Markdown 表格、无 `msagent-io` 块
- 在 `output` 中粘贴完整 Practice YAML 正文

## 编排脚本（非 subagent）

`accuracy_lookup`、`history_clear`、`history_append`、`accuracy_append` 由主 Agent 在本会话 `execute` 对应脚本，**不得**通过 `task` 委派 subagent。

## 审计日志

运行时审计写入 `{working_dir}/.msagent/audit_log/{Agent}_{thread_id}.jsonl`。审计事件与 MSAGENT_IO **独立**，同一 `run_id` 串联一轮用户交互。

### `user.turn`（新一轮用户输入）

主 prompt 发起的一次完整用户消息，开启新 `run_id`：

```json
{
  "agent_name": "Auto-tuning",
  "event": "user.turn",
  "run_id": "...",
  "start_time": "2026-06-09 11:41:31",
  "prompt": "请确认 base_info 配置是否无误，回复后继续。",
  "message": "确认无误"
}
```

`prompt`（可选）：本轮用户输入前，checkpoint 中最后一条主 Agent 助手消息；首轮任务时通常省略。

### `user.response`（执行中回答 / 审批）

同一次 `run_id` 内，用户对 interrupt 的答复（审批或选项）：

```json
{
  "agent_name": "Auto-tuning",
  "event": "user.response",
  "run_id": "...",
  "start_time": "2026-06-09 11:45:02",
  "kind": "approval",
  "prompt": "Tool: execute\nArgs: {...}",
  "options": ["approve", "reject", "always_approve", "always_reject"],
  "response": "reject",
  "context": { "interrupt_id": "...", "tool_name": "execute" }
}
```

`kind`：`approval`（HITL 工具审批）或 `choice`（question/options）。多工具一次审批时 `response` 为 `[{tool_name, decision}, ...]`，`context.batch=true`。

### `subagent.delegation`（subagent 委派）

`input` / `output` 直接来自 MSAGENT_IO 解析结果，不做额外字段转换。

| 审计字段 | 说明 |
|----------|------|
| `start_time` | 主 Agent 发起 `task` 委派时刻 |
| `end_time` | `task` 工具返回时刻 |
| `duration_ms` | 两者间隔（毫秒） |
| `input` / `output` | 从 `msagent-io` 块解析出的结构化数据 |
| `input_valid` / `output_valid` | 协议校验是否通过 |
