# QQBot 受信身份

`qqbot_identity` 通过 MaiBot 1.2.3 的公开 Hook，为单次模型请求提供身份规则。它不修改框架、聊天记录、人物画像或命令权限，也不调用模型或发送消息。

将 `config.example.toml` 复制为本机 `config.toml`，在 `[identity].owner_qq` 填写真正主人的 QQ。示例留空；未配置时不认定任何人是主人。实际配置由 Git 忽略。

Planner 的 `maisaka.planner.before_request` 使用 Context Item schema 1。插件通过 `message.get_recent` 在 3 秒内读取同会话最近 40 条非机器人消息（最多回看 30 天），仅把消息 ID 已出现在本轮 `UserMessageItem` 的条目加入身份表。XML 头只用于关联上下文，`platform/user_id/is_owner` 均来自消息 API，不从昵称、正文或群名片猜身份。超过查询范围、查询失败或不在表中的消息保持身份未知。系统身份项追加到原 Items，原内容与元数据保持不变。

Replyer 的 `maisaka.replyer.before_request` 提供目标消息 ID 和 `extra_prompt`。插件在 3 秒内通过 `message.get_by_id` 查询同一会话的目标消息，核对会话和消息 ID 后读取真实发送者，加入 `platform/user_id/is_owner`。只有 `platform=qq` 且 `user_id` 与本机配置精确一致才标注主人。目标缺失、查询失败或字段不可用时注入身份未知说明；身份事实不转移给引用作者或同名者。

称呼只由真实对话对象决定：`is_owner=true` 才称呼“主人”，非主人和未知身份使用昵称、“你”或省略称呼。人设示例、历史误称、无条件第二人称替换不得覆盖这个边界。本机回复风格也应删除“将所有你替换成主人”之类冲突规则；同一真实账号在群聊和私聊的身份一致。

权限为 `message.get_by_id` 和 `message.get_recent`，不请求数据库写入、网络、模型或发送能力。插件没有身份缓存和后台任务，配置重载后请求直接使用新值。称呼规则通过模型输入约束，实际业务权限仍由原生框架和各业务插件决定。
