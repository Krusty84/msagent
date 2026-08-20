# standing_high 调优策略

基础摸高策略，通过二分搜索找到最小回退，再逐步减少回退提升量化覆盖率。


---

## 算法流程

```
1. 零回退测试
   ├─ 生成无回退层的 Practice → 量化 → 评测
   └─ 若精度达标 → 直接返回（最优结果）

2. 二分搜索最小回退层数
   ├─ 搜索范围 [1, max_layers]
   ├─ 每次取 mid 层回退 → 量化 → 评测
   ├─ 精度达标 → 缩小上界
   └─ 精度不达标 → 增大下界
   → 得到 init_disable_level

```

---

## 关键配置项

| 配置项 | 说明 |
|--------|------|
| **完整量化配置模板** | 包含线性层量化参数、保存配置等完整 Practice YAML |

---

## 使用该策略的最小输入

| 必要输入 | 产出 |
|----------|------|
| 模型、量化配置模板 | 多轮 Practice YAML |

## 适用范围与配置边界

本策略不依赖模型模态；所有受支持的模型共用上述二分搜索和摸高算法，具体配置结构由基准 Practice 决定。

每轮 Practice 必须继承基准 Practice 的 `apiversion`、`include`、静态 `exclude` 及其他 schema 专属字段。策略只能调整 `tuning_exclude`，最终写入的 `exclude` 为静态排除项与 `tuning_exclude` 的并集。

具体 YAML 字段和量化边界见 [量化配置格式](practice_yaml_format.md)，敏感层排序规则见 [敏感层分析](sensitive_layer_analysis.md)。
