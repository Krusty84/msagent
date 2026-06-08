# Modeling 仿真建模

`Modeling` 是面向 `msmodeling` 场景的领域 Agent，负责承接 TensorCast / ServingCast 相关的性能建模、单点仿真、吞吐规划、设备画像和模型接入准备类问题。

> 当前版本已经接入首批 `msmodeling` 专项 skills，可在统一入口下承接参数补齐、命令规划、设备建模引导与执行前确认；涉及真实性能结果时，仍应以实际运行输出为准。

## Agent 定位

- 面向 `msmodeling` 仓库及其相关任务
- 覆盖 TensorCast 性能仿真与 ServingCast 部署规划两大方向
- 聚焦“理解需求 → 梳理输入 → 形成候选命令 / 方案 → 给出验证路径”
- 对已接入的专项链路优先走 skill，对未接入链路继续提供结构化人工引导

## 已接入能力

当前已接入以下 `msmodeling` 专项能力：

- `msmodeling-env-installer`：用于安装和验证 `msmodeling` 开发环境，覆盖 `uv` 创建 `myenv`、安装 `requirements.txt`、配置当前会话 `PYTHONPATH` / `HF_ENDPOINT` 与依赖一致性检查
- `msmodeling-text-generate-executor`：用于单点仿真参数补齐、候选命令生成、执行前确认与结果总结
- `msmodeling-throughput-optimizer-executor`：用于吞吐规划、硬件对比、聚合/分离/P:D 配比搜索的参数补齐与结果总结
- `msmodeling-device-config`：用于把自然语言硬件规格转换为 TensorCast `DeviceProfile` 建模输入，并显式标注待校准项

## 核心能力

- 解释 TensorCast / ServingCast 的能力边界与典型使用方式
- 梳理 `text_generate`、`throughput_optimizer` 等场景的关键输入参数
- 帮助用户区分单点验证、部署规划、设备建模、新模型接入等不同任务类型
- 根据本地仓库文档与实现，给出候选命令、输入清单、检查项和验证路径
- 在没有真实运行结果时，明确区分“仿真/规划建议”和“实测结论”

## 适用场景

- 想按 README 初始化 `msmodeling` 开发环境、创建 `myenv` 或安装 `requirements.txt`
- 想配置当前会话的 `PYTHONPATH`、`HF_ENDPOINT`，或检查 Python 依赖一致性
- 想知道某个 `msmodeling` 需求该走 TensorCast 还是 ServingCast
- 想梳理 `python -m cli.inference.text_generate` 运行前需要准备哪些参数
- 想评估 `python -m cli.inference.throughput_optimizer` 的规划思路或输入项
- 想为新硬件准备 device profile
- 想从 raw profiling / doctor report 出发规划模型接入工作
- 想理解 `op_mapping.yaml`、microbench replay 等建模辅助链路

## 推荐使用方式

- 尽量直接提供 `msmodeling` 仓库路径、模型名称、目标设备、设备数和任务目标
- 如果你要初始化环境，请说明是否要使用默认 `myenv`、是否允许安装 `uv` / `requirements.txt`、是否需要设置 `PYTHONPATH` 或 `HF_ENDPOINT`
- 如果你要的是单点仿真，请说明：模型、device profile、设备数、输入/输出长度、Prefill/Decode 模式
- 如果你要的是吞吐规划，请说明：模型、硬件、设备数、输入输出长度、SLO、部署模式（聚合 / 分离 / P:D 比例）
- 如果你要做设备建模或模型接入，请尽量提供已有资料来源、raw profiling、目标提交位置和约束条件

## 当前边界说明

- 对已接入的 4 个专项 skill，Agent 可以直接承接：
  - 环境依赖安装与检查
  - 参数渐进式补齐
  - 候选命令生成
  - 执行前确认
  - 执行后结果总结
- 环境安装类任务涉及网络安装、删除或覆盖已有环境、fallback 安装到已有 Python 环境时，必须先说明影响并取得确认
- 对尚未接入 skill 的链路，Agent 仍以人工引导为主，而不是伪造自动化流程
- 若需要真实性能结果，仍应以实际运行和验证输出为准

## 后续规划

后续仍建议逐步补齐以下 `msmodeling` 专项能力：

- 新模型接入：`model-adaptation`
- 算子映射 / replay：`op-mapping`、`microbench`

## 典型使用场景

| 场景 | 示例提示词 |
|---|---|
| 环境初始化 | `请帮我按 msmodeling README 初始化环境，创建 myenv 并安装 requirements.txt。` |
| 单点仿真参数梳理 | `请帮我梳理 Qwen3-32B 在 A3 上跑 text_generate 需要补齐哪些参数。` |
| 吞吐规划咨询 | `我想比较同一个模型在两种硬件上的部署吞吐规划，应该怎么准备 throughput_optimizer 输入？` |
| 设备建模入口 | `我要给一块新硬件做 TensorCast device profile，先帮我确认需要哪些规格。` |
| 模型接入准备 | `我已经有 raw profiling，想继续做 model adapter doctor 相关流程，先帮我拆解步骤。` |
