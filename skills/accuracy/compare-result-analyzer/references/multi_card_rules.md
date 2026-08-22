# 多卡汇总分析规则

> **用途**: 本文件定义 Agent 侧执行的多卡汇总分析规则。当用户提供多张卡的比对文件时，
> Agent SHALL 对每张卡独立运行 `analyze_stat.py --format json`，然后 Read 本文件并按规则
> 执行跨卡聚合，最终按 `assets/multi_card_report_template.md` 模板生成多卡汇总报告。
>
> **数据来源**: 每张卡的 per-card JSON（`analyze_stat.py --format json` 输出）。
> 阈值取第一张卡的 `auto_threshold.threshold`（所有卡使用同一个级联阈值）。
>
> **本文件替代已删除的 `scripts/analyze_multi.py`**。

---

## M-001: 数据加载

Agent SHALL 读取每张卡的 JSON 结果文件，从以下字段提取数据：

| 字段 | 用途 |
|------|------|
| `propagation.root_cause[]` | 跨卡根因检测，最差卡/趋势判定 |
| `first_point.confirmed` | 首问题点对齐 |
| `output_nodes[]` | NRE 查找（共识回溯时查找某 prefix 在某卡上的 NRE） |
| `auto_threshold.threshold` | 统一阈值（取第一张卡） |
| `amplifier_candidates[]` | Backward amplifier 共识源 1 |
| `top_root_causes.{direction}[]` | Backward amplifier 共识源 2 |
| `fb_association_candidates[]` | FB 置信度分布 |
| `spike_indicators` | 场景标记聚合 |

---

## M-002: 跨卡共识根因检测

```
算法:
  key_cards = {}  # (prefix, direction) → set of card labels
  for each card in cards:
      for rc in card.propagation.root_cause:
          key = (rc.prefix, rc.direction)
          key_cards[key].add(card.label)

  common = [
      {prefix, direction, cards: [...], max_nre: max(rc.output_nre across cards)}
      for key, card_set in key_cards.items()
      if len(card_set) >= 2
  ]
  sort common by max_nre descending
```

**对齐键**: 精确字符串 `(prefix, direction)`。前缀由 `_common.extract_op_prefix` 完成
`parameters_grad → backward` 归一化，Agent 无需额外处理。

---

## M-003: 卡特定根因

```
算法:
  card_specific[key] = {
      for key, card_set in key_cards.items()
      if len(card_set) == 1:
          append {prefix, direction, card, jump} to card_specific[card]
  }
  for each card: sort by |jump| descending
```

---

## M-004: 首问题点对齐

逐卡比较 `first_point.confirmed.prefix`:

```
same_op = all cards share the same prefix string
by_card = {card_label: first_point.prefix (or None)}
```

---

## M-005: 接近阈值告警

NRE 在区间 `[0.5 × threshold, threshold)` 内：

**类型 1 — `own_first_point`**:
```
for each card:
    fp_nre = card.first_point.confirmed.nre
    if fp_nre >= 0.5 * threshold and fp_nre < threshold:
        add warning {type: "own_first_point", card, nre: fp_nre, threshold_ratio}
```

**类型 2 — `consensus_candidate`**:
```
对每个共识 prefix（出现在 ≥2 卡的首问题点中）:
    对非共识卡:
        nre = 查找 prefix 在该卡上的 output NRE
              查找顺序: (1) output_nodes 中 name.startswith(prefix + '.')
                       (2) propagation.root_cause fallback
        if nre in [0.5*threshold, threshold):
            add warning {type: "consensus_candidate", prefix, card, nre, threshold_ratio}
```

排序: `threshold_ratio` 升序（越接近阈值越靠前）。

---

## M-006: 共识回溯

当各卡首问题点 `same_op == false` 时:

```
找出所有出现在 ≥2 张卡首问题点中的 prefix:
    consensus = prefix → {cards_where_it_is_first_point}
    按 consensus_count 降序

对每个共识 prefix:
    对非共识卡:
        记录 non_consensus_card_nres: {card_label, nre, threshold_ratio}
        标记 near_threshold_cards (nre 在 [0.5*T, T) 中的卡)
```

---

## M-007: Backward Amplifier 共识

聚合两个来源:

**来源 1**: `amplifier_candidates[]` 中 `direction == 'backward'` 的条目
**来源 2**: `top_root_causes.backward[]` 中 `is_true_root_cause_feature == true` 的条目

```
for prefix in (来源1 ∪ 来源2):
    cards_with = [cards where this prefix appears]
    if len(cards_with) >= 2:
        add to backward_amplifier_consensus
按 consensus_count 降序
```

---

## M-008: FB Confidence 分布

逐卡 tally `fb_association_candidates[]`（读取脚本预计算的 `confidence` 字段，无需自行判定）：

```
total_counts = {high: N, medium: N, low: N}   # 按 confidence 字段计数
high_confidence_items = [items where confidence == 'high']
```

Agent 按 `confidence` 字段直接 tally 计数。

---

## M-009: Scenario Flags 聚合

读取每张卡的 `spike_indicators`:

```
grad_norm_spike_cards = [cards where spike_indicators.spike_condition_met == true]
aggregated = {
    grad_norm_spike: len(grad_norm_spike_cards) > 0,
    spike_card_count: len(grad_norm_spike_cards),
    spike_cards: [card labels]
}
```

---

## M-010: 最差卡与趋势判定

```
最差卡 = argmax(各卡 propagation.root_cause 数量)

趋势:
    if 所有卡的 root_cause 总数 <= 1:
        trend = 'consistent'
    elif max_rc > 0.5 * total_rc:
        trend = 'one_card_worse'
    else:
        trend = 'diverging'
```

---

## 多卡报告生成

Agent SHALL 在完成上述聚合后，按 `assets/multi_card_report_template.md` 模板生成报告。

报告需逐卡列出:
- 首问题点 (prefix, NRE, row_range)
- ROOT CAUSE 计数
- 场景标记 (spike_indicators)

跨卡部分包括:
- 共识根因（M-002）
- 卡特定根因（M-003）
- 首点对齐（M-004）
- 接近阈值告警（M-005）
- 最差卡与趋势（M-010）
- 数据覆盖缺口（逐卡独立列出, 跨卡共有缺口优先排查）
