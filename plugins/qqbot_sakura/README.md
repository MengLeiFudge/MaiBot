# QQBot 落樱之都

MaiBot SDK 2.x 原生固定命令插件，迁移落樱之都基础玩法及现有角色状态。部署后 `plugin.enabled` 和 `cutover.write_enabled` 默认都为 `true`，命令会在 Planner/普通聊天链之前真实执行。只有需要紧急停写时才关闭 `cutover.write_enabled`；关闭后命令会明确回复功能已暂停，不会写入状态或落入聊天链。未注册角色使用个人信息、改名或状态修改命令时，会统一提示先注册。

支持的入口完整对应旧 `SAKURA_PATTERN`：

- `落樱之都`、`更新日志`、`玩法`
- `注册<名字>`、`改名<名字>`、`个人信息`
- `加经验<数字>`、`嘤<数字>`
- `恢复`、`回复`
- `加<数字>力量/智力/体质/敏捷/魅力`

状态规则：

- 使用公共 runtime 根下的 `db/qqbot_features.sqlite3`，namespace 保持为 `sakura.players`。
- 数据库没有该 namespace 时，只读导入旧 `db/sakura/players.json` 或 `data/sakura/players.json`，不会修改旧文件。
- SQLite 保持 DELETE journal，不启用 WAL。
- 每条已接管命令先调用 `qqbot.route.claim`；胜出的实例在 `sakura.lock` 进程锁内重新加载并写入，避免双实例丢失更新。
