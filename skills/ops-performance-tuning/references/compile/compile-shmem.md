# SHMEM / MC2：构建、运行与验证

通信算子的构建必须覆盖 Host 启动、Kernel、rank/PE 配置和通信初始化。不同仓的 API、启动器和通信库版本差异较大，因此先提取工程契约，不假设统一命令。

## 1. 识别工程契约

```bash
cd <repo_root>
rg -n "shmem|aclshmem|HCCL|rank|world_size|n_pes|symmetric|alltoall|reduce_scatter" \
  . --glob '!build/**' --glob '!output/**'
rg -n "build.sh|run.sh|mpirun|ranktable|add_executable|kernel" \
  CMakeLists.txt scripts/ tests/ examples/ 2>/dev/null
```

记录：CANN/driver/firmware、通信库版本、SoC、设备与 rank 映射、节点拓扑、消息大小、dtype、PE 数、build/run 命令、正确性条件以及 HCCL 标杆。

## 2. 构建与精度门禁

1. 使用目标仓文档指定的 CANN 和通信库组合，同时编译 Host 与 Kernel；不得复用其他 CANN 版本的二进制。
2. 保存每个 rank 的 stdout/stderr 和退出码；任一 rank 异常均视为失败。
3. 对比完整输出、通信元素数和 rank 映射；不以“程序退出 0”替代正确性验证。
4. 先跑 HCCL 或工程正式标杆，再跑 SHMEM/MC2；两者使用相同拓扑、消息量、dtype、warmup 和 repeat。
5. **设备前提检查（Step 1 就要做）**：`rankNum ≤ 可用空闲设备数`。host 侧 `deviceId = rankId` 的样例每 rank 独占一个物理设备，`ASCEND_RT_VISIBLE_DEVICES` 可见设备数不足时 rank 会在 `aclrtSetDevice` 报 `aclError:107001` 或对端阻塞超时——不要等到 Step 3/4 才发现。rankNum=1 退化运行通常挂起且失去通信语义，不能当基线。cann-samples 抽离案例的构建树重建见 [compile-ascendc.md §4.3.1](compile-ascendc.md)（`shmem.cmake` 会现场编译 third_party/shmem，已有 install 产物优先复用）。

## 3. 性能口径与采集

同时记录 e2e、kernel、algBw、busBw、每 rank 最大值与均值。通信性能由最慢 rank 决定，禁止只报告平均值。

仅当本机帮助明确支持多设备/MC2 时使用 `msprof op`；否则使用完整 `msprof`。每个 rank 的输出目录必须独立，命令、rank 映射、kernel 清单和采集失败项必须归档。详见 [msOpProf 采集指南](../profile/profile-msopprof.md)。

## 4. 调优入口

完成通信量、热点 PE、串行链路、同步次数和通算重叠的证据表后，读取 [SHMEM 优化技术](../optimize/optimize-shmem.md)，再从 [案例路由](../case-routing.md) 选择最多三个同拓扑案例。每轮只修改一个分片、缓冲、信号、核角色或 overlap 机制，并重做所有 rank 的精度与性能验证。
