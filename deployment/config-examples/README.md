# MaiBot 聊天实例本机配置参考

MaiBot 运行时配置位于项目根 `config/`，整个目录包含 provider key 等本机信息，因此不进入 Git。首次部署先按当前稳定版生成完整 `config/bot_config.toml` 和 `config/model_config.toml`，再应用本目录中的固定部署值：

- `bot-identity.example.toml`：风铃 QQ、角色、显示名和独立监听端口；保留技术 Target `xingyao`。
- `provider.example.toml`：只展示 provider 字段形状；`api_key` 必须在本机实际配置中填写。
- `napcat-adapter.example.toml`：合并到 `plugins/napcat_adapter/config.toml`，在适配器入口阻止已知机器人互相触发。

这些文件是覆盖参考，不是可直接替代上游完整配置的最小配置。实际配置还必须保留当前 MaiBot 版本生成的全部必需 section。

启动前还要确认：

- 本实例 `plugins/napcat_adapter/config.toml` 使用 `127.0.0.1:6202` 和 `connection_id = "xingyao"`。按 `napcat-adapter.example.toml` 配置全局禁止发送者名单，包含另外三个现行机器人账号。该名单先于私聊/群聊名单开关执行，不依赖业务插件。
- `plugins/qqbot_poke/config.toml`、`plugins/qqbot_knowledge/config.toml`、`plugins/qqbot_identity/config.toml`、`plugins/qqbot_visual/config.toml`、`plugins/qqbot_web_search/config.toml` 和 `plugins/qqbot_reply_timing/config.toml` 的顶层 `plugin.enabled` 为 `true`。知识插件通过 `127.0.0.1:8081` 使用云栖的共享 DSP 向量检索服务；身份插件在本机 `[identity].owner_qq` 填写真正主人 QQ，示例保持留空。
- 同时启用 `org_orge-8_better-diary`、`qqbot_diary`、`qqbot_workspace`、`qqbot_proactive` 和 `qqbot_reply_guard`，合计十三个通用插件；其余 `plugins/qqbot_*/config.toml` 的 `plugin.enabled` 为 `false`。各插件说明见其 README，完整名单见根 [聊天实例配置](../../../docs/chat-instances.md)。
- `src/plugins/built_in/plugin_management/config.toml` 为禁用状态。

`scripts/enforce_chat_only.py` 在 Core 启动前校验允许列表、配置及聊天插件管理禁用状态，无法确认时停止。`scripts/start.ps1` 从本实例完整配置读取名称和端口，各自维护 PID、日志、数据库和虚拟环境。风铃使用全新角色数据，不继承星遥或注销账号的经历；原数据可恢复归档在根 `.codex/trash/`。

风铃个人工作区为 `D:/project/fengling`。文件工具首次部署等待真实主人私聊绑定，绑定前不注册工具；不伪造会话 ID。better-diary 每天 23:30 从本账号主人私聊生成成品，参考 `better-diary.example.toml`，默认不公开发布空间，也不补写新角色建立前的旧日记；`qqbot_diary` 导出到自己的 `日记/better-diary`，不复制蓝铃的日记、记忆或状态。

`bot-identity.example.toml` 的 `[personality].behavior_style` 为 Planner 指定收尾条件：当前需求已完成且没有新的待回应消息时结束，不以 `wait` 轮询普通沉默；必要的信息查询完成后仍可继续回答。`max_consecutive_wait_count = 1` 限制连续定时等待。群聊无动作后的退避从 5 分钟起步，连续无动作上限为 10 分钟；私聊不受空闲退避限制，明确提及和 @ 沿用原生强制触发。Focus 状态按框架规则绕过空闲退避。

框架执行回复等工具后仍会调用一次 Planner 判断下一步；公开请求 Hook 不支持跳过这次调用。收尾提示用于减少后续多余工具调用，实际节省量需从 `data/MaiBot.db` 的 `llm_usage` 和 `maisaka_monitor_events` 对比，不能将提示词视为硬性轮次上限。

`qqbot_visual` 完整保留入站群聊与私聊附件；设置 `[visual].planner_mode = "multimodal"`、`replyer_mode = "multimodal"`，由支持视觉的主模型读取原图。表情收集沿用 `emoji.steal_emoji`。插件提供可向前翻页的 `read_chat_history`，详见 [消息与媒体阅读](../../plugins/qqbot_visual/README.md)。近期上下文保留现有群聊 40、私聊 60 条；30 条只是主动查阅的默认页长。统一边界见根工作区 [消息完整性与按需触发](../../../docs/message-context.md)。

`qqbot_identity` 向 Planner 和 Replyer 注入真实发送者的 `platform/user_id/is_owner`。Planner 以消息头中的 ID 关联同会话消息 API 返回的最新人类消息；Replyer 按实际目标消息 ID 查询。昵称、自称、人物画像和正文里的 QQ 都不能替代真实身份。知识证据也同时提供给两条路径，详见对应插件 README。

`provider.example.toml` 的生成 provider 使用本机 CPA 地址 `http://127.0.0.1:8317/v1` 与 CPA 客户端密钥。联网搜索单独使用官网凭据，部署方法见 [qqbot_web_search](../../plugins/qqbot_web_search/README.md)；嵌入 provider 保留原百炼接口，CPA 支持 `/embeddings` 前不能一起替换地址。

`provider.example.toml` 将 Planner、Replyer、识图、记忆与其他文本辅助任务统一设为 `reasoning.effort = "high"`，实际模型仍是支持视觉的 `deepseek-flash`。各任务推理与正文共享 8192 token，任务硬超时 60 秒，provider 请求超时 90 秒；拍一拍使用 `utils` 任务并提供相同输出余量。官网搜索使用独立的 32768 输出上限。输出上限不代表固定消耗量，按正常聊天后的实际账单观察费用。

普通及阶段答复统一使用原生 `reply`、回复器、后处理分段及发送流程。[回复时机插件](../../plugins/qqbot_reply_timing/README.md) 补齐出站连接标识与首条等待。[CPA 会话插件](../../plugins/cpa_session/README.md) 取消同发送者新补充到来时的过期请求，群内其他成员不打断当前答复；首条成功后继续本轮分段。原生 `planner_interrupt_max_consecutive_count = 0`，避免与按发送者取消重复。
