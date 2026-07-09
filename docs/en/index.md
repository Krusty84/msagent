# MindStudio-Agent Documentation

MindStudio-Agent is an Agent workspace for Ascend NPU debugging and optimization. This English section provides a compact entry point for installation, configuration, core Agent usage, and Skill development.

```{toctree}
:maxdepth: 2
:caption: Getting Started

getting_started/quick_start
```

```{toctree}
:maxdepth: 2
:caption: User Guide

user_guide/faq
```

```{toctree}
:maxdepth: 2
:caption: Developer Guide

developer_guide/interface-reference
developer_guide/skill-development
```

```{toctree}
:maxdepth: 2
:caption: Agent Guide

agent_guide/Profiler
agent_guide/Accuracy
agent_guide/Quantizer
agent_guide/Modeling
agent_guide/Minos
agent_guide/Operator
```

## Built-in Agents

| Agent | Focus | Description |
|---|---|---|
| [Profiler](agent_guide/Profiler.md) | Performance tuning | Analyzes Ascend profiling data, bottlenecks, slow ranks, MFU, communication, operators, and host scheduling issues. |
| [Accuracy](agent_guide/Accuracy.md) | Accuracy debugging | Analyzes msProbe dump data, RL train/inference consistency, NaN / overflow, and deterministic calculation issues. |
| [Quantizer](agent_guide/Quantizer.md) | Model quantization | Orchestrates msModelSlim quantization, model adaptation, configuration generation, quantization execution, and evaluation. |
| [Modeling](agent_guide/Modeling.md) | Simulation modeling | Supports LLM/VLM simulation modeling, throughput planning, device profiling, and serving parameter recommendations. |
| [Minos](agent_guide/Minos.md) | Documentation UX and review | Reviews README, Quick Start, onboarding flow, documentation usability, and GitCode PR quality. |
| [Operator](agent_guide/Operator.md) | Operator tuning | Supports Ascend operator performance analysis and end-to-end operator optimization. |

For the full Chinese documentation tree, see the [main documentation index](../index.md).
