# 仿真模式必须使用 sim 编译的可执行文件

## 场景

用户使用 `msprof op simulator --soc-version=Ascend910B4 ./demo` 拉起仿真分析时，报错：

```
terminate called after throwing an instance of 'std::__ios_failure'
  what():  basic_filebuf::xsgetn error reading the file: Bad address
[WARN]  Child process killed by signal 6
```

## 根因

算子可执行文件（demo）如果是安装 **npu** 模式编译的，仿真器无法直接加载和运行这种二进制。仿真模式要求算子工程使用 `sim` 选项编译，生成的可执行文件才兼容仿真器。

## 解决方案

使用 `--simulator` 编译的算子工程。例如：

```bash
# 编译时cmake选项选择仿真模式
cmake -DCMAKE_ASC_RUN_MODE=sim -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..; make -j

# 然后用编译产物拉起仿真
msprof op simulator --soc-version=Ascend910B4 --output=./sim_output ./demo
```

## 验证方式

- 编译目录下有 `CMakeCache.txt` 中包含 simulator 相关配置
- 仿真命令执行后无 `signal 6` / `Bad address` 报错，日志中可见 `[INFO] Running simulation task: Binary Simulation Running` 并正常生成 `simulator/` 目录及各核 CSV/trace.json

## asc算子编译选项说明

| 选项 | 说明 |
|------|------|
| `CMAKE_ASC_RUN_MODE` | 指定为`sim`, 开启NPU仿真模式 |
| `CMAKE_ASC_ARCHITECTURES` | 指定NPU架构版本号，CMake会根据该值配置对应的CPU调试依赖库。<br>`dav-2201` 对应 Atlas A2/A3 系列，`dav-3510` 对应 Ascend 950PR/Ascend 950DT |

## 关联信息

- 日期：2026-04-22
- 芯片：Ascend910B4
- CANN 版本：9.0.0
- 算子：MatmulLeakyRelu（Ascend C）
