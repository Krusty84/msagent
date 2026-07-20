# 环境准备

**Load when:** 进入量化配置调优前，需确认硬件、运行环境就绪。

## msmodelslim 安装（强制）

量化调优通过 **当前 shell 环境** 调用 `msmodelslim` CLI（`analyze` / `quant` 等），编排层脚本通过 Python 直接 `import msmodelslim`。

1. 确认已安装 Ascend 工具链与 CANN（NPU 场景）。
2. 安装 msmodelslim（版本以项目要求为准）：

```bash
pip install msmodelslim
```

3. 验证：

```bash
python -c "import msmodelslim; print('ok')"
```

4. 若模型适配阶段执行过 `bash install.sh`，脚本会在**同一 Python 环境**中读取新 adapter，**无需**重启 MCP 服务；继续调优前确认 `import` 无报错即可。

## Ascend 运行环境变量

原先写在 `config.mcp.json` modelslim 条目中的环境变量，需在**执行量化/评测脚本的 shell** 中配置（或写入用户 `~/.bashrc` / 启动脚本）。典型项包括：

- `ASCEND_HOME_PATH` / `ASCEND_TOOLKIT_HOME` / `ASCEND_AICPU_PATH` / `ASCEND_OPP_PATH`
- `ATB_HOME_PATH` 及 ATB 相关 tuning 变量
- `LD_LIBRARY_PATH`（含 Ascend driver、ATB、opp 等路径）
- `PYTHONPATH`（含 ascend-toolkit `python/site-packages` 与 tbe 路径）

以本机 CANN 安装路径为准；不同机器路径可能不同，**禁止**假设固定目录存在。

## NPU 资源检查与物理绑定（强制）

环境检查时必须先运行 `npu-smi info` 获取全部卡信息，确认**总卡数**；
不可假设机器一定有 8 张卡，不同机器的卡数不同。

### 情况一：用户未指定 NPU 卡号

1. 逐卡检查 HBM 占用和进程列表，判断哪些卡空闲。
   - HBM 有少量占用且存在用户进程时，表示该卡被占用；
   - 仅有少量 HBM 占用、但无用户进程时，通常为驱动占用，可视为空闲。
2. 根据本次量化与测评所需卡数，给出候选空闲卡列表及用途建议。
3. 必须将候选卡号回显给用户确认；在获得用户确认或用户明确指定卡号前，不得启动敏感层分析、量化或测评。
4. 用户确认后，将确认的卡号记录为 `selected_npu_ids`。

### 情况二：用户已指定 NPU 卡号

1. 用户指定的卡号列表是本任务唯一允许使用的物理 NPU 白名单。
2. 仍需运行 `npu-smi info` 确认机器总卡数，并检查用户指定卡的 HBM 占用与进程情况。
3. 若指定卡全部可用，将该列表直接记录为 `selected_npu_ids`。
4. 若任一指定卡不可用，必须回显具体卡号、HBM 占用和相关进程，并等待用户决定。
5. 不得因为其他卡更空闲而自动替换、扩展或改用白名单外的 NPU。

### 回显与执行要求

回显中必须体现：

- 机器总卡数；
- 每张卡的 HBM 占用量和进程情况；
- 用户是否已指定卡号；
- 最终选用的物理卡号列表及用途（量化 / 评测）。

`selected_npu_ids` 一经确定，敏感层分析、量化和测评均只能使用该物理卡列表。

所有相关进程必须设置：

```bash
ASCEND_RT_VISIBLE_DEVICES=<selected_npu_ids>
```

## 命令调用方式

- **敏感层分析 / 量化**：通过 `execute` 运行 `msmodelslim analyze`、`msmodelslim quant`（参数见各 Skill 文档）。
- **编排层 / 校验 / 评测**：通过 `execute` 运行 skill 目录下脚本，例如：

```bash
python skills/quantization-accuracy-tuning-orchestrator/scripts/history_clear.py --save-path /path/to/workdir
```

编排脚本 **stdout** 输出单行 JSON（`{ok: ...}`）；CLI 以 **exit code** 判定成败。

## 环境就绪确认

向用户回显：msmodelslim 可 import、Ascend 环境变量已配置、NPU 卡号已确认。获得用户认可后再进入模型准备阶段。
