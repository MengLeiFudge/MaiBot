# QQBot 公共能力

这是拆分式 QQBot -> MaiBot 功能源码的公共插件，不直接提供聊天命令。夜凛最小插件允许策略中本插件必须保持 `plugin.enabled = false`；源码仅为后续与 AstrBot 同步维护固定功能而保留。

代码提供的内部能力包括：

- 阻止已配置机器人账号触发固定命令、普通聊天和记忆链。
- 规范 SDK 2.7 的跨插件 API 返回值并统一处理 Host / OneBot 动作错误。
- 为多个 MaiBot 实例提供固定命令唯一执行 claim。
- 按当前群保存群名片和 QQ 昵称。
- 向其他 `qqbot_*` 插件公开统一业务数据根。

`storage.legacy_runtime_root` 留空时使用当前 MaiBot 插件数据目录，不读取 qqbot 根目录或 AstrBot 运行态。只有未来明确启用这些业务插件时才需要设置独立的 MaiBot 数据根；不得指向云栖 AstrBot 数据。

公开 API：

- `qqbot.route.claim`
- `qqbot.identity.display_name`
- `qqbot.storage.runtime_root`

启动前的 `scripts/enforce_chat_only.py` 会把本插件和除 `qqbot_poke`、`qqbot_knowledge`、`qqbot_identity`、`qqbot_visual` 外的其他 `qqbot_*` 插件实际配置顶层 `plugin.enabled` 强制改回 `false`。
