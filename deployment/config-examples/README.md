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
- 蓝铃作为独立 bot，按本机各插件的 `plugin.enabled` 决定启用状态，不参与云栖与夜凛之间的家族插件同步或统一允许策略。
- 蓝铃的聊天插件管理入口默认禁用，其 `src/plugins/built_in/plugin_management/config.toml` 由本机独立维护。

`scripts/validate_plugin_config.py` 在 Core 启动前校验现有插件配置的 TOML 及布尔值 `plugin.enabled`；配置无效时阻止启动，启用状态保持本机选择。`scripts/start.ps1` 通过 `read_startup_config.py` 从本实例完整配置读取名称和 WebUI 端口，PID、日志、数据库与虚拟环境均位于各自工作目录。首次部署蓝铃使用全新 `data/`，不复制其他账号的记忆、登录态或数据库。

`bot-identity.example.toml` 的 `[personality].behavior_style` 为 Planner 指定收尾条件：当前需求已完成且没有新的待回应消息时结束，不以 `wait` 轮询普通沉默；必要的信息查询完成后仍可继续回答。`max_consecutive_wait_count = 1` 限制连续定时等待。群聊无动作后的退避从 5 分钟起步，连续无动作上限为 10 分钟；私聊不受空闲退避限制，明确提及和 @ 沿用原生强制触发。Focus 状态按框架规则绕过空闲退避。

框架执行回复等工具后仍会调用一次 Planner 判断下一步；公开请求 Hook 不支持跳过这次调用。收尾提示用于减少后续多余工具调用，实际节省量需从 `data/MaiBot.db` 的 `llm_usage` 和 `maisaka_monitor_events` 对比，不能将提示词视为硬性轮次上限。

`qqbot_visual` 在接收 Hook 中门控非定向普通图片；定向附件沿用原生识图，接收阶段的引用只补充同会话已有描述。设置 `emoji.steal_emoji = true`，保留表情组件的原始数据以进入原生本地表情收集链。蓝铃私聊另在 Planner 和 Replyer 请求 Hook 中直接补入同会话图片及表情图像，动态表情抽取代表帧，也支持普通引用中的原图，详见 [qqbot_visual](../../plugins/qqbot_visual/README.md)。保持 `[visual].planner_mode = "text"`、`replyer_mode = "text"` 和 `wait_image_recognize_max_time = 15.0`；仅蓝铃私聊通过插件补图，群聊普通图片仍走原有策略。框架内部对已发图、工具结果图启动的识图任务没有接收 Hook，插件不能拦截。

蓝铃的私聊主动续聊配置见 [qqbot_proactive](../../plugins/qqbot_proactive/README.md)。主人身份复用本机身份插件配置，主动次数状态保存在该插件的标准数据目录。

蓝铃的个人工作区和文件工具配置见 [qqbot_workspace](../../plugins/qqbot_workspace/README.md)。工作区固定为 `D:\project\lanling`，工作区外只读；文件工具绑定已有主人私聊的真实会话，模型无权通过工具参数扩大写入位置。

蓝铃的私聊规划漏执行检查见 [qqbot_reply_guard](../../plugins/qqbot_reply_guard/README.md)，用于核对只有分析而没有调用 `reply` 的待回复消息。

`qqbot_identity` 向 Planner 注入身份规则，当前 Core 的 Context Items 没有结构化发送者，Planner 身份保持未知；Replyer 按实际目标消息 ID 查询同会话发送者并注入 `platform/user_id/is_owner`。昵称、自称、人物画像和正文里的 QQ 都不能替代真实身份。详见两个插件的 README。

`provider.example.toml` 将 Planner、Replyer、识图、记忆与其他文本辅助任务统一设为 `reasoning.effort = "max"`，实际模型仍是支持视觉的 `deepseek-flash`。各任务推理与正文共享 8192 token，任务硬超时 60 秒，provider 请求超时 120 秒。蓝铃本机保留原有任务模型分配；拍一拍与主动续聊判断也提供相同输出余量。接话条件、规划轮数、主动联系间隔和重试策略沿用现有配置。输出上限不代表固定消耗量，按正常聊天后的实际账单观察费用。

蓝铃所有群和私聊的长期记忆配置见 `lanling-identity.example.toml` 的 `a_memorix` 部分：关闭顶层聊天准入过滤，启用聊天摘要自动写回。原生消息窗口阈值与摘要上下文长度均为 36；达到窗口后异步生成并存储摘要，检索通过原生 `query_memory`。这不代表逐句永久保存，也不保证开启后立即记起过去的全部聊天；现有历史是否纳入首次摘要由原生窗口决定，不执行额外批量回填。全局记忆共享、群到私聊及私聊到群的跨会话召回保持关闭，各会话在自己的范围读写记忆。嵌套 `filter.retrieval` 只限制跨会话检索，不改变本会话访问。
