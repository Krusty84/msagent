# 阈值标准参考

> **用途**: 本文件定义 compare-result-analyzer skill 的自适应阈值算法和 MeanBias 判定标准。分析步骤 1（阈值确定）时，agent 必须先 Read 本文件。

---

## 自适应阈值（默认推荐）

skill 默认使用 **自适应阈值级联检测**（序列变点 → 锚定回溯 → Delta-NRE 离群 → 分布间隙 → 统计兜底），自动从数据分布中确定最优 NRE 阈值，无需用户手动指定。

### 级联算法

```
SICD 序列变点检测 ──第一优先级──▶ 找到变点 → 返回阈值
    │ 在 log(NRE+1) 空间扫描执行序列
    │ 找第一个结构断裂点（"哪里开始出问题"）
    │ 零/近零基线快速通道
    ▼ 未找到变点
锚定回溯 (AnchoredBacktrack) ──第二优先级──▶ 可靠 → 返回阈值
    │ Top-5 最差节点回溯 300 行
    │ ratio ≥ 5 跳变检测
    │ p25 过滤传播链内部跳变
    ▼ 不可靠
Delta-NRE 离群 (DeltaNREOutlier) ──第三优先级──▶ 有离群 → 返回阈值
    │ IQR 离群检测 (需要 ≥30 op_groups)
    │ upper_fence = Q3 + 3×IQR
    ▼ 无离群/样本不足
分布间隙 (DistributionGap) ──第四优先级──▶ 有显著间隙 → 返回阈值
    │ 三守卫过滤
    │ threshold = gap_lower × 2.0
    ▼ 无显著间隙
统计兜底 (StatisticalFallback)
    threshold = max(p50 × 3, 0.1%)
```

> **分段预处理**：级联前先执行分段检测（dtype 变化 / NRE ≥10× 跃迁为断点，shape 变化不参与），每段独立运行级联，全局阈值 = min(各段有效阈值)；全局阈值 > 5% 时触发低信号回退。详见 `references/constraints.md` C-ANALYSIS-017。

### 级联输出

除阈值外，级联同时返回：
- **method**: 实际使用的检测方法
- **confidence**: 置信度 (`high` / `medium` / `low`)
- **stats**: 统计量 (`noise_ceiling`, `p25`, `p50`, `p01`)，供后续全自适应最大跳变补充使用

### SICD 序列变点检测的核心优势

序列变点检测作为第一优先级的原因：
1. **天然区分噪声基线与信号** — 在 log 空间做序列扫描，不受绝对 NRE 尺度影响
2. **只找"第一个"结构断裂** — 正是分析师手动寻找的"哪里开始出问题"
3. **确认窗口防止单点误判** — 要求变点后连续 3 点也显著偏高
4. **近零基线快速通道** — 当前面节点全近零 NRE 时，第一个非零即是变点
5. **多窗口 SICD 变体** — 在全局 SICD 基础上增加滑动窗口（200/500/1000），每个窗口独立检测局部跳变。取 min(全局阈值, 所有窗口有效阈值) 作为最终阈值，防止全局窗口被远端大幅振荡拉高而漏检前段早期小幅跳变

### 多窗口 SICD 机制

**问题**: 全局 SICD 在全序列上只找一个断点。当序列前段噪声近零、后段出现大幅振荡时，断点落在前后分界处，阈值被后段噪声基线拉高。前段幅度虽小但语义重要的早期跳变被漏检。

**解决方案**: 滑动窗口变体——小窗口（200）只看局部短序列，噪声基线近零，小幅跳变即可触发，不受远端大幅振荡影响。大窗口（500/1000）提供中间粒度，平衡局部敏感性与统计稳定性。

- 窗口大小: `[200, 500, 1000]`，步长为窗口大小的 50%
- 有效性判定: 窗口内节点数 ≥ 50 且 NRE 标准差 > 0
- 聚合策略: `final = min(global_threshold, min(all_valid_window_thresholds))`
- 对所有已有合适阈值的 case，多窗口不会产生更低的假阳性阈值（窗口内噪声基线足够低时，SICD 给出的阈值不会低于该窗口的噪声上限）
- stats 中记录 `multi_window_applied`、`multi_window_threshold`、`multi_window_size`

---

## MeanBias 定义与判定

```
MeanBias = |Mean diff| / Bench L2norm
```

- `Mean diff`：NPU tensor 与 Bench tensor 分别计算均值后的差值（`Mean diff = mean(NPU tensor) − mean(Bench tensor)`），来自比对 CSV 的 `Mean diff` 列。CSV 中所有数值列均为 tensor 级聚合统计量，非逐元素比较结果。
- `Bench L2norm`：Bench tensor 的 L2 范数，来自比对 CSV 的 `Bench l2norm` 列。
- MeanBias 为**原始比率**（0 ~ +inf），转换为百分比时乘以 100。
- MeanBias **不是原始比对 CSV 的列**，由分析脚本根据 `Mean diff` 和 `Bench l2norm` 计算得出。

**判定系数 α**：
- 系数 **α = 1.2**，表示允许的 bias 漂移占整体幅值变化的比例。
- MeanBias 的补充阈值为 `α × 阈值`（阈值由自适应级联检测确定，无需用户指定；例如阈值为 1%，则 MeanBias >= 1.2% 时触发补充判定）。
- **α 设定依据**：MeanBias 与 NormRelativeErr 在正常数据中通常处于同一量级，α ≈ 1.2 可在保证检测整体偏置的同时，降低因统计波动导致的误报。

**NRE 与 MeanBias 的关系**：
- 仅当 NRE < 阈值时，才检查 MeanBias 补充。NRE 已超标的节点直接判定为"不可忽略"，不再依赖 MeanBias。
- MeanBias 补充主要捕获**整体性偏移**——衡量两个 tensor 的整体性偏移程度，与 NRE 互补。
- 报告中所有"需关注"、"不可忽略"、"问题节点"等判断，均基于此双层标准。
