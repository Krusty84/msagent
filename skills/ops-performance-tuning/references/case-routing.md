# 算子类型全流程知识库梗概：编译、msOpProf 采集与调优

> 本文是跨 CANN 版本、A2/A3/A5 与多 DSL 的快速路由入口。先识别算子类型，再按统一口径完成“版本对齐 → 编译 → 精度基线 → msOpProf 采集 → Bound 判定 → 同型案例调优 → 前后对比”。详细内容按需下钻到同目录的 `compile-*`、`profile-*`、`diagnose-*`、`optimize-*` 与 `cases/`。

## 导航

- [环境指纹与芯片映射](#1-先固定环境指纹禁止跨环境比较)
- [分 DSL 编译路径](#2-编译路径速查)
- [msOpProf 跨版本采集](#3-msopprof-跨版本采集先探测再选命令)
- [按算子类型路由调优](#4-按算子类型路由调优)
- [标准调优闭环](#5-标准调优闭环)
- [案例证据等级](#6-案例入库与证据等级)
- [官方来源](#7-官方来源)

## 1. 先固定环境指纹，禁止跨环境比较

每次基线和优化轮都记录以下信息；任一项变化都应重建基线：

```bash
source /usr/local/Ascend/cann/set_env.sh
npu-smi info
which bisheng
which msprof
msprof --version 2>/dev/null || true
cat "$ASCEND_HOME_PATH/version.info" 2>/dev/null || true
python3 - <<'PY'
try:
    import acl
    print("runtime_soc:", acl.get_soc_name())
except Exception as exc:
    print("runtime_soc: unavailable:", exc)
PY
```

还必须记录：算子仓 URL、branch/tag/commit、CANN/driver/firmware 版本、设备 ID、逻辑 shape、物理 padding 后 shape、dtype、format、TilingKey、blockDim、编译优化级别、频率、warmup/launch 次数。

### 1.1 CANN 源码分支与芯片映射

- 官方算子仓源码必须优先选择与本机 CANN 发布版本匹配的 tag/branch；`master` 只用于明确的前沿验证。Host Tiling、Kernel API、编译器和 OPP 包不一致时，性能数据不可作为同口径结论。
- 先用 `npu-smi info`/运行时 SoC 名称识别设备，再选择编译参数，禁止只根据机器昵称猜架构。

| 代际 | 常见设备 | 官方算子仓 `build.sh --soc` | 独立工程常见 `NPU_ARCH` | 关键差异 |
|---|---|---|---|---|
| A2 | Ascend 910B、Atlas A2 | `ascend910b` | `dav-2201` | MemBase/高层 API 为主；不要下沉 A5 VF/RegBase 专属路径 |
| A3 | Atlas A3 / 910_93 系列 | `ascend910_93` | 以本机工具链和样例声明为准 | 不硬编码 `dav-*`；从目标仓 CMake 与本机 `--help` 获取 |
| A5 | Ascend 950/950PR | `ascend950` | `dav-3510` | 可重点评估 RegBase/VF、MicroAPI、PipeTimeline/PcSampling |

## 2. 编译路径速查

### 2.1 CANN 官方算子仓：ops-nn / ops-math / ops-cv / ops-transformer

```bash
source /usr/local/Ascend/cann/set_env.sh
cd <repo_root>

# 完整自定义算子包：用于 ACLNN/框架调用闭环
bash build.sh --pkg --soc=<ascend910b|ascend910_93|ascend950> \
  --ops=<op_name> --vendor_name=custom -j16

# 仅编译内核：大算子或快速迭代优先
bash build.sh --opkernel --soc=<soc> --ops=<op_name> --build-type=Release

# 需要源码映射时保持 Release，只增加调试信息
bash build.sh --opkernel --soc=<soc> --ops=<op_name> \
  --build-type=Release --bisheng_flags=ccec_g
```

- `--pkg` 产物安装后用 ACLNN demo 运行；`--opkernel` 产物可在本机 `msprof op --help` 支持时通过 `--config=<kernel.o>` 直接采集。
- profiling 编译禁止为了源码映射切到 `-O0`；优化级别变化会使前后数据不可比。
- Conv/Reduce 等会拉入大量关联内核时，先用 `--opkernel` 缩短迭代，再用 `--pkg` 做最终接口回归。
- 完整参数以目标仓 `bash build.sh --help` 和对应版本 `docs/zh/install/build.md` 为准。

### 2.2 其他 DSL

| DSL | 最短编译/运行入口 | 基线与精度入口 | 详细资料 |
|---|---|---|---|
| Ascend C 独立工程 | `cmake -S . -B build -DNPU_ARCH=<dav-*> && cmake --build build --parallel` | ACLNN demo / golden 对比 | [ops/ascendc/ops.md](compile/compile-ascendc.md) |
| CATLASS | 按仓库 CMake preset/示例 target 构建 | 示例输出 `Compare success.`，shape 覆盖小/中/大 | [ops/catlass/ops.md](compile/compile-catlass.md) |
| Triton-Ascend | 安装与本机 CANN 匹配的 wheel 或源码后运行 Python 用例 | `torch.allclose` + `triton.testing` | [ops/triton/ops.md](compile/compile-triton.md) |
| TileLang-Ascend | 按 ascendc_pto 或 npuir 路线安装并运行示例 | `Kernel Output Match!` | [ops/tilelang/ops.md](compile/compile-tilelang.md) |
| PyPTO | 按工程 build/run 脚本编译图 | `torch.allclose`，同时保存图/泳道配置 | [ops/pypto/ops.md](compile/compile-pypto.md) |
| SHMEM / MC² | Host+Kernel+多 rank 启动程序一体构建 | HCCL baseline → SHMEM/MC² → 离线对比 | [ops/shmem/ops.md](compile/compile-shmem.md) |

## 3. msOpProf 跨版本采集：先探测，再选命令

`msprof op` 与独立 `msopprof` 的封装、参数名和可用指标随 CANN/msOpProf 版本变化。不要把一台 A5 beta 环境的参数直接复制到 A2/A3。

```bash
if command -v msprof >/dev/null 2>&1 && msprof op --help >/tmp/msopprof_help.txt 2>&1; then
  echo "use: msprof op"
elif command -v msopprof >/dev/null 2>&1; then
  msopprof --help >/tmp/msopprof_help.txt 2>&1
  echo "use: msopprof"
else
  echo "ERROR: msOpProf is unavailable" >&2
  exit 1
fi

# 所有可选参数都以这份本机帮助为门禁
rg -n "application|kernel-name|launch-count|warm|aic-metrics|aicore_metrics|config|simulator" \
  /tmp/msopprof_help.txt
```

### 3.1 命令模板

官方当前用法以“参数后直接跟应用”为主：

```bash
# Ascend C / ACLNN 可执行文件
msprof op --output=./prof/baseline \
  --kernel-name=<kernel_name> --launch-count=20 \
  ./execute_<op> <args>

# Triton / TileLang Python 用例
msprof op --output=./prof/baseline \
  --kernel-name=<kernel_name> --launch-count=20 \
  python3 test_<op>.py

# CATLASS 示例
msprof op --output=./prof/baseline \
  ./00_basic_matmul 256 512 1024 0

# 独立 msopprof 包装（旧版/独立安装常见）
msopprof --output=./prof/baseline ./execute_<op> <args>
```

若本机 `--help` 明确支持 `--application`，也可使用该版本的应用字符串形式：

```bash
msprof op --output=./prof/baseline \
  --application="DEVICE_ID=1 ./execute_<op> <args>"
```

规则：

1. `--application`、位置参数、`--aic-metrics`/`--aicore_metrics` 只使用本机帮助存在的形式。
2. `--warm-up` 不是所有版本都提供。若帮助中没有该选项，在测试程序内部先执行固定 warmup，再启动正式 launch；前后必须相同。
3. 不指定 `--kernel-name` 时，部分版本只采集调度到的第一个算子；多 kernel/融合程序必须明确过滤并核对 `OpBasicInfo.csv`。
4. `--config=<kernel.o>`、`simulator`、MC² 采集均属于能力门控项；本机帮助不支持就回退到 ACLNN 应用采集或完整 `msprof`。
5. 输出目录需可写/可遍历；短 kernel 同时保存 host event 计时，绝对性能以 event 为主，msOpProf 用于结构与趋势诊断。

### 3.2 指标能力矩阵

以下是当前官方 msOpProf 用户指南给出的芯片支持边界；最终仍以安装版本 `--help` 为准。

| 指标/能力 | A2 | A3 | A5 | 用途 |
|---|---:|---:|---:|---|
| KernelScale | ✓ | ✓ | ✓ | 核间任务规模、尾核长尾 |
| Occupancy | ✓ | ✓ | ✓ | 核间负载与占用 |
| Source | ✓ | ✓ | ✓ | 热点源码/指令定位 |
| MemoryDetail | ✓ | ✓ | — | A2/A3 的细粒度内存分析 |
| TimelineDetail | ✓ | ✓ | — | A2/A3 的细粒度时间线；二级指针、Triton、MC² 有限制 |
| PipeTimeline | — | — | ✓ | A5 Pipe 重叠与气泡 |
| PcSampling | — | — | ✓ | A5 指令热点、VF/SIMT/Scalar 诊断 |
| Roofline | 依版本 | 依版本 | 依版本 | 通常与 Default 组合，先查帮助 |

常见输出优先级：

- `OpBasicInfo.csv`：Task Duration、Block Dim、频率等基线字段。
- `KernelScale`/Occupancy：长尾 block、工作量分布与核数是否合理。
- `PipeUtilization`/Timeline：MTE2、Vector、Cube、MTE3 的重叠和气泡。
- Memory/L2：GM/L2/UB 搬运、Cache 命中与带宽。
- Source/PcSampling：把热点映射回地址计算、标量控制、Vector/Cube 指令。

## 4. 按算子类型路由调优

### 4.1 Vector / Elementwise / Quantize

**常见算子**：Add/Mul/Logit/Activation/Cast/Quantize/Dequantize。

- 编译：官方 ops-nn/ops-math 单算子优先 `--opkernel`；A5 RegBase/VF 路径必须以 `ascend950`/`dav-3510` 编译，A2/A3 保留兼容分支。
- 采集：Task Duration → KernelScale/Occupancy → PipeUtilization → Source/PcSampling；重点看尾核、Scalar 占比、UB 往返、Vector 指令链与同步。
- 调优顺序：按元素均衡切核 → 连续 elementwise 合并 → 标量参数用 `Muls`/常量折叠 → 减少 `Compare+Select` → 循环不变量外提 → 删除经依赖证明冗余的 barrier → A5 评估 RegTensor/MicroAPI。
- 直接案例：[Logit A5 多核+MicroAPI](cases/ascendc/pr_ascendc_logit_a5_microapi.md)、[Quantize A2 倒数乘与同步精简](cases/ascendc/pr_ascendc_quantize_a2_micro_opt.md)、[Add 核间负载判读](cases/ascendc/pr_ascendc_msopprof_add_block_imbalance.md)、[cann-samples 性能故事](cases/ascendc/pr_ascendc_cann_samples_perf_story.md)。

### 4.2 Reduction / Norm / Softmax

**常见算子**：ReduceSum/Max、LayerNorm/RMSNorm、Softmax、统计量归约。

- 编译：Reduce 大算子依赖多时先 `--opkernel`；为 Source 分析只加 `ccec_g`，保持 Release。
- 采集：KernelScale/Occupancy 检查长尾；PipeTimeline 看 MTE 与 Vector；Source/PcSampling 看地址生成、归约树与同步；Memory/UB 看反复读写。
- 调优顺序：分段/树形归约 → 合理多核切分与尾块特化 → 全载小轴 → 行/列布局与 vector 对齐 → 中间值留 UB/寄存器 → 合并归约后的 elementwise → A5 评估 areg/VF。
- 直接案例：[LayerNormV3 areg 地址优化](cases/ascendc/pr_ascendc_layernorm_areg.md)、[Triton 原子归约 SIMD 案例及 revert 警示](cases/triton/pr_triton_atomic_simd_24x.md)。

### 4.3 MatMul / Cube / Grouped MatMul

**常见算子**：MatMul/BatchMatMul/GroupedMatmul、量化 GEMM、Cube 主导融合。

- 编译：ops-nn/CATLASS 按目标 SoC 构建；shape 多时先固定一个代表 shape，再做完整 shape 集回归。
- 采集：Task Duration → Arithmetic/Cube 利用率 → MTE2/L1/L0A/L0B/L0C → L2 hit → PipeTimeline；记录实际 M/N/K、trans、dtype、tile 与 swizzle。
- 调优顺序：先容量模型与 Tiling → M/N/K tile → A/B 一侧全载 → L1/L0 double buffer → swizzle/核间负载 → Fixpipe/双目的端 → L2 友好布局 → Vector-Cube-Vector 联合流水。
- 直接案例：[MatMul 小输入全载](cases/ascendc/pr_ascendc_matmul_full_load_wiki.md)、[MatMul VCV 流水](cases/ascendc/pr_ascendc_matmul_vcv_wiki.md)、[CATLASS swizzle](cases/catlass/pr_catlass_swizzle_balance.md)、[msTuner 3.8x](cases/catlass/pr_catlass_mstuner_3p8x.md)、[Grouped MatMul 默认 Tiling](cases/catlass/pr_catlass_grouped_matmul_tiling.md)。

### 4.4 Attention / Transformer 融合算子

**常见算子**：FA/SFA/GQA/MQA、Decode Attention、Chunk Gated Delta Rule、QKV/Norm/MatMul 融合。

- 编译：优先 ops-transformer 对应 CANN branch；同时固定 layout、head 数、seq length、mask、kv cache 与量化配置。
- 采集：分解前处理 Vector、核心 MatMul/Cube、Softmax/归约、后处理与多 kernel 间隙；再看单 kernel PipeTimeline。
- 调优顺序：减少中间 GM 落盘与 kernel launch → 融合 Vector 前后处理 → KV/小矩阵复用 → persistent 调度（decode）→ tile 间流水 → A5 VF 化局部热点。融合后必须防止 Cube tile 挤占 Vector UB。
- 直接案例：[CGDR stage1 求逆 VF 化](cases/ascendc/pr_ascendc_cgdr_vf_inverse.md)、[TileLang persistent MQA decode](cases/tilelang/pr_tilelang_persistent_mqa_decode.md)、[FA/SFA 案例](cases/tilelang/pr_tilelang_fa_pr698.md)、[PyPTO GQA 合轴合图](cases/pypto/pr_pypto_gqa_decode_vector_merge.md)。

### 4.5 Conv / CV / 大工作集算子

**常见算子**：Conv2D/Conv3D、反向输入/反向权重、Pool、图像变换。

- 编译：关联内核多时用 `--opkernel` 快速迭代，最终再 `--pkg` 做 ACLNN 和全变种回归。
- 采集：Cube 利用率、L1/L0 搬运、MTE2/MTE3、L2 hit、KernelScale；对 ND/HW/Channel tail 单独建 case。
- 调优顺序：输入/权重/输出 Tiling → L1/L0 复用 → im2col/格式转换与 Cube 重叠 → 固定暂存 TBuf 化 → double buffer → L2 工作集切分 → 尾块对齐与特化。
- 直接案例：[Conv3DDX L1 TQue→TBuf](cases/ascendc/pr_ascendc_conv3ddx_tque_tbuf.md)、[超 L2 工作集 Tiling](cases/ascendc/pr_ascendc_tiling_l2_official.md)。

### 4.6 Communication / MC² / SHMEM

**常见算子**：AllGather+MatMul、ReduceScatter、AllToAllV、通信计算融合。

- 编译：保留 Host 启动、rank 配置、通信初始化和 Kernel 编译完整链路。需要 msOpProf 通算流水时，按官方方式增加 `-DASCENDC_TIME_STAMP_ON` 与 `-g`，但以本机版本指南为准。
- 采集：先做 HCCL baseline，再单独跑 SHMEM/MC²；同时记录 e2e、kernel、algBw、busBw、每 rank 最大/均值与通信计算重叠。
- 工具：新版 msOpProf 已有 MC² 多设备/通算流水能力时可用 `msprof op`；旧版、fork/多进程采集不稳定或帮助未声明支持时，回退完整 `msprof`。禁止继续使用“MC² 永远不能用 msprof op”这类跨版本绝对规则。
- 调优顺序：先固定通信量与 PE/rank → 排查串行链路/热点 PE → 通信分片 → 通信与 Cube/Vector 交错 → 合并 barrier/flag → 尾块对齐 → 多 rank 长尾。
- 直接案例：[AllGatherMM Scalar+尾块对齐](cases/ascendc/pr_ascendc_allgathermm_scalar_align.md)、[SHMEM AllToAllV full-mesh](cases/shmem/pr_shmem_alltoallv_mr152.md)、[ReduceScatter 串行链路消除](cases/shmem/pr_shmem_reduce_scatter_serial.md)、[MC² 融合背景案例](cases/ascendc/pr_ascendc_mc2_fusion.md)。

### 4.7 DSL / 编译器级优化

**常见问题**：Triton SIMT/SIMD lowering、TileLang 调度、constexpr 重编译、autotune、PyPTO 图与泳道。

- 采集：先确认最终 kernel 名和实际 launch 数，再用 `--kernel-name` 过滤；把 compile time 与 kernel time 分开，禁止把 JIT 首轮计入稳态 kernel 基线。
- 调优顺序：稳定 IR/shape specialization → 减少重复编译 → 合并图/launch → 调整 tile/num_warps 等调度参数 → 检查生成代码的搬运、向量化和同步。
- 直接案例：[Triton constexpr 重编译](cases/triton/pr_triton_constexpr_recompile.md)、[TileLang 编译器性能 PR](cases/tilelang/pr_tilelang_compiler_prs.md)、[PyPTO 独立 MatMul TileShape](cases/pypto/pr_pypto_per_matmul_tile_shapes.md)。

## 5. 标准调优闭环

1. **精度门禁**：基线实现和标杆在完整 shape/dtype/format 集通过；保存 tolerance 与 mismatch。
2. **建基线**：应用内固定 warmup，host event 重复计时；另做一次 msOpProf 采集。短 kernel 不把 profiler 插桩后的绝对时延当最终性能。
3. **判 Bound**：按 Compute/Cube、Vector、Memory/GM、L2/片上容量、Scalar/控制、Communication 六类形成证据表。
4. **找同型案例**：按本文件 §4 进入相同算子类型，再按 SoC/DSL/瓶颈过滤；PR 机制可迁移，性能数字不可跨环境迁移。
5. **单变量修改**：一次只实施一个可归因改动；重新编译、精度全量回归、同口径计时与关键指标采集。
6. **Keep/Revert**：全量 case 无不可接受回退且目标 case 稳定提升才保留；否则回滚该变量。
7. **输出前后对比**：至少给出 case/shape/dtype、baseline us、optimized us、Δ%、加速比、精度、结果 JSON、profiling 目录与环境指纹。

推荐结果表：

| Case | Shape | DType | Baseline event(us) | Optimized event(us) | Δ% | Speedup | Precision | msOpProf 结论 |
|---|---|---|---:|---:|---:|---:|---|---|
| `<id>` | `<shape>` | `<dtype>` | `<b>` | `<o>` | `(o-b)/b*100` | `b/o` | PASS/FAIL | Bound 与关键指标变化 |

## 6. 案例入库与证据等级

| 等级 | 来源 | 可写入内容 | 禁止事项 |
|---|---|---|---|
| A | 官方已合并 PR/MR，含测试表 | 机制、适用平台、原文数字、精度结果 | 把某个 shape 的收益泛化到全场景 |
| B | 官方 PR/MR，无数字 | 代码机制、文件路径、平台分支 | 推算或补写加速比 |
| C | 官方 Wiki/指南/sample | 推荐流程、可运行案例、原文数字 | 把教程数字当目标设备峰值 |
| D | 社区/二手材料 | 仅作线索与背景，明确警示 | 作为唯一验收证据 |

每个案例至少包含：算子类别、DSL、来源类型、直接链接、验证日期、瓶颈、优化机制、原文性能数字、适用 SoC/CANN 与风险。PR 未公开数字时统一写“原文未附量化数字”，不得用提交标题推导收益。

## 7. 官方来源

- 官方算子仓：[ops-nn](https://gitcode.com/cann/ops-nn)、[ops-transformer](https://gitcode.com/cann/ops-transformer)、[ops-math](https://gitcode.com/cann/ops-math)、[ops-cv](https://gitcode.com/cann/ops-cv)。
- 官方构建与版本：[ops-nn QUICKSTART](https://gitcode.com/cann/ops-nn/blob/master/docs/QUICKSTART.md)、[build 参数说明](https://gitcode.com/cann/ops-nn/blob/master/docs/zh/install/build.md)、[CANN release-management](https://gitcode.com/cann/release-management)。
- 官方采集：[Ascend/msopprof 用法](https://github.com/Ascend/msopprof/blob/master/docs/en/user_guide/msopprof_usage.md)、[msOpProf 用户指南](https://github.com/Ascend/msopprof/blob/master/docs/en/user_guide/msopprof_user_guide.md)、[Triton-Ascend profiling](https://github.com/triton-lang/triton-ascend/blob/main/docs/en/debug_guide/profiling.md)。
- 官方优化资料：[MatMul 全载](https://gitcode.com/cann/ops-nn/wiki/MatMul%E7%AE%97%E5%AD%90%E6%80%A7%E8%83%BD%E4%BC%98%E5%8C%96%E5%AE%9E%E8%B7%B5%E4%B8%8E%E6%95%88%E6%9E%9C%E5%88%86%E6%9E%90.md)、[MatMul VCV](https://gitcode.com/cann/ops-nn/wiki/MatMul%E7%AE%97%E5%AD%90VCV%E6%80%A7%E8%83%BD%E4%BC%98%E5%8C%96%E5%AE%9E%E8%B7%B5%E4%B8%8E%E6%95%88%E6%9E%9C%E5%88%86%E6%9E%90.md)、[Ascend C Tiling/L2 技术文章](https://www.hiascend.com/developer/techArticles/20240920-1)。
# 优化案例索引

> 只在完成 profiling 与 Bound 判定后使用。先按 DSL 过滤，再按算子类别、SoC 能力与瓶颈机制选择最多三个案例。案例数字只代表来源所述环境，不得直接作为当前算子的目标或预期收益。

## 使用规则

1. 来源优先级：官方已合并 PR/MR > 官方 Wiki/指南/sample > 仓库维护者 benchmark > 社区材料。
2. 标有 revert、open、二手来源、预期值或无等价基线的案例只能作为机制线索。
3. PR 未公开量化数字时，不得根据标题、代码量或指标变化推导加速比。
4. 选中案例后只打开对应文件，不得批量加载 `cases/`。

## 总览

| 框架 | 仓库 | 定位 | 底层 |
|---|---|---|---|
| catlass | <https://gitcode.com/cann/catlass>（gitee 旧仓 <https://gitee.com/ascend/catlass> 已停更迁移） | CANN 官方 C++ 模板元编程 GEMM/融合算子模板库（对标 NVIDIA CUTLASS） | Ascend C / CCE，头文件模板 |
| triton-ascend | <https://github.com/triton-lang/triton-ascend>（GitCode 镜像 Ascend/triton-ascend） | OpenAI Triton 语言的 Ascend NPU 后端 | Triton IR → AscendNPU IR → CCE |
| tilelang-ascend | <https://github.com/tile-ai/tilelang-ascend>（ascendc_pto 分支）、<https://github.com/tile-ai/tilelang-mlir-ascend>（npuir 分支） | 基于 TVM 的 Python tile DSL，昇腾后端 | 两条路线：Ascend C & PTO / AscendNPU IR (MLIR) |
| Ascend C | 官方文档 + <https://gitcode.com/cann/cann-samples>、<https://gitcode.com/cann/asc-tools>、gitee.com/ascend/samples | CANN 原生算子编程语言 | bisheng/CCE 编译器，msopgen 工程化 |

## 案例总览（按 DSL）

### catlass

| # | 类别 | PR/案例 | 类型 | 关键数字 | 警示 | 文件 |
|---|---|---|---|---|---|---|
| 1 | matmul | MR !507 matmul_fixpipe_opti | PR（新特性） | 未附量化数字 | — | [cases/catlass/pr_catlass_fixpipe_dualdst.md](cases/catlass/pr_catlass_fixpipe_dualdst.md) |
| 2 | matmul | MR !966 msTuner GetTiling 改运行时获取 | PR（Bug修复/refactor，附寻优数据） | tiling 寻优 164.028→43.291 us（~3.8x） | 非性能优化标签，数字来自测试节 | [cases/catlass/pr_catlass_mstuner_3p8x.md](cases/catlass/pr_catlass_mstuner_3p8x.md) |
| 3 | matmul | MR !978 grouped matmul default tiling | PR（修复） | 未附量化数字 | 属容量约束修复 | [cases/catlass/pr_catlass_grouped_matmul_tiling.md](cases/catlass/pr_catlass_grouped_matmul_tiling.md) |
| 4 | matmul | Swizzle `<3,1>`→`<4,1>` 负载均衡 | 非 PR（官方调优指引文档） | 40.6→35.3µs | 经腾讯云社区转载 | [cases/catlass/pr_catlass_swizzle_balance.md](cases/catlass/pr_catlass_swizzle_balance.md) |
| 5 | matmul | 整体性能基线（README 官方数据） | 非 PR | 定制 shape 达标杆 0.98~1.2 倍 | — | [cases/catlass/pr_catlass_baseline.md](cases/catlass/pr_catlass_baseline.md) |

### triton-ascend

| # | 类别 | PR/案例 | 类型 | 关键数字 | 警示 | 文件 |
|---|---|---|---|---|---|---|
| 6 | reduction | PR #208/#218 SIMT→SIMD 原子操作 | PR | atomic_add_3d 81.163→3.309µs（~24.5x） |  后被 revert（PR #376/#405） | [cases/triton/pr_triton_atomic_simd_24x.md](cases/triton/pr_triton_atomic_simd_24x.md) |
| 7 | vector-elementwise | PR #693 lgamma SIMT 优化 | PR | 性能分 0.12→0.5+（~4x） |  后被 PR #815 回退，#833 重开未遂 | [cases/triton/pr_triton_lgamma_simt.md](cases/triton/pr_triton_lgamma_simt.md) |
| 8 | misc | PR #7483 constexpr 重编译优化（vllm-ascend 生态） | PR | 未附量化数字 | — | [cases/triton/pr_triton_constexpr_recompile.md](cases/triton/pr_triton_constexpr_recompile.md) |
| 9 | misc | autotune/基础设施 PR（背景参考） | PR | 未见公开量化数字 | — | [cases/triton/pr_triton_autotune_background.md](cases/triton/pr_triton_autotune_background.md) |

### tilelang-ascend

| # | 类别 | PR/案例 | 类型 | 关键数字 | 警示 | 文件 |
|---|---|---|---|---|---|---|
| 10 | attention | PR #1390 persistent 调度 MQA decode | PR（抓取时 open） | Batch8 −35.2%（1.54x） |  作者声明"非标准不严谨测试" | [cases/tilelang/pr_tilelang_persistent_mqa_decode.md](cases/tilelang/pr_tilelang_persistent_mqa_decode.md) |
| 11 | attention | PR #698 高性能 FA | PR | PR 页无量化表 | 数字见官方 benchmark | [cases/tilelang/pr_tilelang_fa_pr698.md](cases/tilelang/pr_tilelang_fa_pr698.md) |
| 12 | attention | PR #665 SFA 性能与优化指南 | PR | 未附量化数字 | — | [cases/tilelang/pr_tilelang_sfa_pr665.md](cases/tilelang/pr_tilelang_sfa_pr665.md) |
| 13 | attention | PR #1494 GQA-BWD（Refactor 附 benchmark） | PR | BWD 34.42ms/12.98 TFLOPS | 用于说明与框架级实现的差距 | [cases/tilelang/pr_tilelang_gqa_bwd_pr1494.md](cases/tilelang/pr_tilelang_gqa_bwd_pr1494.md) |
| 14 | misc | 官方 benchmark：TileLang vs 手写 AscendC | 非 PR（README 官方数据） | GEMM 0.993x；hc_sinkhorn 1.028x | — | [cases/tilelang/pr_tilelang_benchmark_vs_ascendc.md](cases/tilelang/pr_tilelang_benchmark_vs_ascendc.md) |
| 15 | misc | 编译器级性能特性 PR（#113/#101/#74/#292） | PR | 无公开量化数字 | — | [cases/tilelang/pr_tilelang_compiler_prs.md](cases/tilelang/pr_tilelang_compiler_prs.md) |

### Ascend C

| # | 类别 | PR/案例 | 类型 | 关键数字 | 警示 | 文件 |
|---|---|---|---|---|---|---|
| 16 | matmul | 官方 Matmul 优化案例 | 非 PR（昇腾社区文章） | 11200→620us（~18x） | CSDN 镜像二手来源 | [cases/ascendc/pr_ascendc_matmul_case_18x.md](cases/ascendc/pr_ascendc_matmul_case_18x.md) |
| 17 | matmul | MR !168/!7782 RotateQuant Blaze 组件化迁移 | PR | 29.159→29.305us（+0.50%，例外验收） |  迁移性能持平案例，非提速案例 | [cases/ascendc/pr_ascendc_rotate_quant_blaze.md](cases/ascendc/pr_ascendc_rotate_quant_blaze.md) |
| 18 | vector-elementwise / multi-type | cann-samples 2_Performance story（13 案例） | 非 PR（官方递进教程） | flash_attn v0→v1 +96%；含 CUBE/VEC/SCALAR/SIMT/Pipeline/MEM 六类 bound 案例 | 含可运行代码 | [cases/ascendc/pr_ascendc_cann_samples_perf_story.md](cases/ascendc/pr_ascendc_cann_samples_perf_story.md) |
| 19 | communication | MC² 通算融合 | 非 PR（社区文章） | 端到端 1.3x–1.5x |  二手来源 | [cases/ascendc/pr_ascendc_mc2_fusion.md](cases/ascendc/pr_ascendc_mc2_fusion.md) |
| 20 | misc | MR !1589（ops-transformer）mHC 算子 | PR | mhc_pre 24x~52x | 对比 torch.einsum | [cases/ascendc/pr_ascendc_mhc_24x.md](cases/ascendc/pr_ascendc_mhc_24x.md) |
| 21 | vector-elementwise | MR !8443（ops-nn）Logit A5 多核均衡 + MicroAPI | PR | 原文未附量化数字 | 仅 A5 快路径；A2/A3 保留原实现 | [cases/ascendc/pr_ascendc_logit_a5_microapi.md](cases/ascendc/pr_ascendc_logit_a5_microapi.md) |
| 22 | vector-elementwise | MR !7420（ops-nn）Quantize A2 两轮微优化 | PR | 8 例合计 776.2→766.7us（1.012x） | 无同款 builtin，只是新 kernel 前后自比 | [cases/ascendc/pr_ascendc_quantize_a2_micro_opt.md](cases/ascendc/pr_ascendc_quantize_a2_micro_opt.md) |
| 23 | misc / convolution | MR !8276（ops-nn）Conv3DDX L1 TQue→TBuf | PR | 原文未附量化数字 | 只替换固定暂存，不可破坏流水队列 | [cases/ascendc/pr_ascendc_conv3ddx_tque_tbuf.md](cases/ascendc/pr_ascendc_conv3ddx_tque_tbuf.md) |
| 24 | reduction / norm | MR !8293（ops-nn）LayerNormV3 areg 地址优化 | PR | 原文未附量化数字 | 架构/编译器能力需实测 | [cases/ascendc/pr_ascendc_layernorm_areg.md](cases/ascendc/pr_ascendc_layernorm_areg.md) |
| 25 | matmul | ops-nn Wiki：小输入全载复用 | 非 PR（官方 Wiki） | 未抄录统一数字 | 按 SoC 重算片上容量 | [cases/ascendc/pr_ascendc_matmul_full_load_wiki.md](cases/ascendc/pr_ascendc_matmul_full_load_wiki.md) |
| 26 | matmul | ops-nn Wiki：VCV 流水优化 | 非 PR（官方 Wiki） | 未抄录统一数字 | 看全链路，不只看 Cube 利用率 | [cases/ascendc/pr_ascendc_matmul_vcv_wiki.md](cases/ascendc/pr_ascendc_matmul_vcv_wiki.md) |
| 27 | communication | MR !9716（ops-transformer）AllGatherMM Scalar + 尾块对齐 | PR | 原文未附量化数字 | 兼具性能与精度修复 | [cases/ascendc/pr_ascendc_allgathermm_scalar_align.md](cases/ascendc/pr_ascendc_allgathermm_scalar_align.md) |
| 28 | attention | MR !9692（ops-transformer）CGDR stage1 求逆 VF 化 | PR | 原文未附量化数字 | A5 VF 路径需覆盖数值稳定性 | [cases/ascendc/pr_ascendc_cgdr_vf_inverse.md](cases/ascendc/pr_ascendc_cgdr_vf_inverse.md) |
| 29 | vector-elementwise | msOpProf 官方 Add 核间负载判读 | 非 PR（官方文档） | Block0 7.456666us；Block2 10.001111us | 诊断数据，不是前后收益 | [cases/ascendc/pr_ascendc_msopprof_add_block_imbalance.md](cases/ascendc/pr_ascendc_msopprof_add_block_imbalance.md) |
| 30 | misc / tiling | Ascend C 官方 L2 工作集 Tiling | 非 PR（官方文章） | 原文原则入库，未写统一数字 | L2 参数随 SoC 变化 | [cases/ascendc/pr_ascendc_tiling_l2_official.md](cases/ascendc/pr_ascendc_tiling_l2_official.md) |

### SHMEM

| # | 类别 | PR/案例 | 类型 | 关键数字 | 警示 | 文件 |
|---|---|---|---|---|---|---|
| 31 | communication | MR !152（agent-skills）alltoallv 8PE full-mesh 调优交付 | PR | 带宽 68.99→79.75 GB/s（+15.6%）；e2e −13.5% | — | [cases/shmem/pr_shmem_alltoallv_mr152.md](cases/shmem/pr_shmem_alltoallv_mr152.md) |
| 32 | communication | reduce_scatter AIV 内跨 PE 串行链路消除 | 非 PR（agent-skills 优化模式库） | 串行实现带宽仅 HCCL 65%~71%；预期 +20% | 20% 为原文预期值 | [cases/shmem/pr_shmem_reduce_scatter_serial.md](cases/shmem/pr_shmem_reduce_scatter_serial.md) |

### PyPTO

| # | 类别 | PR/案例 | 类型 | 关键数字 | 警示 | 文件 |
|---|---|---|---|---|---|---|
| 33 | matmul | 多 Matmul 独立 TileShape（Decode Attention） | 非 PR（cannbot-skills 调优案例库） | 257.12→237.12us（-7.8%）；累计 -46.1% | — | [cases/pypto/pr_pypto_per_matmul_tile_shapes.md](cases/pypto/pr_pypto_per_matmul_tile_shapes.md) |
| 34 | matmul | 小 Shape 矩阵乘 Vector 预处理（I-1） | 非 PR（cannbot-skills 调优案例库） | 500→40us（~12.5x） | — | [cases/pypto/pr_pypto_small_shape_matmul.md](cases/pypto/pr_pypto_small_shape_matmul.md) |
| 35 | matmul | 大 Shape Matmul 分核布局/L2 命中率（S-13） | 非 PR（cannbot-skills 调优案例库） | 2.1→1.6ms（+31%，6144³） | — | [cases/pypto/pr_pypto_l2_hit_layout_s13.md](cases/pypto/pr_pypto_l2_hit_layout_s13.md) |
| 36 | attention | GQA Decode Attention Vector 合轴+合图 | 非 PR（cannbot-skills 调优案例库） | 275.44→258.98us（-6.0%）；任务数 -18.5% | — | [cases/pypto/pr_pypto_gqa_decode_vector_merge.md](cases/pypto/pr_pypto_gqa_decode_vector_merge.md) |
| 37 | misc | 权重矩阵批量 NONE_CACHEABLE（I-2，Pangu 7B） | 非 PR（cannbot-skills 调优案例库） | 437.28→354us（-19.1%） | — | [cases/pypto/pr_pypto_weight_none_cacheable.md](cases/pypto/pr_pypto_weight_none_cacheable.md) |
