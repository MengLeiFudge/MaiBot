# CPA 会话透传

本地 Provider 插件通过 Planner、Replyer 的公开 Hook 取得真实会话 ID，并将其写入请求级 `X-Client-Request-Id`。CPA 按现有配置转为 OpenCode Go 的 `x-opencode-session`。请求使用自身 UA `QQBot/MaiBot-cpa-session/0.1.0`。

启用 `config.toml` 的 `plugin.enabled`，把 `config/model_config.toml` 中指向 CPA 的生成 provider 的 `client_type` 改成 `cpa_responses`，然后重启 MaiBot。保留 provider 名称、`deepseek-flash` 模型名、地址、密钥及生成参数。夜凛启动允许表已包含本传输插件。

插件直接调用框架现有的 `OpenAIResponsesClient.get_response`，使用原生请求类型和序列化函数；工具、图像、推理、HTTP 重试与流式读取沿用该客户端。会话关联与原生回放项仅缓存在内存，卸载清空。没有聊天会话来源的独立辅助调用使用本次逻辑请求 ID。

RPC 和框架源码均无需修改。模型路由与 CPA 同名渠道兜底见根工作区 [模型 API 路由](../../../docs/model-routing.md#opencode-go)。
