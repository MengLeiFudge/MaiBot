# QQBot 定向看图

`qqbot_visual` 通过 MaiBot 1.2.3 的 `chat.receive.before_process` 门控入站媒体。它使用公开 SDK 能力，不修改框架，也不直接调用视觉模型。

将 `config.example.toml` 复制为本机 `config.toml` 并启用。本人 QQ、昵称和别名通过 `config.get` 读取 `bot.qq_account/nickname/alias_names`，跟随 `bot` 配置热重载。定向消息包括私聊、群聊中的本人真实 @、适配器 `at_bot`、回复本人，以及当前顶层正文含昵称或别名；机器人自己的发言排除。转发、引用正文和图片描述中的名字不参与呼叫判定。

非定向图片和表情保留已有描述，缺描述时写 `[图片]`/`[表情包]`，移除 `binary_data_base64`，保留组件 hash 和其余路由、发送者、时间及文本。转发中的媒体递归处理。原生组件遇到非空 content 会跳过 VLM 和保存，因此普通新群图不保证进入媒体文件缓存。

定向消息的当前附件沿原生链路处理。保持 `[visual].planner_mode = "text"`、`replyer_mode = "text"`，设置 `wait_image_recognize_max_time = 15.0`：文本规划最多等后台描述 15 秒，这不等于图片任务的超时。保持 `emoji.steal_emoji = false`，关闭自动收集表情。

对定向消息中的引用，插件最多用 3 秒通过 `message.get_by_id` 查询同会话目标，只把已有图片或表情描述补入回复组件。当前公开能力没有独立识图入口，不重新识别引用图片，也不下载或另存图片。查询失败保持原引用并记录异常类型。

接收 Hook 不覆盖框架内部对机器人已发图、工具结果图创建的后台识图任务；这些任务仍沿用原生行为。插件禁用、加载失败或 Hook 被跳过时，原生入站识图行为会恢复。日志只记录配置状态及错误类型，不记录消息、图片或账号内容。
