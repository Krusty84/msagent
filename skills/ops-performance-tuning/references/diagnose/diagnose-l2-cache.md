# l2cache

> l2cache（§4）：L2 命中率与读写明细的采集和判读；工具总览与五类对照表见 [msOpProf 采集指南](../profile/profile-msopprof.md)。

## 1. 特性介绍

L2 Cache 分析：采集 L2 命中率与读写明细，判断访存流量是否被 L2 有效缓存。命中率低意味着大量访问直达 HBM，是"减流量 / 改 Tiling / 换数据布局"类优化的直接依据。

## 2. 特性功能

- **输出文件**：`L2Cache.csv`，含 close/far 的 read/write hit、miss、victim 计数与 `read_hit_rate(%)`、`write_hit_rate(%)`（aic/aiv 分列）。
- **判读结论**：L2 命中率低列入 10 类瓶颈→优化映射。

## 3. 如何使用

- 命令行：
  - 本机 msOpProf 帮助中可用的 `L2Cache`/Memory 指标；以位置参数形式为例（输出目录需先 `chmod 755`）：

    ```bash
    mkdir -p msprof_repro/baseline && chmod 755 msprof_repro msprof_repro/baseline
    msprof op --output=./msprof_repro/baseline \
      --kernel-name=add_custom ./execute_add_op
    ```

  - msOpProf `MemoryDetail` 是 **L2 / 内存细节增强**，当前官方指南列为 A2/A3 能力；A5 使用 Default/Memory/L2 类输出，具体选项按本机帮助。
- 使用场景：HBM 带宽占用高但疑似流量可复用（L2 命中低）时；Tiling 设计阶段按目标 SoC 的实际 L2 容量评估工作集，禁止硬编码统一容量。

判读依据：

- CATLASS 诊断表："HBM 带宽高 + L2 命中低 → 换 workspace Kernel"；驻留 workspace 方案实测 L2 命中 **96–99%**（见 [optimize/optimize-catlass.md](../optimize/optimize-catlass.md) §3.1）；
- Ascend C 侧对比指标表含 L2 Cache 命中率。

## 4. 判读规则

1. 先判断数据是否存在跨 tile、跨阶段或跨 kernel 复用；纯流式读写的低命中率不自动构成问题。
2. 同时检查 GM 流量、active bandwidth、工作集大小和访问布局。只有命中率低且重复流量高，才优先考虑 L2 切分、全载或 workspace。
3. AIC 字段为 NA 时先核对 kernel 类型，不把 AIV 数据套用到 Cube 路径。
4. 前后比较必须使用相同工作集和采集口径；命中率改善但总时延不降时不保留修改。
