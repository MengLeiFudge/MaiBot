# QQBot 戳一戳

MaiBot 原生拍一拍插件，通过 `chat.receive.before_process` Hook 在普通聊天、Planner 和 A-Memorix 之前消费 NapCat 通知。

行为边界：

- 只响应 `target_id == 当前 self_id` 的真人群拍击；其他拍击通知也会被拦截，不进入聊天和记忆。
- 状态按 `group_id + self_id` 存于内存，插件重载即清空。
- 60 秒内聚合当前群所有真人拍击；平静、不耐烦、恼火、MUTE 四阶段不向群友公开计数和阈值。
- 非 MUTE 阶段通过 SDK 的 `model="replyer"` 使用当前 MaiBot 普通聊天任务模型做一次受控 JSON 决策：无视、反拍、短文字；恼火阶段可额外选择进入 MUTE。模型随主配置的 `model_task_config.replyer` 调整。
- 反拍和禁言目标始终由代码固定为当前拍击者，模型不能指定用户、群或时长。
- MUTE 期间不调用 AI，每次未处于成功禁言冷却的拍击直接随机禁言当前拍击者 30-90 秒。
- OneBot 确认禁言成功后，按 `group_id + self_id` 进入默认 90 秒内存冷却；冷却不按拍击者拆分，期间后续拍击在聊天和记忆前静默消费，不再调用 AI 或禁言。失败或异常不启动该冷却。
- AI 租约、冷却和状态代际会阻止并发重复响应及旧请求发送过期动作。

该插件默认在 MaiBot 实例启用；它不读取或写入业务数据库。
