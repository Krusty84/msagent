# PR #665 "[Perf] Improve SFA Performance and Add Optimization Guide"【PR】

## 基本信息
- 算子类别：attention
- DSL/框架：tilelang
- 类型：PR
- 来源可信度：一手 PR 原文

## 来源链接
- PR/出处链接：<https://github.com/tile-ai/tilelang-ascend/pull/665>（ 已验证可达，验证日期 2026-08；2026-03-25 合入）
- 优化代码查看：<https://github.com/tile-ai/tilelang-ascend/pull/665/files>

## 问题与瓶颈
原文未附 profiling 瓶颈定位数据。

## 优化方法（理论手段）
1. 新增 SFA（Sparse Flash Attention）精度验证脚本、多版本 SFA 实现与性能优化指南。
2. kernel 级改写，提交含 "using axpy"、"remove score_scale" 等——用 axpy 融合乘加、移除冗余 score_scale 计算以减少指令。

## 性能对比
PR 正文未附量化数字。

## 适用范围与警示
- 适用 shape/dtype/芯片：原文未附。
