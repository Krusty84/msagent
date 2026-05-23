---
name: deterministic-calculation-analysis
description: 执行msProbe数据比对并分析比对结果，定位确定性计算问题首个输入一致输出不一致的API。
keywords: [确定性计算, msprobe, md5比对, CRC-32, 精度问题定位]
---

# 确定性计算比对分析

## 技能目标

1. **数据比对** — 调用 msProbe 工具比对两份 dump 数据，生成比对结果文件
2. **结果分析** — 分析比对结果，寻找首个输入一致输出不一致的计算 API
3. **排除误检** — 支持排除特定 API 后重新分析，直至定位根因

## 分析流程

### 1. 用户输入数据校验

用户提供 target_path（调试侧）和 golden_path（标杆侧）两份 dump 数据路径。

```shell
python3 "<skill_root>/scripts/md5_dump_files_checker.py" <target_path> <golden_path>
```

- 校验通过 → 输出 `level="L1"` 或 `level="mix"`，继续下一步
- 校验不通过 → 输出异常原因，终止流程

### 2. 依赖检查

```shell
pip3 show mindstudio-probe || pip3 install mindstudio-probe --pre
```

### 3. 数据比对与分析

根据第 1 步输出的 level 选择对应分支。两个分支都包含：调用 msProbe 比对 → 分析结果 → 支持 --exclude-api 排除后重分析。

#### 3.1 level="L1"

```shell
# 比对
msprobe compare -tp <target_path> -gp <golden_path> -o <output_path>

# 分析（支持传入目录或单个文件）
python3 "<skill_root>/scripts/find_first_diff_api_L1.py" <output_path>
```

分析特点：
- 无 Module 层级信息，仅有 API 级 md5 比对
- 找不到问题 API 时自动检测"状态跳变边界"，提示可能漏采的位置
- 支持 `--exclude-api "API名称"` 排除误检后重新分析

#### 3.2 level="mix"

```shell
# 比对
msprobe graph_visualize -tp <target_path> -gp <golden_path> -o <output_path>

# 分析
python3 "<skill_root>/scripts/find_first_diff_api_mix.py" <output_path>/compare_*.vis.db
```

分析特点：
- 包含 API 和 Module 两级分析，展示父子层级链路
- 当 API 级找不到结果、但 Module 级发现异常时，提示可能漏采的 API
- 支持 `--exclude-api "API名称"` 排除误检后重新分析

### 4. 输出

展示比对分析的结果并进行解读。

#### 4.1 找到问题 API

展示首个输入一致输出不一致的 API，包括其 Module 层级链路（仅 mix）和 md5 比对详情：

```
+----------------------+------------------------------------------+
|                     Rank 0                              |
+----------------------+------------------------------------------+
| 首个问题API           | NPU.npu_rms_norm.0.backward              |
+----------------------+------------------------------------------+
| API所在Module层级     | DefaultModel [Module]                    |
|                      |   → input_layernorm.RMSNorm [Module]     |
|                      |   → NPU.npu_rms_norm.0.backward [API]    |
+----------------------+------------------------------------------+
| API分析依据           | Input MD5 (全部一致)                     |
|                      | Output MD5 (不一致)                      |
|                      |   output.1: NPU=xxx vs Bench=yyy         |
+----------------------+------------------------------------------+
```

找到首个问题 API 即排查结束，后续只让用户选择是否排除该 API 重新分析。如果用户选择排除，则让用户输入要排除的 API 名称（支持前缀匹配，多个以空格分隔），内部重新执行分析流程。

#### 4.2 未找到问题 API（状态跳变）

```
+----------------------+------------------------------------------+
| 首个问题API           | 无                                       |
+----------------------+------------------------------------------+
| API分析依据           | 最后一个正常API: xxx                     |
|                      | 第一个输入不匹配API: yyy                 |
|                      | 两者之间的API可能被msprobe漏采           |
+----------------------+------------------------------------------+
```

**解读**: 未找到输入完全一致但输出不一致的 API，说明根因 API 可能被 msProbe 漏采。两个边界 API 之间的区域即为可疑范围，可调整 msProbe 采集配置后重新 dump 分析。

## 关键字段说明

### db（mix 级别）

| 字段 | 说明 |
|------|------|
| `node_name` | API/Module 名称 |
| `node_order` | 执行顺序，越小越先执行 |
| `node_type` | 0=Module, 1=API |
| `data_source` | NPU=调试侧, Bench=标杆侧 |
| `precision_index` | 0=pass, 1=error |
| `up_node / sub_nodes` | 父子层级关系 |
| `input_data / output_data` | 输入/输出数据，JSON 格式，包含 md5 值 |
| `is_distributed` | 是否为通信算子 |

### csv/xlsx（L1 级别）

| 字段 | 说明 |
|------|------|
| `NPU Name` | 调试侧 API 名称 |
| `Bench Name` | 标杆侧 API 名称 |
| `NPU MD5` | 调试侧 tensor CRC-32 值 |
| `BENCH MD5` | 标杆侧 tensor CRC-32 值 |
| `Result` | 比对结果（pass/fail） |
| `NPU Tensor Shape` | 调试侧张量形状 |
| `Bench Tensor Shape` | 标杆侧张量形状 |
