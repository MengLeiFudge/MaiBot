# MaiBot Pi 协作对等插件

维护与 AstrBot 相同的 HTTP 协议和批次行为，使用 MaiBot 公开 Hook、普通 LLM 文本生成及 NapCat 公开消息 API。当前 plugin.enabled=false；账号合同规定 AstrBot 为副作用功能唯一运行方。不要在 QQ 中启用本插件。

## 迁移使用

以后由用户明确切换运行账号时，先停用旧入口，再填写本插件 bridge 配置和 Pi bridge.json 中完全一致的 bridge_id、database_id、generation、task_id、platform_id、bot_id、token。MaiBot 的平台为 qq，默认账号为夜凛 2629227874，主人固定为 605738729。Pi 支持本地配置绑定不同 bot_id，不从 HTTP 请求接受新目标。

本插件不导入 AstrBot 源码。运行目录使用 SDK ctx.paths.data_dir；更换绑定或世代前归档旧 queue.sqlite3，防止旧消息改投。配置模板 token 为空，保持禁用；真实配置和数据不能提交 Git。完整服务端配置与迁移说明见 pi-collab/docs/bridge.md。

## 对等行为

- Hook 在普通聊天前截获“@机器人 需求 …”纯文本，按适配器结构化账号/群/发送者核验来源；引用、转发不作为直接需求。
- 每群 10 条或 30 分钟触发一批；先核对鉴权及绑定，才通过 SDK llm.generate 做一次无工具汇总。
- 原文、冻结批次和尝试预算持久化；HTTP 重试复用已生成摘要。全机器人 UTC 日默认最多 24 次尝试，含失败；模型失败后至少等30分钟再试该批。
- 原文默认容量 16 MiB/10000 条，七天清理；满时拒收。摘要、来源审计及决定由 Pi 长期保存。
- 待决卡片只私聊主人；命令“确认 <决策ID> <选项>”仅允许主人真实私聊事件。普通群需求和摘要都不是执行授权。
- 一句结论仅回原批次群。QQ 发送与本地回执之间崩溃仍可能重复，输出附稳定 ID。
- on_unload/config reload 停止旧循环，再关闭队列；默认禁用时不启动网络或模型工作。

验证限静态源码、配置解析与类型检查。本轮保持默认禁用，不修改 Core、账号路由、框架配置或插件启用策略。
