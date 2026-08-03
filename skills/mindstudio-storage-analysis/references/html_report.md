# HTML 诊断报告

`render_io_report.py` 将机器可读产物和 Agent 解释合并成一个离线 HTML。它只负责展示，不重新执行 R000-R500，也不执行任何优化建议。

## 输入

| 参数 | 必需 | 来源 |
|---|---|---|
| `--snapshot` | 是 | `collect_io_snapshot.py` 输出的 `io_snapshot.json` |
| `--findings` | 是 | `analyze_io_snapshot.py` 输出的 `findings.json` |
| `--targets` | 否 | `discover_io_target.py` 输出的 `target_candidates.json`；用户已明确目标时可省略 |
| `--msprof` | 否 | `summarize_msprof.py` 输出的非认证诊断摘要 |
| `--agent-report` | 否 | Agent 生成的总结与建议 JSON |
| `--output` | 是 | 最终单文件 HTML 路径 |

Agent 报告格式：

```json
{
  "summary": "分析范围：<对象与窗口>。结论：<一句到两句核心结论>。证据：1) <证据一>；2) <证据二>。综合判断：<当前能证明和不能证明的事情>。",
  "recommendations": [
    {
      "priority": "high",
      "title": "建议标题",
      "detail": "建议内容和验证方式。",
      "source_rule_ids": ["R100", "R400"],
      "requires_confirmation": true
    }
  ],
  "limitations": ["缺少同窗 NPU profiler 时间线。"]
}
```

- `priority` 只接受 `high`、`medium`、`low`、`info`。
- `source_rule_ids` 只接受 R000-R500，用于说明建议依据，不允许 Agent 伪造新规则。
- `requires_confirmation=true` 表示建议涉及 workload 或系统变更，仍须遵守 `SKILL.md` 的安全门禁。
- 所有 Agent 文本按纯文本转义，不执行 Markdown、HTML 或 JavaScript。
- `summary` 使用“分析范围 / 结论 / 编号证据 / 综合判断”的紧凑结构，不写无法扫描的长段落。渲染器只按这些标记分段，不改写 Agent 原文；旧格式仍以单段结论兼容展示。

## 输出内容

报告包含：目标与时间窗口、产物流水线、Agent 总结、R000-R500 结论、本地磁盘表格、GlusterFS FUSE 主网络存储活动表、辅助 NFS 表格、进程 IO 表格、最终训练目标摘要、msprof 辅助线索、provider 状态和输入文件 SHA-256。最终 HTML 只展示已选程序、PID、数据集路径、文件系统和必要命令摘要，不展开其他候选或候选分数；完整候选仍保留在 `target_candidates.json`，需要确认时由 Agent 在交互中展示。GlusterFS 表中的进程树计数只表示活动与小读取候选，不表示 client/brick 延迟。

HTML 不使用外部字体、图片、JavaScript 或前端框架，可离线打开和浏览器打印。它可能包含主机名、PID、命令摘要和数据路径，对外分享前需要人工确认脱敏范围。

## 纯语言模型调用

模型不需要生成页面代码，也不需要识别图片。它只需：

1. 读取 `findings.json` 并生成上述 `agent_report.json`。
2. 调用固定的 `render_io_report.py`。
3. 检查命令成功且 HTML 文件存在。

多模态能力只在开发模板或人工验收视觉效果时有帮助，不是生产生成流程的依赖。
