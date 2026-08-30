# QQBot 养鲲

MaiBot SDK 2.x 原生命令插件，迁移现有养鲲状态和命令处理逻辑。

插件默认 `plugin.enabled = true` 且 `cutover.write_enabled = true`。AstrBot 已停止，MaiBot 是养鲲状态的唯一写入者；命令在 Planner 前执行，不再保留会命中后静默丢弃的关闭门禁。多实例仍通过公共 claim 和进程间锁串行修改共享状态。

存储规则：

- 默认通过 `mlj.qqbot-common` 读取统一 runtime 根。
- 兼容首次读取旧 `data/kun/*.json`。
- 新事实写入 `db/qqbot_features.sqlite3`。
- 多 MaiBot 实例使用命令 claim 和进程间文件锁，避免同一命令重复执行或 JSON/SQLite 状态并发覆盖。
- 等级与财富排行榜逐个调用公共 `qqbot.identity.display_name` API；显示名规则完全由 `qqbot_common` 负责，单个 API 调用失败、返回缺失或空值时仅将该用户稳定回退为 QQ，不影响其余榜单。

当前命令包括养鲲、摸鲲、属性、洗练、Boss、排行、商城、签到、查看/进击/赠送等原有入口。
