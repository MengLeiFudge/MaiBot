# CPA 会话透传

本地 Provider 插件通过 Planner、Replyer 的公开 Hook 取得真实会话 ID，并将其写入请求级 `X-Client-Request-Id`。CPA 按现有配置转为 OpenCode Go 的 `x-opencode-session`。请求使用自身 UA，版本与插件发布版本一致。

启用 `config.toml` 的 `plugin.enabled`，把 `config/model_config.toml` 中指向 CPA 的生成 provider 的 `client_type` 改成 `cpa_responses`，然后重启 MaiBot。保留 provider 名称、`deepseek-flash` 模型名、地址、密钥及生成参数。夜凛启动允许表已包含本传输插件。

插件直接调用框架现有的 `OpenAIResponsesClient.get_response`，使用原生请求类型和序列化函数；工具、图像、推理、HTTP 重试与流式读取沿用该客户端。会话关联与原生回放项仅缓存在内存，卸载清空。没有聊天会话来源的独立辅助调用使用本次逻辑请求 ID。

本轮首条公开回复送达前，新消息可以取代旧输入：图片或记忆准备期间收到补充，若 Planner 快照尚未包含补充，就跳过旧模型调用；Planner、Replyer 请求中收到新消息时取消本插件的对应请求。模型刚返回时再次核对，旧规划不执行工具、旧正文不进入发送；原生循环重新收集消息，保留图片、补充文字、人设及完整回复器。首条补等期间由 [qqbot_reply_timing](../qqbot_reply_timing/README.md) 再次核对源消息。

首条明确发送成功后，本轮后续分段和研究补充继续完成，新消息由下一轮处理。发送失败不关闭取消资格；已经提交到平台、结果未知的发送不能擅自撤回或重发。取消的模型请求返回 `cancelled`、空输出和未知用量，不伪造已生成的正文或零成本；无聊天来源的辅助请求保持原处理。

RPC 和框架源码均无需修改。模型路由与 CPA 同名渠道兜底见根工作区 [模型 API 路由](../../../docs/model-routing.md#opencode-go)。
