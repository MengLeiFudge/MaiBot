# QQBot Lolicon 美图

MaiBot 固定命令插件，迁移旧 AstrBot Lolicon 图片能力和群级配置。插件默认启用，不需要额外打开迁移门禁。

## 指令

- `美图`、`来点美图`：请求非 R18 图片。
- `色图`、`涩图`、`蛇图`：请求 R18 图片。
- `混合`：请求混合模式图片。
- 图片命令后可写空格分隔的 tag，末尾数字作为数量；无 tag 时默认 1 张，有 tag 且未指定数量时默认 5 张，最多请求 20 张。
- `开群色图` / `关群色图`：仅主人、仅群聊，控制当前群是否允许 R18 或混合请求。
- `开图片显示` / `关图片显示`：仅主人、仅群聊，控制 R18 结果是否直接发送远程图片。关闭时发送原图 URL 文本；非 R18 图片仍直接发送。

群聊和私聊命令都会在固定命令链中确定性消费，并通过 `qqbot.route.claim` 选择唯一执行实例。图片和文本使用 NapCat API 发送。

## 数据兼容

- 群配置继续读写公共运行数据根 `db/qqbot_features.sqlite3` 的 `json_state` 表，namespace 为 `settings.lolicon`，兼容旧 `group_r18` / `show_image` 结构。
- 图片元数据继续写入公共运行数据根 `db/lolicon.sqlite3` 的 `images` 表。
- 图片直接使用 Lolicon API 返回的远程 URL，不下载、不建立本地图片缓存。
- SQLite 连接设置 `busy_timeout=30000`，不切换共享数据库 journal mode，也不启用 WAL。

`storage.runtime_root_override` 留空时通过 `qqbot.storage.runtime_root` 获取公共数据根。Lolicon API 不需要密钥；默认端点为 `https://api.lolicon.app/setu/v2`。

## 配置

示例见 `config.example.toml`。`permissions.owner_qq` 默认是 `605738729`；部署时可以通过 MaiBot 插件配置覆盖。
