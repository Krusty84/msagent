# MSAgent 体验报告
## 一、报告基本信息
- 用例ID：UC001
- 测试场景：环境搭建验证
- 执行命令：`msagent --version`
- 预期结果：返回 `msAgent v26.1.0a2`
- 实际结果：msAgent v26.1.0a2
- 测试结论：通过
![alt text](chrome_5vwQOXW2kK.png)


- 用例ID：UC002
- 测试场景：模型与API配置
- 执行命令：`msagent config --show`
- 预期结果：显示正确的Provider、Base URL和模型名称
- 实际结果：配置正确显示，API Key未明文暴露
- 测试结论：通过
![alt text](chrome_6dJFu1TOVq.png)



---

## 二、交互记录
### 第 1 轮交互
| 项目 | 内容 |
|------|------|
| 输入 Prompt | hi |
| Agent 输出（文字摘要） | |
| 是否符合预期 | 是 |
| 评价 |正常运行|
![alt text](chrome_QK4bYcQwaX.png)
### 第 2 轮交互

| 项目 | 内容 |
|------|------|
| 输入 Prompt | 当前有哪些能力？你能完成哪些性能分析任务？|
| 是否符合预期 | 是 |
| 评价 | 正常运行|
![alt text](chrome_is5RBrWBZ5.png)

### 第 3 轮交互

| 项目 | 内容 |
|------|------|
| 输入 Prompt |请检查以下Profiling数据是否完整，是否能够进行后续性能分析：

/workspace/msagent-task/data/Qwen3-32B

请列出：
1. 数据格式；
2. Rank数量；
3. 关键文件；
4. 缺失项；
5. 是否可以继续分析。 |
| Agent 输出（文字摘要） |  【类型】 框架 profiler（Ascend PyTorch Profiler）| 多卡（4 Rank） 【状态】 ✅ valid —— 采集正常 Stop，已解析，交付件完整，可直接分析                        

  ------------------------------------------------------------------------------------------------------------------------------------------------------------

  1. 数据格式                                                                                                                                                 

   • 类型：*_ascend_pt（PyTorch 框架 Profiler，CANN 底层 msprof 采集）                                                                                        
   • 导出模式：Text（export_type: ["text"]），同时附带 DB 交付件                                                                                              
   • profiler 等级：Level1（含 AI Core 指令流水指标 ACL_AICORE_PIPE_UTILIZATION）                                                                             
   • 采集范围：CPU + NPU 双活动                                                                                                                               

  2. Rank 数量                                                                                                                                                

  4 个 Rank（Rank 0–3），由 4 个 ascend_pt 目录组成：                                                                                                         

                                                                
   Rank  目录                                                   
   ──────────────────────────────────────────────────────────── 
   0     df536040f370_651_..._ascend_pt（profiler_info_0.json） 
   1     df536040f370_654_..._ascend_pt（profiler_info_1.json） 
   2     df536040f370_657_..._ascend_pt（profiler_info_2.json） 
   3     df536040f370_664_..._ascend_pt（profiler_info_3.json） 
                                                                

  profiler_metadata.json 确认通信域 global_ranks: [0,1,2,3]（Qwen3-32B，4 卡训练）。另含 cluster_analysis_output/cluster_analysis.db（376K，集群分析输出）。  

  3. 关键文件（每个 Rank 均齐全）                                                                                                                             

                                                                                                                                                    
   文件                                                         状态                          说明                                                  
   ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── 
   profiler_info_{rank}.json                                    ✅                            采集正常 Stop                                         
   trace_view.json                                              ✅ 53MB                       Timeline，可解析（已验证 Overlap Analysis 输出正常）  
   kernel_details.csv                                           ✅ 5.6MB / 17,641 行 / 46 列  含 AIC/AIV 流水、cube 利用率、Block Dim、Wait Time 等 
   op_statistic.csv / operator_details.csv / api_statistic.csv  ✅                            算子/A PI 统计                                        
   step_trace_time.csv                                          ✅                            计算/通信/调度时间                                    
   communication.json / communication_matrix.json               ✅                            通信详情与矩阵                                        
   ascend_pytorch_profiler_{rank}.db（23MB） + analysis.db      ✅                            统一 DB（含 CommAnalyzer 表）                         
                                                                                                                                                    

  4. 缺失项 / 注意事项                                                                                                                                        

   • ⚠️ 框架侧增强配置未开启：record_shapes=false、with_stack=false、with_modules=false、profile_memory=false。这不影响算子/Timeline/通信分析，但无法做框架侧 
     shape 关联、栈回溯定位和内存 profile 分析（注：kernel_details.csv 内 CANN 层 Input Shapes 字段仍可用）。                                                 
   • ℹ️ Rank1 额外生成 trace_view_mindstudio_insight_data.db（含 -wal/-shm 残留），其他 Rank 无此文件。该文件是 MindStudio Insight                            
     视图的可选交付件，不阻塞分析，可忽略。                                                                                                                   
   • ℹ️ scheduling: {} 为空 → 采集为手动 start/stop 全量采集，无 warmup/active 步数限制。                                                                     

  5. 是否可继续分析                                                                                                                                           

  ✅ 可以，数据完整、valid，无需重新采集或解析。                                                                                                              

  初步快览（Rank0 Overlap Analysis，总时长 1766ms）：Free 36.26% > Communication 25.36%（几乎全部 Not Overlapped）> Computing 13.03% ——                       
  提示通信暴露度高、调度空闲大，值得深入。    |
| 是否符合预期 | 是 |
| 评价 | 正常运行|
![alt text](chrome_xmrhJKxJAW.png) ![alt text](chrome_qGAar8x9OK.png) ![alt text](chrome_Ungn1loXDh.png)

### 第 4 轮交互

| 项目 | 内容 |
|------|------|
| 输入 Prompt |请分析该Profiling数据是否存在集群快慢卡。

要求：
1. 给出异常Rank；
2. 比较各Rank的Computing、Communication和Free时间；
3. 给出具体数据证据；
4. 区分真正的慢卡和等待慢卡的受害卡。 |
| 是否符合预期 | 是 |
| 评价 | 正常运行|
![alt text](chrome_uLcZJj3prF.png) ![alt text](chrome_pibyPvLjev.png) ![alt text](chrome_rL6l7LJgsz.png) ![alt text](chrome_U8asDHMf3e.png)

### 第 5 轮交互

| 项目 | 内容 |
|------|------|
| 输入 Prompt |请重点分析该数据中的下发、调度和Host Bound问题。

请检查：
1. 是否存在异常长的Free切片；
2. Host侧是否存在算子下发延迟；
3. 同步API是否存在异常；
4. 是否可能与CPU绑核、NUMA、CPU抢占或内存锁竞争有关；
5. 给出具体排查命令。 |
| 是否符合预期 | 是 |
| 评价 | 正常运行|
![alt text](chrome_mLKG2SdbNA.png) ![alt text](chrome_PhRyYNO80H.png) ![alt text](chrome_dy1anDKmiD.png) ![alt text](chrome_G4lOTqnbAU.png) ![alt text](chrome_JASwKpAhP5.png) ![alt text](chrome_Mckg1srscu.png)

### 第 6 轮交互

| 项目 | 内容 |
|------|------|
| 输入 Prompt |请根据以上分析生成一份完整诊断结论，包括：

1. 数据概况；
2. 快慢卡结论；
3. 关键证据；
4. 根因判断；
5. 影响评估；
6. 优化建议；
7. 需要使用MindStudio Insight验证的内容。 |
| 是否符合预期 | 是 |
| 评价 | 正常运行|
![alt text](chrome_8InnvV4Ixp.png) ![alt text](chrome_B9W6kxuOTT.png) ![alt text](chrome_h1w4Ezwy2d.png) ![alt text](chrome_iGUJuXNxxP.png) ![alt text](chrome_k97RH0HKk8.png) ![alt text](chrome_0AcpWWiYYa.png)

*（根据实际对话轮次自行增删）*
---

## 三、多轮交互整体评价
| 评价维度 | 评分（1~5） | 说明 |
|----------|-------------|------|
| 问题理解准确性 | 5| |
| 数据分析深度 |5 | |
| 证据链条完整性 |5 | |
| 优化建议实用性 |5 | |
| 多轮对话连贯性 | 5| |
| 响应速度与稳定性 |5 | |
| 整体满意度 |5 | |



---
## 四、问题与改进建议
### 发现的问题
1. 
2. 
3. 
### 改进建议
1. 在快慢卡结论中同时输出各Rank原始指标和计算过程。
2. 每个根因结论应关联具体文件、字段和时间范围。
3. 增加分析进度、当前步骤和预计剩余阶段提示。


## MindStudio Insight验证结果

| 验证项 | msagent结论 | Insight结果 | 是否一致 |
|---|---|---|---|
| 异常Rank | | | |
| Computing时间 | | | |
| Communication时间 | | | |
| Free时间 | | | |
| 最大Free切片 | | | |
| 根因判断 | | | |

### 概览截图
![alt text](MindStudio-Insight_VrN4u3zJt0.png)


### 时间线Free切片
![alt text](MindStudio-Insight_4cEmJO9K3h.png) ![alt text](MindStudio-Insight_fmRrVmZpRo.png) ![alt text](MindStudio-Insight_PYqhMAYH2Z.png) ![alt text](MindStudio-Insight_Se2VtIMwWK.png)
