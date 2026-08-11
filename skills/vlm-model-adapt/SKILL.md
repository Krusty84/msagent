---
name: vlm-model-adapt
description: 为 msModelSlim 分析、实现、注册并执行适配器级验收的 Dense 图文理解模型（VLM）适配器。用于接入采用 safetensors 权重的图像加文本理解模型，处理 processor、视觉编码、视觉语言融合、语言模型逐层调度和模型注册；量化产物验证与敏感层/调优执行只做交接，不用于纯 LLM、多模态生成模型、MoE VLM、视频或音频模型。
---

# VLM Model Adapter

为目标视觉语言理解模型创建可被 msModelSlim 使用的模型适配器。

## 使用原则

- 将目标版本的 msModelSlim 源码作为接口与行为的最终依据。
- 将模型官方 processor 和 forward 实现作为多模态语义的最终依据。
- 将同版本接入文档作为流程依据，使用本 Skill 的 `assets/` 作为实现骨架；可以参考当前 msModelSlim 已支持的 Dense VLM Adapter 来核对公共接口、Runner、Loader 和注册写法，但不得直接复制，也不得用其替代目标模型源码推导 processor、forward 或融合语义。
- 不把模板中的模块路径、输入字段或融合逻辑直接套用到目标模型。
- 只适配 Dense、图像加文本、safetensors 权重的视觉语言理解模型。拒绝纯文本、视频和音频校准分支。
- 按“目标模型与公共接口源码 > 同版本接入文档 > 本 Skill 模板 > 用户模板”的优先级解决不一致；发现冲突时先明确报告，不得静默采用。

## 调用参数与自动发现

从上层 Agent 接收以下参数：

- `model_path`：必需，本地完整模型目录，与现有 `msmodelslim-model-adapt` 的权重目录参数含义一致。
- `model_type`：上层应传入准备注册或调用的 msModelSlim 模型名称；缺失时按模型目录与配置生成候选，仅在候选不唯一时向上层确认。
- `trust_remote_code`：可选，默认 `False`。
- `dataset`：可选；源码分析与初稿阶段不强制要求，到真实 `handle_dataset()` 或分段前向验收阶段缺失时再向上层索取图像加文本校准集。

不要要求上层提供可从运行环境确定的信息。先通过已安装模块、包元数据、配置文件和源码自动发现：

- msModelSlim 的安装位置、版本、接口、接入文档、注册配置和测试；若安装位置不是可修改的源码 checkout，再向上层索取目标源码目录。
- Transformers 的精确版本、实际导入路径以及 Config、Model、Processor、Tokenizer 的实现文件。
- 模型目录中的配置、chat template、`auto_map`、safetensors 权重索引、模块路径和权重前缀。
- CPU/NPU 环境，以及完整视觉与融合模块加一个语言层能否在 CPU 内存中常驻。

把“Adapter 为后续量化和敏感层分析提供了哪些已验证契约”作为实现后的交接信息，不在本 skill 内宣称量化产物或调优策略已经通过。自动发现失败或模型不满足当前 Dense 图文与统一加载边界时，报告已检查的证据和唯一必要的缺口。

## 工作流

### 1. 读取规范与当前接口

读取 msModelSlim 当前版本的多模态理解模型接入文档、基础模型接入文档、Pipeline 接口、VLM 基类、量化服务和注册配置。随后读取[模型实现定位](references/model-source-discovery.md)、[适配器结构与基类选择](references/adapter-structure.md)和[能力接口组合](references/capability-interfaces.md)。实现数据处理前读取[多模态数据处理](references/handle-dataset.md)，实现分层初始化前读取[模型初始化](references/init-model.md)，采用语言层按需加载时读取[动态 Decoder 与权重加载](references/dynamic-decoder-loading.md)，定义访问序列前读取[模型访问序列](references/model-visit.md)，拆解分段前向前读取[模型分段前向](references/model-forward.md)，实现缓存控制前读取[KV cache 控制](references/kv-cache.md)。开始拼装目标文件前读取[完整示例与装配顺序](references/complete-example-and-assembly.md)，执行本 skill 自身验收前读取[Adapter 自身验收](references/validation.md)。始终按当前源码复核文档、模板和参考 Adapter。

### 2. 分析目标模型

分别确定 Config、Model、Processor 和 Tokenizer 的真实实现来源：优先定位服务器实际安装的 Transformers 实现，不存在或不兼容时再按 `auto_map` 和模型目录查找自定义源码；两边都没有时，从网络搜索与服务器安装版本完全一致的 Transformers 官方源码，仍找不到时停止并报告。随后梳理 processor 输入、视觉模块、融合模块、语言 Decoder、位置编码、跨层状态、权重布局和保存要求。不要读取已支持模型的 Adapter 来补全目标模型行为。

### 3. 实现适配器

基于 `assets/qwen-style-adapter/` 的完整目录示例实现 Adapter、Loader 和注册。视觉语言理解模型默认继承 `VLMBaseModelAdapter`，并必须组合 `ModelInfoInterface` 与 `ModelSlimPipelineInterfaceV1`；不得按模型品牌选择基类。优先复用基类的配置、dtype 和输入搬运能力，复用 `generated_decoder_layer_visit_func()`、Runner 与 Loader。按[完整示例与装配顺序](references/complete-example-and-assembly.md)复制 Skill 内模板，再根据目标源码替换模型类、配置路径、模块路径、权重前缀和前向逻辑。实现 `ModelSlimPipelineInterfaceV1` 要求的 `handle_dataset()`、`init_model()`、`generate_model_visit()`、`generate_model_forward()` 和 `enable_kv_cache()` 五个抽象方法；同时实现 `ModelInfoInterface` 要求的 `get_model_pedigree()` 与 `get_model_type()`。不得拿模型信息方法替代 Pipeline 方法。

所有受支持模型大小统一采用同一加载流程：在 CPU 上临时把语言层数设为 `1`，通过目标模型官方 `from_pretrained()` 完整加载视觉与融合模块及首个语言层，在 `finally` 中恢复原始层数，再由 `generate_decoder_layer()` 按 safetensors 权重索引逐层加载其余语言层。把 `init_model()` 中从首次配置修改到常驻权重校验的全过程视为一个事务：修改前快照每个顶层与嵌套配置属性，任一步失败时按原值完整恢复；成功时只保留经目标运行逻辑证明需要的 cache、attention 等运行态设置。Runner 负责执行时的模块设备调度；不得因模型较小而改走完整语言模型常驻分支。若目标结构无法在该流程下保持等价，停止并报告当前 skill 不支持，不临时发明第二套加载路径。

检查目标模型的持久 buffer：动态权重加载必须覆盖 `state_dict()` 中的参数和持久 buffer。若某个 checkpoint-backed buffer 被 forward 使用，但当前 msModelSlim 导出链路不保存 buffer，在 Adapter 中将该对象精确转为 `requires_grad=False` 的 `nn.Parameter`；不得批量转换与保存无关的运行时 buffer。

### 4. 注册与配置

创建 `__init__.py`、`model_adapter.py` 和 `loader.py`，并在 `config/config.ini` 添加模型别名、Loader 入口及必要依赖约束。注册格式以当前 `PluginModelFactory`、`BaseModelAdapterLoader` 公共接口和 `assets/qwen-style-adapter/config.ini` 为准；修改后重新安装 msModelSlim 生成 entry point。同时检查官方 `lab_practice/`、自定义与插件实践仓的谱系命名，为 `get_model_pedigree()` 确定稳定的当前模型家族分组键。已有兼容实践时复用对应谱系；新模型家族暂无实践时，仍实现稳定的新谱系键并报告“目前不能自动读取最佳实践”；不阻塞显式 `config_path` 量化，也不得为复用无关配置而冒用其他模型的谱系键。

### 5. Adapter 自身验收

按[Adapter 自身验收](references/validation.md)依次完成静态契约检查和服务器真实图文前向对齐。本 skill 只有在两层均通过后才能报告“Adapter 自身验收通过”；缺少服务器环境、真实权重或图文样本时，报告“实现完成，真实前向待验”，不得等同于通过。

### 6. 交接后续验证与调优

输出后续任务所需的 `model_type`、`model_path`、`trust_remote_code`、Transformers 精确版本、Adapter/Loader 路径、图文数据集参数、Decoder 名称与层数，以及本 skill 的验收记录。全回退量化、产物权重一致性、保存加载及 W8A8 交给 `msmodelslim-adapter-verification`；敏感层分析和策略调优交给 `tune-practice-cfg`。除非上层另外调用对应 skill 并取得结果，不执行这些流程，也不报告其通过。

## 资源约定

- `assets/qwen-style-adapter/`：自包含的 Qwen 风格 Adapter 结构示例；`target_vlm/` 仅含三个运行时文件，根目录 `config.ini` 是注册片段。复制后必须按目标源码替换所有占位符。
- `references/model-source-discovery.md`：Transformers 优先、本地自定义实现兜底的逐组件源码定位规则。
- `references/adapter-structure.md`：通用目录、Loader、注册和基类选择规则。
- `references/capability-interfaces.md`：Adapter 类定义、必需接口与按量化能力选择的可选接口。
- `references/handle-dataset.md`：`handle_dataset()` 的通用工作流、边界、实现风格选择与 Qwen 模板使用要求。
- `references/init-model.md`：`init_model()` 的分层加载流程、配置恢复、权重加载与模型相关边界。
- `references/dynamic-decoder-loading.md`：动态语言层、safetensors 权重索引和模型相关替换点。
- `references/model-visit.md`：`generate_model_visit()` 的拓扑顺序、动态语言层依赖和两段式 Qwen 模板。
- `references/model-forward.md`：`generate_model_forward()` 的源码检索、调用链还原、融合与逐层前向规则。
- `references/kv-cache.md`：`enable_kv_cache()` 的真实配置路径识别、Qwen 写法和验证要求。
- `references/complete-example-and-assembly.md`：Skill 内模板的装配顺序、模型相关替换项与成品清零门禁。
- `references/validation.md`：本 skill 负责的静态契约和真实分段前向最低验收标准。
- `references/`：继续补充实现规范、模型分析清单和验证标准。

## 输出

交付 Adapter、loader、注册修改、配置、Adapter 自身验收结果和后续验证交接信息。分别报告“Adapter 自身验收”“量化产物验证”“敏感层/调优”的状态；后两项没有外部 skill 结果时统一标记为“未执行”，不得推断为通过。
