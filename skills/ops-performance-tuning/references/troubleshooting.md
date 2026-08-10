# 算子编译故障分流

> 编译或运行失败时，按此表逐项排查。无法匹配时，保存完整日志并标记 `BLOCKED`。

| 现象 | 判定 | 动作 |
|---|---|---|
| `msprof` 不可见 | 环境未加载或组件缺失 | 先加载 CANN 环境；仍缺失则给安装/修复提示 |
| `npu-smi` 不可见 | 驱动工具未安装、未挂载或 PATH 异常 | 上板模式 `BLOCKED`；不继续运行 |
| `GLIBCXX_3.4.32' not found` | miniconda3 的 libstdc++.so.6 版本过旧 | 替换为系统库：`ln -sf /usr/lib/x86_64-linux-gnu/libstdc++.so.6 <conda>/lib/libstdc++.so.6`；TBE 编译阶段 Python ctypes 依赖此项 |
| TBE 编译 OSError `libopmaster_ct.so` | 同 GLIBCXX 问题，Python ctypes 间接依赖不兼容 | 同上；不可用 `LD_PRELOAD`（影响 Python ctypes 加载） |
| `aic-ascend950-ops-info.ini` 不存在 (ffn系列) | ffn 是复合算子目录(ffn/ffn_worker_batching/ffn_worker_scheduler/swin_attention_ffn/swin_transformer_ln_qkv/swin_transformer_ln_qkv_quant)，全部 6 个子算子均缺 ascend950 ops info 配置 | `BLOCKED`：等待上游 ops-transformer 仓库补充 A5 支持。flash_attn/moe_gating_top_k/grouped_matmul 等同仓库其他算子确认 A5 可用 |
| `No module named 'packaging'` | venv 环境中 Python packaging 模块缺失，ops-transformer CMake generate 阶段依赖 | `python3.11 -m pip install packaging`（注意：必须用 venv 的 python 安装，不能用 conda pip 的 symlink） |
| `generate_compile_cmd_ascend950` Error | 同上，packaging 缺失导致编译命令生成失败 | 同上 |
| `No module named 'pip'` | venv 中 pip 缺失，ES wheel 打包失败 | `ln -sf $(which pip3) <venv>/bin/pip` |
| `No module named 'setuptools'` | venv 中 setuptools 缺失，ES wheel 打包失败 | `<venv>/bin/python3 -m pip install setuptools` |
| `No module named 'packaging'` | venv 中 packaging 缺失，ops-transformer CMake generate 失败 | `<venv>/bin/python3 -m pip install packaging` |
| `generate_es_math_whl` Error 1 | pip/setuptools 缺失导致 ES wheel 构建失败，仅 `--pkg` 模式触发 | 依次安装 pip, setuptools；或改用 `--opkernel` 跳过 ES 打包 |
| 单算子 `--pkg` 编译超 10 分钟 | 大算子(conv/reduce)拉入关联算子，内核变种 300+ | 改用 `--opkernel` 仅编译 .o；参见 `ops/ascendc/ops.md` §4.5.2 |
| `ASCEND_CANN_PACKAGE_PATH` 不存在 | asc-devkit 类独立工程缺少 CANN 包路径 | `export ASCEND_CANN_PACKAGE_PATH=$ASCEND_HOME_PATH`；或 cmake 时传 `-DASCEND_CANN_PACKAGE_PATH=$ASCEND_HOME_PATH` |
| 独立工程 CMake `FATAL_ERROR` | 未按工程要求设置环境变量 | 阅读工程 `cmake/config.cmake`，确认所需变量已设置 |
| 构建日志无 `-g` | 调试选项未进入 Device 编译 | 用 `--bisheng_flags=ccec_g`（统一算子仓）或 `ascendc_compile_options`（独立工程）注入 |
| 构建含 `-O0` | 调优配置无效 | 移除 `-O0`，保留原优化级别后重编译 |
| `torch_npu` 未安装 | triton-ascend 缺少 PyTorch NPU 后端 | `pip install torch-npu`；参见 triton-ascend 的 README 依赖说明 |
| 设备列表为空 | 无板卡、容器未透传或权限不足 | 保存命令输出并停止上板 |
| 应用直接运行失败 | 不是 profiler 问题 | 停止采集，保存应用错误 |
| profiler 成功但无原始文件 | 输出路径、权限、采集对象或工具参数错误 | 标记 `FAIL`，检查目录与 Kernel 匹配 |
| 仿真使用非 0 设备 | 仿真设备配置错误 | 修改应用参数或环境为设备 0 |