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
| `--kernel-name` 采集后结果为空但应用正常跑完 | kernel 名被 C++ mangled（匿名命名空间符号 `_ZN12_GLOBAL__N_1...`），过滤条件不匹配 | 先无过滤全量采集，从 `OpBasicInfo.csv` 抄真实 kernel 名；见 profile-msopprof.md §5.1 |
| 采集日志报 `libprofapi.so` / `CheckInputFileValid` / `child process exited 1` 但 CSV 正常产出 | 工具链噪声报错（CANN 9.1 实测） | 以产出检查为准，不凭 ERROR 行判失败 |
| fork 多进程程序只采到每进程第一个 kernel | msopprof 对 fork 子进程（SHMEM 多 rank 样例）采集不完整 | 标记 `partial`；改 SHMEMI_PROF 或完整 `msprof` |
| tiling/分核改动后运行 device fault（D2H 错误） | 新 tiling 与 kernel 的 buffer/深度假设冲突 | ① 按原口径回滚确认基线可复跑；② 加 `-g`（`--bisheng_flags=ccec_g` 或 `ascendc_compile_options`）重编 + msSanitizer 定位；③ 缩小改动粒度（如 baseK 128→192 而非 256）；④ 核对深度/容量联动字段（depthA1/B1、scaleFactor） |
| kernel 挂起（host 自旋等 device） | 死锁：同步协议改动后 flag 配对错误、或原子修改对只生效一半 | ① 强杀进程后实测 device 会自行恢复（可直接复跑基线确认）；② 对"不可拆分原子对"先做最小二分：只切模板/配置不改调度，验证单路径可用后再引入第二部分；③ 挂起即回滚，不在 hang 状态上调性能 |
| 精度 golden 生成链断裂（标杆 aclnn 拒绝该场景/脚本退化） | 校验不再可信：如官方输入含 NaN 位型导致"全 NaN 一致即 pass"的退化通过 | 停止性能调优；改用语义仿真 golden（CPU 参考）并保持工程原容差，补充无 NaN 辅助数据集；审查校验脚本本身（位级比对 vs 数值转换）后再恢复门禁 |
| 基线冷跑与稳态差异大（>20%） | 首次运行含编译/初始化/缓存预热，非 kernel 真实耗时 | 固定 warmup 剔除冷跑；稳态跑 ≥3 次取中位；run 间噪声 >5% 时，小于噪声幅度的收益不得归因 |
| 短 kernel（µs 级）event wall 无收益但 profiler 有收益 | launch 开销（µs 级）掩盖 kernel 收益，属计时口径问题 | 不要据 wall 判回滚或直接宣称收益：stream 内多 launch 单 event 对计时，或以 device duration 作辅助口径并在报告中注明 |