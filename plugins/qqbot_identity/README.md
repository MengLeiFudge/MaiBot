# QQBot 受信身份

`qqbot_identity` 通过 MaiBot 1.2.3 的公开 Hook，为单次模型请求提供身份规则。它不修改框架、聊天记录、人物画像或命令权限，也不调用模型或发送消息。

将 `config.example.toml` 复制为本机 `config.toml`，在 `[identity].owner_qq` 填写真正主人的 QQ。示例留空；未配置时不认定任何人是主人。实际配置由 Git 忽略。

Planner 的 `maisaka.planner.before_request` 使用 Context Item schema 1。插件添加一个 `SystemMessageItem`，保留全部原 Items 和元数据。当前接口没有结构化的发送者 `platform/user_id`，所以 Planner 只得到身份规则和身份未知说明；不能从昵称、群名片、正文 QQ、自称或画像推断主人。

Replyer 的 `maisaka.replyer.before_request` 提供目标消息 ID 和 `extra_prompt`。插件在 3 秒内通过 `message.get_by_id` 查询同一会话的目标消息，核对会话和消息 ID 后读取真实发送者，加入 `platform/user_id/is_owner`。只有 `platform=qq` 且 `user_id` 与本机配置精确一致才标注主人。目标缺失、查询失败或字段不可用时注入身份未知说明；身份事实不转移给引用作者或同名者。

权限仅需 `message.get_by_id`，不请求数据库写入、网络、模型或发送能力。插件没有身份缓存和后台任务，配置重载后请求直接使用新值。身份规则是模型输入约束，实际业务权限仍由原生框架和各业务插件决定。
