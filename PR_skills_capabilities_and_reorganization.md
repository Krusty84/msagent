# PR 合入模板

**PR 标题：**【bugfix】Agent驱动LLM模型量化调优性能提升与skill分类重组

**注：经过自检不涉及的可标注"不涉及"或直接打勾，特殊情况请文字备注。不符合规范的 PR 不允许合入，请（后备）commit 注意。**

----

## 1. 修改描述

- **修改原因：**
  1. 量化调优场景缺少数据集压缩测评、EP 并行适配、结构化回退经验库等能力，需补充；同时各 skill 需按领域分类重组，便于扩展维护。
  2. **【bugfix】** skill 分类重组后，`SkillFactory` 的 category 由目录结构推导（`factory.py:156-157`），旧版扁平安装（`.msagent/skills/<skill-name>/`）会导致 category 被推导为 `default`，与 `Quantizer.yml` 的 `skills.patterns: - quantizer:*` 及 `get_skill(name, category="quantizer")` 不匹配，导致 Skill 找不到（`ToolException: Skill not found`）。
- **修改内容：**
  1. **新增 AISBench 数据集压缩测评 skill**（`benchmark/aisbench-dataset-compression-herding`）：支持 RBF Kernel Herding 生成 coreset（aime2025/gpqa），用于量化调优快速迭代
  2. **新增 MoE 模型 EP 并行适配 skill**（`quantizer/msmodelslim-ep-parallel-adaptation`）：含结构门禁 Check 1~6 + 数值门禁 Check 7（单卡量化 vs 多卡 EP 量化逐层激活余弦相似度对比）
  3. **新增量化结构化回退经验库 skill**（`quantizer/quantization-expert-experience-tuning-rules`）：L1/L2/L3 三级结构，覆盖 DeepSeek、GLM、MiniMax 等模型
  4. **编排层能力增强**：EP 路由决策、服务化推理脚本接入、双出口标准（子集+全集）与子集收紧规则
  5. **skills 目录分类重组**：将 `aisbench-dataset-compression-herding` 移至 `benchmark/`，`msmodelslim-ep-parallel-adaptation` 和 `quantization-expert-experience-tuning-rules` 移至 `quantizer/`，确保 category 推导正确
  6. **新增 PR 文档**（`PR_skills_capabilities_and_reorganization.md`），含部署同步注意事项（必须保留 `<category>/<skill-name>/` 层级，否则 skill 无法被识别）

----

## 2. 功能验证

- [ ] **功能自验**
- [ ] **本地自验用例截图**
- [ ] **冒烟是否通过** （填入群链接的自验证报告中，如未通过，请说明原因：____________________ ，功能代码请主动申报添加冒烟）

----

## 3. 分支合并要求

- [ ] **代码合并**（请确保将 master 分支的最新代码同步合并至 poc 分支及 pre-research 分支，同时保证 poc 分支的代码也已正确合并到 pre-research 分支。）

----

## 4. 代码检视

- **要求：**
  - 合入代码超过 200 行，需三人以上会议检视。
  - 检视密度≥1个/100行。
  - 检视缺陷密度未达要求需提供说明。
  - 大于 1000 行代码原则上不允许合入，需进行备案。
- [ ] **是否经过代码检视**
- [ ] **是否具备 UT 测试用例看护** （如不符合，请说明原因：____________________）

- **检视意见数：____ 条** （请填写本次检视的意见总数，用于commit合入前审视）

----

## 5. 安全自检

### Python、C++

- [x] **对外接口新增/删除/变更后，资料要同步新增/删除/变更，新增接口入参校验参考外部输入表格** — 不涉及（仅文档和 skill 配置变更）
- [x] **不允许私有的文件操作，需要使用公共模块的安全函数** — 不涉及
- [x] **任务结束后需要删除临时文件，同时需要考虑任务失败后，临时文件没有残留** — 不涉及
- [x] **数组访问需要校验越界场景，对除法需要做除零校验** — 不涉及
- [x] **需要对递归方法做递归深度校验，正则表达式必须做 ReDoS 校验** — 不涉及
- [x] **需要充分进行接口输入和返回值异常情况的校验** — 不涉及
- [x] **日志打印不要出现拼写或语法错误，不要暴露代码细节和敏感信息** — 不涉及

### C++

- [x] 不涉及

----

## 6. 变更知会

- [ ] **资料修改**
- [ ] **变更通知（消息知会 + 邮件知会）**

----

## 7. 部署注意事项（本次 skill 目录重组/新增后必须同步）

**背景：** 本 PR 将 `quantization-accuracy-tuning-orchestrator` 等 skill 按领域分类重组（移入 `skills/quantizer/`），并新增了 `references/routing.md`、`references/user_input.md` 等文件。若目标环境（如 `/home/<user>/xxx/.msagent/skills/`）仍在使用旧版本 skill，会出现 `references/routing.md` / `references/user_input.md` 找不到的报错。

**合入后必须完成以下同步：**

> ⚠️ **重要：category 由目录结构推导**（`SkillFactory._load_skill_file`）。`skills/quantizer/xxx/` → category=`"quantizer"`，`skills/benchmark/xxx/` → category=`"benchmark"`。因此 `.msagent/skills/` 下**必须保留分类层**，否则 `get_skill(name, category="quantizer")` 会找不到。

```bash
# 1. 在目标环境（如 /home/<user>/xxx/.msagent/skills/）下按分类目录批量同步
#    必须保留 quantizer/、benchmark/ 等分类层，不能拍平

cd <仓库路径>/msagent

# 同步 quantizer 分类
cp -r skills/quantizer/quantization-accuracy-tuning-orchestrator \
      <目标环境>/.msagent/skills/quantizer/quantization-accuracy-tuning-orchestrator

cp -r skills/quantizer/msmodelslim-ep-parallel-adaptation \
      <目标环境>/.msagent/skills/quantizer/msmodelslim-ep-parallel-adaptation

cp -r skills/quantizer/quantization-expert-experience-tuning-rules \
      <目标环境>/.msagent/skills/quantizer/quantization-expert-experience-tuning-rules

# 同步 benchmark 分类
cp -r skills/benchmark/aisbench-dataset-compression-herding \
      <目标环境>/.msagent/skills/benchmark/aisbench-dataset-compression-herding

# 2. 核对关键文件
ls <目标环境>/.msagent/skills/quantizer/quantization-accuracy-tuning-orchestrator/references/
# 应包含：routing.md  user_input.md  quantization_tuning.md  prepare_model.md  subagent_io_protocol.md 等
```

**注意：**
- **必须保留分类目录**：`<category>/<skill-name>/` 结构，不可拍平为 `<skill-name>/`，否则 category 推导为 `default`，与 agent 配置（`Quantizer.yml` 中的 `skills.patterns: - quantizer:*`）不匹配
- 若目标环境已有旧版扁平安装（`.msagent/skills/<skill-name>/`），需先删除或备份，再按新版结构同步
- 本次新增/移动的 skill 均需同步：`quantizer/quantization-accuracy-tuning-orchestrator`、`quantizer/msmodelslim-ep-parallel-adaptation`、`quantizer/quantization-expert-experience-tuning-rules`、`benchmark/aisbench-dataset-compression-herding`
- 若使用 wheel 安装，则需重新构建打包（`hatch_build.py` 会递归打包 `skills/` 全目录，含分类层和 references）