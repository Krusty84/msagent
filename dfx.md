# msagent DFX 优化记录

| 场景 | 原报错及原因 | DFX 修改方案 |
| --- | --- | --- |
| Agent 处理包含 `error` 的日志时，流式模型响应失败 | 原报错：`Error processing message: No generations found in stream`。该错误表示 OpenAI 兼容模型服务返回了空的流式响应，LangChain 无法聚合出 generation；并非日志文本中出现 `error` 本身导致。原始异常对用户缺少可行动说明。 | 在消息错误格式化中识别该异常，提示“模型流式接口返回空响应、本次请求未完成、会话仍可继续、请稍后重试”。保留完整异常链、线程、Agent、模型等信息写入日志，便于定位服务端空流问题。 |
| Agent 执行任务时模型服务触发 429 限流 | 原报错：`Error processing message: Error code: 429`。现有 `retry.model.max_retries` 已由底层 LLM 客户端执行；最终出现 429 表示配置的重试次数仍未恢复。原始报错未说明重试状态和会话影响。 | 不新增第二套重试逻辑，继续使用既有 `retry.model.max_retries`。在消息错误格式化中识别 429、`Error code: 429` 和 rate-limit 信息，提示“配置的重试已耗尽、本次请求未完成、会话仍可继续、请稍后重试”；详细 HTTP/模型上下文保留在日志中。 |
| 上下文过长触发自动压缩时，tokenizer 下载因 SSL 证书失败 | 原报错：`Error compressing conversation: HTTPSConnectionPool(...openaipublic.blob.core.windows.net...cl100k_base.tiktoken...CERTIFICATE_VERIFY_FAILED...)`。LLM tokenizer 不支持计数后会尝试加载 `tiktoken` 的 `cl100k_base`；首次加载需要联网下载，内网自签名证书使下载失败。原逻辑未覆盖 LLM tokenizer 的这类运行时异常，且直接将底层 URL/证书异常展示给用户。 | token 统计改为：LLM tokenizer 失败时尝试 `tiktoken`，仍失败时使用离线字符估算，避免辅助计数阻断压缩。压缩失败时终端仅提示“压缩未完成，已返回结果和现有会话已保留；达到上下文限制时可新建会话”，详细异常写入 warning 日志，不再直接暴露 SSL 细节。 |
