# 目前存在的问题

1. 精度异常定位能力作为subagent还是skill？个人倾向subagent。编排类走主会话加载 skill；执行类（重流程、多工具）由 subagent 承载，主会话只做委派和汇总。fp-vs-quant 是典型的执行类——7 个步骤、拉 vllm 服务、采集 dump、多轮脚本调用，与 quantize/evaluate 同级，属于 Quantizer 的能力集。

2. 拉vllm服务在测评subagent中存在，是否可以复用或者提取为独立的skill？个人倾向提取为独立的skill。（不对，好像是用的msmodelslim的集成的服务化能力

3. fp-vs-quant是否需要集成在 orchestrator 调优工作流中，这个功能应该不要加入循环调优，相对调优工作流应该独立一些。
