# 夜凛本机配置参考

MaiBot 运行时配置位于项目根 `config/`，整个目录包含 provider key 等本机信息，因此不进入 Git。首次部署先按当前稳定版生成完整 `config/bot_config.toml` 和 `config/model_config.toml`，再应用本目录中的固定部署值：

- `bot-identity.example.toml`：夜凛 QQ、显示名和 WebUI 端口。
- `provider.example.toml`：只展示 provider 字段形状；`api_key` 必须在本机实际配置中填写。
- `napcat-adapter.example.toml`：合并到 `plugins/napcat_adapter/config.toml`，在适配器入口阻止已知机器人互相触发。

这些文件是覆盖参考，不是可直接替代上游完整配置的最小配置。实际配置还必须保留当前 MaiBot 版本生成的全部必需 section。

启动前还要确认：

- `plugins/napcat_adapter/config.toml` 使用 `127.0.0.1:6201` 和 `connection_id = "yelin"`，并应用 `napcat-adapter.example.toml` 的全局禁止发送者名单。该名单先于私聊/群聊名单开关执行，不依赖业务插件。
- `plugins/qqbot_poke/config.toml`、`plugins/qqbot_knowledge/config.toml`、`plugins/qqbot_identity/config.toml` 和 `plugins/qqbot_visual/config.toml` 的顶层 `plugin.enabled` 为 `true`。知识插件通过 `127.0.0.1:8081` 使用云栖的共享 DSP 向量检索服务；身份插件在本机 `[identity].owner_qq` 填写真正主人 QQ，示例保持留空。
- 其余 `plugins/qqbot_*/config.toml` 的顶层 `plugin.enabled` 为 `false`。
- `src/plugins/built_in/plugin_management/config.toml` 为禁用状态。

`scripts/enforce_chat_only.py` 会在 Core 启动前复核并纠正后两项；配置无法解析时会阻止启动。

`qqbot_visual` 在接收 Hook 中对非定向图片和表情保留已有描述，缺描述时设置 `[图片]`/`[表情包]` 并移除二进制负载，原生组件处理因 content 非空跳过识图及保存。因此普通新图不保证保存在媒体缓存。私聊、群聊当前正文中的昵称/别名、本人 @ 或回复本人时，当前附件沿用原生识图。引用图只补充同会话已有描述，不重新识别。设置 `[visual].planner_mode = "text"`、`replyer_mode = "text"`、`wait_image_recognize_max_time = 15.0`，让文本规划最多等待图片后台描述 15 秒；保持 `emoji.steal_emoji = false`。框架内部对已发图、工具结果图启动的识图任务没有接收 Hook，插件不能拦截。

`qqbot_identity` 向 Planner 注入身份规则，当前 Core 的 Context Items 没有结构化发送者，Planner 身份保持未知；Replyer 按实际目标消息 ID 查询同会话发送者并注入 `platform/user_id/is_owner`。昵称、自称、人物画像和正文里的 QQ 都不能替代真实身份。详见两个插件的 README。

`provider.example.toml` 保持 Replyer 输出上限 4096，通过其独立模型配置 `reasoning.effort = "none"` 避免思考耗尽共享预算；其余任务及超时保持现有配置。
