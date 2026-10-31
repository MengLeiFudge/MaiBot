# MaiBot 聊天实例本机配置参考

MaiBot 运行时配置位于项目根 `config/`，整个目录包含 provider key 等本机信息，因此不进入 Git。首次部署先按当前稳定版生成完整 `config/bot_config.toml` 和 `config/model_config.toml`，再应用本目录中的固定部署值：

- `bot-identity.example.toml`：夜凛 QQ、显示名和 WebUI 端口。
- `lanling-identity.example.toml`：蓝铃身份及独立监听端口；应用到独立 `maibot-lanling/` 工作目录。
- `provider.example.toml`：只展示 provider 字段形状；`api_key` 必须在本机实际配置中填写。
- `napcat-adapter.example.toml`：合并到 `plugins/napcat_adapter/config.toml`，在适配器入口阻止已知机器人互相触发。

这些文件是覆盖参考，不是可直接替代上游完整配置的最小配置。实际配置还必须保留当前 MaiBot 版本生成的全部必需 section。

启动前还要确认：

- 夜凛 `plugins/napcat_adapter/config.toml` 使用 `127.0.0.1:6201` 和 `connection_id = "yelin"`；蓝铃使用 `127.0.0.1:6204` 和 `connection_id = "lanling"`。按 `napcat-adapter.example.toml` 配置全局禁止发送者名单，包含另外四个机器人账号。该名单先于私聊/群聊名单开关执行，不依赖业务插件。
- `plugins/qqbot_poke/config.toml`、`plugins/qqbot_knowledge/config.toml`、`plugins/qqbot_identity/config.toml` 和 `plugins/qqbot_visual/config.toml` 的顶层 `plugin.enabled` 为 `true`。知识插件通过 `127.0.0.1:8081` 使用云栖的共享 DSP 向量检索服务；身份插件在本机 `[identity].owner_qq` 填写真正主人 QQ，示例保持留空。
- 其余 `plugins/qqbot_*/config.toml` 的顶层 `plugin.enabled` 为 `false`。
- `src/plugins/built_in/plugin_management/config.toml` 为禁用状态。

`scripts/enforce_chat_only.py` 会在 Core 启动前复核并纠正后两项；配置无法解析时会阻止启动。`scripts/start.ps1` 通过 `read_startup_config.py` 从本实例完整配置读取名称和 WebUI 端口，PID、日志、数据库与虚拟环境均位于各自工作目录。首次部署蓝铃使用全新 `data/`，不复制其他账号的记忆、登录态或数据库。

`bot-identity.example.toml` 的 `[personality].behavior_style` 为 Planner 指定收尾条件：当前需求已完成且没有新的待回应消息时结束，不以 `wait` 轮询普通沉默；必要的信息查询完成后仍可继续回答。`max_consecutive_wait_count = 1` 限制连续定时等待。群聊无动作后的退避从 5 分钟起步，连续无动作上限为 10 分钟；私聊不受空闲退避限制，明确提及和 @ 沿用原生强制触发。Focus 状态按框架规则绕过空闲退避。

框架执行回复等工具后仍会调用一次 Planner 判断下一步；公开请求 Hook 不支持跳过这次调用。收尾提示用于减少后续多余工具调用，实际节省量需从 `data/MaiBot.db` 的 `llm_usage` 和 `maisaka_monitor_events` 对比，不能将提示词视为硬性轮次上限。

`qqbot_visual` 在接收 Hook 中门控非定向普通图片；私聊、群聊当前正文中的昵称/别名、本人 @ 或回复本人时，当前图片沿用原生识图。引用图只补充同会话已有描述，不重新识别。设置 `[visual].planner_mode = "text"`、`replyer_mode = "text"`、`wait_image_recognize_max_time = 15.0`，让文本规划最多等待图片后台描述 15 秒；设置 `emoji.steal_emoji = true`，保留表情组件的原始数据以进入原生本地表情收集链。门控和表情收集的具体边界见 [qqbot_visual](../../plugins/qqbot_visual/README.md)。框架内部对已发图、工具结果图启动的识图任务没有接收 Hook，插件不能拦截。

`qqbot_identity` 向 Planner 注入身份规则，当前 Core 的 Context Items 没有结构化发送者，Planner 身份保持未知；Replyer 按实际目标消息 ID 查询同会话发送者并注入 `platform/user_id/is_owner`。昵称、自称、人物画像和正文里的 QQ 都不能替代真实身份。详见两个插件的 README。

`provider.example.toml` 保持 Replyer 输出上限 4096，通过其独立模型配置 `reasoning.effort = "none"` 避免思考耗尽共享预算；其余任务及超时保持现有配置。
