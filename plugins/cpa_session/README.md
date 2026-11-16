# CPA 会话透传

本地 Provider 插件通过 Planner、Replyer 的公开 Hook 取得真实会话 ID，并将其写入请求级 `x-opencode-session`，由 CPA 透传给 OpenCode Go。请求使用自身 UA，版本与插件发布版本一致。

启用 `config.toml` 的 `plugin.enabled`，把 `config/model_config.toml` 中指向 CPA 的生成 provider 的 `client_type` 改成 `cpa_responses`，然后重启 MaiBot。保留 provider 名称、`deepseek-flash` 模型名、地址、密钥及生成参数。夜凛启动允许表已包含本传输插件。

插件直接调用框架现有的 `OpenAIResponsesClient.get_response`，使用原生请求类型和序列化函数；工具、图像、推理、HTTP 重试与流式读取沿用该客户端。会话关联与原生回放项仅缓存在内存，卸载清空。没有聊天会话来源的独立辅助调用使用本次逻辑请求 ID。

本轮首条公开回复送达前，同一源消息发送者的新补充可以取代旧输入。私聊按会话、群聊按会话及发送者跟踪接收顺序；其他群成员的消息照常进入完整上下文，等待后续轮次处理，不撤销当前模型请求和首段发送。`chat.reply_timing.planner_interrupt_max_consecutive_count = 0`，由本插件处理相关补充的取消，避免原生任意消息中断与插件取消重叠。首条补等使用 [qqbot_reply_timing](../qqbot_reply_timing/README.md) 的相同发送者判断。
被新补充取消的规划、正文和待发送首段，通过公开 `maisaka.proactive.trigger` 请求原生循环重新排队，每个旧轮次只排队一次；不依赖空响应后的普通群聊频率和空闲等待。插件声明该能力权限，排队失败保留错误日志。图片、补充文字、人设、原生 Planner、Replyer 和后处理流程完整保留。

首条明确发送成功后，本轮后续分段和研究补充继续完成，新消息由下一轮处理。发送失败不关闭取消资格；已经提交到平台、结果未知的发送不能擅自撤回或重发。取消的模型请求返回 `cancelled`、空输出和未知用量，不伪造已生成的正文或零成本；无聊天来源的辅助请求保持原处理。

框架当前的插件通信单帧上限为16 MiB；完整图片上下文超过该上限时，请求在进入插件前就失败，现有公开配置和 SDK 未提供分帧或共享媒体引用。此限制仍需框架提供相应能力，不能靠插件减少图片、删上下文或把媒体改成占位规避。模型路由与 CPA 同名渠道兜底见根工作区 [模型 API 路由](../../../docs/model-routing.md#opencode-go)。
