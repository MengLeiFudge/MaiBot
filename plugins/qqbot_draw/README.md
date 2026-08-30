# QQBot RightCodes 生图

MaiBot SDK 2.x 原生插件，承载现有 RightCodes 生图闭环：

- 群消息积分累计、积分查询和全局前 10 排行。
- 生图模型查看和固定命令切换。
- 按已保存模型预扣积分，调用 RightCodes 异步任务，失败或超时原额退款。
- 命令引用文字、当前图片或引用图片时，先用当前 `replyer` 模型整理提示词；整理失败不扣积分，不配置独立 provider 或 fallback。
- 当前图片与引用图片统一转换为 RightCodes `image` 字段要求的 Data URL，最多 3 张、单张最多 20 MiB。
- 私聊或直接 @ 的“生成……图片”自然语言只提示真实固定指令和积分副作用，不直接执行生图。
- 普通问答命中 RightCodes、接口路径、尺寸或模型关键词时，只向该次 replyer 请求临时注入官方接口摘要。
- 成功和失败结果通过 NapCat 原生 API 引用原请求；成功结果附带随机本地摘要，不调用额外 LLM。
- 同一群消息只由 `points.owner_self_id` 累计一次；命令通过公共 claim 选择唯一实例。

插件默认 `plugin.enabled = true` 且 `cutover.write_enabled = true`。AstrBot 已停止，MaiBot 是生图、积分累计与模型选择的唯一写入者；固定命令在 Planner 前执行，不存在“命中后静默丢弃”的关闭门禁。多实例仍由公共 claim 和 SQLite/文件锁保证一次执行。

`config.toml` 含真实 API Key，因此被插件仓库忽略。`config.example.toml` 是可提交模板。业务数据根默认由 `mlj.qqbot-common` 提供，迁移完成后可统一切换。
