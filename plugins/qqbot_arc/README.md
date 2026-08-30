# QQBot ARC

MaiBot 原生 Arcaea 推荐、活动、猜歌、后台同步和作者限定 APK 更新插件，插件 ID 为 `mlj.qqbot-arc`。

## 接管范围

- `arctj10.5`：按 PTT 从本地 `songlist` 推荐谱面。
- `archd` / `arctz`：查询当前 World Mode 活动。
- `zm` / `arczm`：开始字符猜歌，可追加题数。
- `qh` / `arcqh`：开始曲绘猜歌或继续开格，可追加网格大小或 `max`。
- `开*`、`题号+曲名`、`猜+曲名`、曲绘模式下的可信曲名：由 `chat.receive.before_process` 在 Planner、普通聊天和 A-Memorix 前消费。
- `jx` / `arcjx`：公布答案并结束会话。
- `xz` / `arcxz`：固定作者 `605738729` 查询 lowiro 官网版本，并通过本地 arcaeaRecord 项目下载最新 APK；重复发送可查看实时进度，下载完成后群聊命令通过 localhost artifact API 发布到当前群文件，私聊明确提示只能在群聊发布。

## 门禁与多实例

`plugin.enabled=true` 且 `cutover.write_enabled=true` 是仓库默认运行形态，ARC 固定命令和猜歌会话已经由本插件接管并实际执行。`cutover.write_enabled=false` 只用于紧急回退：匹配的固定命令和明确的活动会话答案会静默消费，不发送状态文本，也不改动会话。

ARC 固定命令和会话答案均调用 `qqbot.route.claim`，只有唯一实例执行。`routing.bot_account_ids` 应配置全部机器人 QQ，插件内会再次拒绝机器人发送者。除机器人自循环发送者和 claim loser 外，命中的固定命令始终返回可见结果：关闭状态、私聊误用群猜入口、网络失败、资产缺失、存储或渲染异常都不会再静默落入普通聊天；错误文本不回显原始异常、URL 参数或凭据。

`background.enabled=true` 默认启动 60 秒有限周期后台循环。别名、定数和官网版本按配置间隔同步；活动默认每小时查询一次，过期猜歌被原子删除并在原群揭晓；有活动时每日向目标群提醒一次。每个阶段独立隔离异常，一个网络源失败不会阻断其余阶段；插件卸载和配置重载会取消循环。`background.reminder_group_ids` 留空时通过 MaiBot NapCat adapter 的 `get_group_list(no_cache=False)` 获取当前 bot 群列表，显式配置后只提醒指定群。后台错误日志只记录阶段和异常类型，不记录异常正文。

猜歌会话写入共享 `qqbot_features.sqlite3` 的 `arc_guess_sessions` 表。每次读改写使用 SQLite `BEGIN IMMEDIATE` 事务，两个 MaiBot 实例不会以各自内存状态覆盖对方。

## 数据与资产

- `storage.runtime_root_override` 留空时调用 `qqbot.storage.runtime_root`。
- `storage.database_path` 留空时使用 `<runtime_root>/db/qqbot_features.sqlite3`。
- `storage.cache_root` 留空时使用当前插件 `self.ctx.paths.runtime_dir/qqbot_arc`，仅保存可重建面板；显式配置时使用指定目录。
- `assets.assets_root` 留空时读取 `<runtime_root>/data/arc`，目录内应包含 `官谱/songlist` 和各歌曲曲绘。
- `assets.aliases_path` 是可选只读静态别名文件。插件不会写 JSON/TXT 运行事实源。
- `apk.arcaea_record_root` 指向含 `pom.xml` 的 arcaeaRecord Maven 项目；APK 下载会先编译该项目，再运行 `arc.record.Main 6`。
- `apk.artifact_root` 必须位于允许发布的 Git 仓库内；云栖使用 `/mnt/d/project/maibot/data/local_artifacts/arc`，让 artifact API 能校验当前 branch/commit 后再发布。
- `arc.background_state`、每日提醒 claim、同步别名和定数写入共享 `qqbot_features.sqlite3`；两个 MaiBot 实例共享状态并通过 SQLite 唯一约束去重提醒。
- 后台别名缓存会参与猜歌答案匹配，后台定数缓存会覆盖本地 songlist 的粗粒度 `rating/ratingPlus` 计算。
- APK 先下载到 artifact 根内的唯一临时目录，确认是非空 `.apk` 后以 `os.replace` 原子发布；成功、失败、超时和插件卸载都会清理临时目录。编译、下载和官网版本查询均有独立有限超时。
- `artifact_publish.enabled=true` 默认在已完成状态下接管群文件发布；endpoint 只允许 `127.0.0.1`、`::1` 或 `localhost` 的固定发布路径。客户端提交新鲜时间戳、当前 Git branch/commit 和 APK SHA-256，服务端仍独立复核归档和哈希。
- 群内答题者名称通过 `qqbot.identity.display_name` 解析。

猜歌面板和推荐曲绘在发送前异步读取并编码为 OneBot `base64://` 图片，避免 Windows NapCat 直接读取 WSL 本地路径。

运行时不导入 AstrBot、NoneBot、旧 QQBot 插件目录或 `.agent-reference`；APK manager、下载器、算法、活动解析和面板渲染均位于本插件。APK 与下载临时目录由 `.gitignore` 排除，不得纳入 Git。

## APK 运行依赖

`xz` / `arcxz` 使用 arcaeaRecord 的既有下载协议，需要本机安装与项目 `maven.compiler.release` 匹配的 JDK 和 Maven，并让 `apk.arcaea_record_root` 指向可编译、含 `arc.record.Main` 的项目。可通过 `apk.java_home` 和 `apk.maven_command` 显式覆盖可执行文件；下载器会把现有 HTTP(S)_PROXY 转换为 Java/Maven 标准代理系统属性，不保存独立代理或凭据。插件本身不新增 Python 包依赖。首次命令启动异步下载，重复命令查询进度；下载完成后，群聊命令发布到当前群文件，内容未变化时由 artifact 服务跳过删除、上传和重复通知。

## 验证

```bash
python -m unittest discover -s plugins/qqbot_arc/tests -v
ruff check plugins/qqbot_arc
python -m py_compile plugins/qqbot_arc/*.py plugins/qqbot_arc/tests/*.py
```
