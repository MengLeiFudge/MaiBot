# MaiBot 聊天实例本机配置参考

MaiBot 运行时配置位于项目根 `config/`，整个目录包含 provider key 等本机信息，因此不进入 Git。首次部署先按当前稳定版生成完整 `config/bot_config.toml` 和 `config/model_config.toml`，再应用本目录中的固定部署值：

- `bot-identity.example.toml`：夜凛 QQ、显示名和 WebUI 端口。
- `lanling-identity.example.toml`：蓝铃身份及独立监听端口；应用到独立 `maibot-lanling/` 工作目录。
- `provider.example.toml`：只展示 provider 字段形状；`api_key` 必须在本机实际配置中填写。
- `napcat-adapter.example.toml`：合并到 `plugins/napcat_adapter/config.toml`，在适配器入口阻止已知机器人互相触发。

这些文件是覆盖参考，不是可直接替代上游完整配置的最小配置。实际配置还必须保留当前 MaiBot 版本生成的全部必需 section。

启动前还要确认：

- 夜凛 `plugins/napcat_adapter/config.toml` 使用 `127.0.0.1:6201` 和 `connection_id = "yelin"`；蓝铃使用 `127.0.0.1:6204` 和 `connection_id = "lanling"`。按 `napcat-adapter.example.toml` 配置全局禁止发送者名单，包含另外四个机器人账号。该名单先于私聊/群聊名单开关执行，不依赖业务插件。
- `plugins/qqbot_poke/config.toml`、`plugins/qqbot_knowledge/config.toml`、`plugins/qqbot_identity/config.toml`、`plugins/qqbot_visual/config.toml` 和 `plugins/qqbot_web_search/config.toml` 的顶层 `plugin.enabled` 为 `true`。知识插件通过 `127.0.0.1:8081` 使用云栖的共享 DSP 向量检索服务；身份插件在本机 `[identity].owner_qq` 填写真正主人 QQ，示例保持留空。
- 蓝铃作为独立 bot，按本机各插件的 `plugin.enabled` 决定启用状态，不参与云栖与夜凛之间的家族插件同步或统一允许策略。
- 蓝铃的聊天插件管理入口默认禁用，其 `src/plugins/built_in/plugin_management/config.toml` 由本机独立维护。

`scripts/validate_plugin_config.py` 在 Core 启动前校验现有插件配置的 TOML 及布尔值 `plugin.enabled`；配置无效时阻止启动，启用状态保持本机选择。`scripts/start.ps1` 通过 `read_startup_config.py` 从本实例完整配置读取名称和 WebUI 端口，PID、日志、数据库与虚拟环境均位于各自工作目录。首次部署蓝铃使用全新 `data/`，不复制其他账号的记忆、登录态或数据库。

`bot-identity.example.toml` 的 `[personality].behavior_style` 为 Planner 指定收尾条件：当前需求已完成且没有新的待回应消息时结束，不以 `wait` 轮询普通沉默；必要的信息查询完成后仍可继续回答。`max_consecutive_wait_count = 1` 限制连续定时等待。群聊无动作后的退避从 5 分钟起步，连续无动作上限为 10 分钟；私聊不受空闲退避限制，明确提及和 @ 沿用原生强制触发。Focus 状态按框架规则绕过空闲退避。

框架执行回复等工具后仍会调用一次 Planner 判断下一步；公开请求 Hook 不支持跳过这次调用。收尾提示用于减少后续多余工具调用，实际节省量需从 `data/MaiBot.db` 的 `llm_usage` 和 `maisaka_monitor_events` 对比，不能将提示词视为硬性轮次上限。

`qqbot_visual` 在接收 Hook 中门控非定向普通图片；定向附件沿用原生识图，接收阶段的引用只补充同会话已有描述。设置 `emoji.steal_emoji = true`，保留表情组件的原始数据以进入原生本地表情收集链。蓝铃私聊另在 Planner 和 Replyer 请求 Hook 中直接补入同会话图片及表情图像，动态表情抽取代表帧，也支持普通引用中的原图，详见 [qqbot_visual](../../plugins/qqbot_visual/README.md)。保持 `[visual].planner_mode = "text"`、`replyer_mode = "text"` 和 `wait_image_recognize_max_time = 15.0`；仅蓝铃私聊通过插件补图，群聊普通图片仍走原有策略。框架内部对已发图、工具结果图启动的识图任务没有接收 Hook，插件不能拦截。

蓝铃的共享自身状态和私聊主动续聊配置见 [qqbot_proactive](../../plugins/qqbot_proactive/README.md)。活动、心情和休息期限由同一份持久状态提供给所有私聊与群聊；休息时暂停主动联系，并在 QQ 发送前拦截其他会话发言。状态工具成功后才生效，发起会话保留短暂告别窗口，到期或明确醒来后恢复。共享摘要只包含机器人自身状态，各会话的聊天原文和长期记忆仍按原范围保存。主人身份复用本机身份插件配置，两类状态分别保存在该插件标准数据目录的 `self_state.json` 与 `schedule.json`。

蓝铃的个人工作区和文件工具配置见 [qqbot_workspace](../../plugins/qqbot_workspace/README.md)。`D:\project\lanling` 与整个 `D:\project\qqbot\maibot-lanling` 项目目录均可读写，项目内不按文件或配置字段另设授权；其他本机位置只读。文件工具绑定已有主人私聊的真实会话，项目文件的具体使用由提示词和主人后续指引约束。

每日成品由 better-diary 负责，取材绑定主人私聊，23:30 自动生成，当日成品按配置公开发布空间；本机配置参考 [better-diary.example.toml](better-diary.example.toml)。[qqbot_diary](../../plugins/qqbot_diary/README.md) 只读上游成品存档并同步到个人工作区，保留手写历史与人工修改。空间使用独立的公开日记写作说明，不把完整聊天口吻带入公开日记。

日程意图由工作区 `计划/日程.json` 保存，`qqbot_proactive` 的既有调度读取当天安排，并在 00:00–08:00 暂停主动联系；日程不等于已执行活动，静默窗口不替代共享休息状态。麦麦恋人与主动私聊 Plus 已退出蓝铃部署，其源码、配置和数据可恢复归档，不再安装第二套好感度或主动调度。

蓝铃的私聊规划漏执行检查见 [qqbot_reply_guard](../../plugins/qqbot_reply_guard/README.md)，用于核对只有分析而没有调用 `reply` 的待回复消息。

`qqbot_identity` 从同会话消息 API 读取真实发送者，为 Planner 中已经出现的消息补充身份表，Replyer 则按实际目标消息 ID 查询并注入 `platform/user_id/is_owner`。只有已核验的主人使用“主人”称呼，其他人和未知身份使用昵称或“你”；本机回复风格不得把所有第二人称替换成“主人”。昵称、自称、人物画像和正文里的 QQ 都不能替代真实身份。详见 [qqbot_identity](../../plugins/qqbot_identity/README.md)。

`provider.example.toml` 的生成 provider 使用本机 CPA 地址 `http://127.0.0.1:8317/v1` 与 CPA 客户端密钥。联网搜索单独使用官网凭据，部署方法见 [qqbot_web_search](../../plugins/qqbot_web_search/README.md)；嵌入 provider 保留原百炼接口，CPA 支持 `/embeddings` 前不能一起替换地址。

`provider.example.toml` 将 Planner、Replyer、识图、记忆与其他文本辅助任务统一设为 `reasoning.effort = "max"`，实际模型仍是支持视觉的 `deepseek-flash`。各任务推理与正文共享 8192 token，任务硬超时 60 秒，provider 请求超时 120 秒。蓝铃本机保留原有任务模型分配；拍一拍与主动续聊判断也提供相同输出余量。接话条件、规划轮数、主动联系间隔和重试策略沿用现有配置。输出上限不代表固定消耗量，按正常聊天后的实际账单观察费用。

蓝铃所有群和私聊的长期记忆配置见 `lanling-identity.example.toml` 的 `a_memorix` 部分：关闭顶层聊天准入过滤，启用人物事实写回、聊天摘要写回、启发式召回和 Episode 自动生成；摘要消息窗口阈值为 12、回看上下文长度为 36、普通记忆查询默认取回 5 条，启发式召回在当前聊天新增 12 条消息后才会再次考虑。后台任务会增加模型调用，不做历史批量回填；现有历史是否纳入首次摘要由原生窗口决定。全局记忆共享、群到私聊及私聊到群的跨会话召回保持关闭，各会话在自己的范围读写记忆。嵌套 `filter.retrieval` 只限制跨会话检索，不改变本会话访问。

回复延迟先区分 `llm_usage` 的模型时间、`maisaka_monitor_events` 的 Planner/工具时间和回复工具内 `detail.metrics`。蓝铃的 `response_post_process.typing_speed = 0.2` 缩短模型完成后的模拟打字等待；不因此关闭 Episode、改变召回频率或降低思考强度。Planner 的阶段时间还包含记忆印象整理，不能把它等同于主模型耗时；行为分析超时也可能延长部分回合，提速效果需以正常聊天样本观察。
