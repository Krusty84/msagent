# PyPTO：构建、运行与验证

PyPTO 工程没有跨仓通用的一条构建命令。先识别目标仓的图构建入口、JIT/离线编译方式、精度测试和 benchmark，再沿用工程现有环境；禁止凭案例文件名自创 API 或启动参数。

## 1. 识别工程契约

```bash
cd <repo_root>
rg -n "pypto|TileShape|runtime_debug_mode|torch\.allclose|benchmark|warmup" \
  . --glob '!build/**' --glob '!output/**'
rg -n "add_executable|pyproject|setup.py|build.sh|run.sh|pytest" \
  CMakeLists.txt pyproject.toml setup.py scripts/ tests/ examples/ 2>/dev/null
```

记录：PyPTO/CANN/torch_npu 版本、目标图入口、输入 shape/dtype、编译缓存目录、正确性命令、正式 benchmark、warmup/repeat、最终 kernel 清单。

## 2. 构建与精度门禁

1. 按目标 branch 文档安装配套依赖并执行仓内构建脚本；保存完整命令和 commit。
2. 删除或隔离旧版本编译缓存，再执行一次冷启动确认能够成功生成图与 kernel。
3. 正式性能基线排除首次 JIT/图编译，只统计固定 warmup 后的稳态执行。
4. 使用工程既有 golden 和容差覆盖完整目标 shape；任一失败即停止性能分析。

## 3. 泳道与 profiling

需要确认图、任务或泳道时，可在当前版本 API 明确支持的配置位置临时启用 `debug_options={"runtime_debug_mode": 1}`。记录生成文件及其版本相关字段；完成定位后关闭调试模式并重建 Release 基线。

用 [msOpProf 采集指南](../profile/profile-msopprof.md) 枚举图实际启动的全部 kernel。JIT 编译时间、host 调度时间和各 kernel event 时间分别记录，禁止合并成一个“kernel 时延”。

## 4. 调优入口

完成 Bound 判定后读取 [PyPTO 优化技术](../optimize/optimize-pypto.md)，再从 [案例路由](../case-routing.md) 选择最多三个 PyPTO 同型案例。每次只调整一个图融合、轴合并、TileShape、缓存属性或泳道机制，并重做完整精度和稳态性能测试。
