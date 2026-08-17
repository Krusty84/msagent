# CATLASS 整体性能基线【非 PR：README 官方数据】

## 基本信息
- 算子类别：matmul
- DSL/框架：catlass
- 类型：非PR（官方文档）
- 来源可信度：官方文档（README 官方数据）

## 来源链接
- PR/出处链接：<https://gitcode.com/cann/catlass/blob/master/README.md>（ 已验证可达，验证日期 2026-08）
- 优化代码查看：不适用——本案例为官方 README 公布的性能基线数据，非代码 PR，无 diff 可查。

## 问题与瓶颈
原文未附（README 为性能宣称与基线数据，未描述具体问题现象或 profiling 瓶颈）。

## 优化方法（理论手段）
原文未附（README 仅给出性能宣称，未展开优化手段）。

## 性能对比

"在定制 shape 下的性能能达到相应算子标杆性能的 0.98~1.2 倍"，附 Matmul（M=512/20480/65536 多组 N/K/TransA/TransB）与 GroupedMatmul（M 轴/K 轴切分）对 aclnn 的比值柱状图，环境 CANN8.2.RC1。

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 性能比值（对 aclnn 标杆） | 1.0（aclnn 基准） | 0.98~1.2 倍 | 定制 shape 下与标杆持平到小幅领先 |

## 适用范围与警示
- 该比值为"定制 shape 下"的官方宣称数据，环境为 CANN8.2.RC1；非定制 shape 或其他 CANN 版本下不代表同等水平。
