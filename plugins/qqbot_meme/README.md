# QQBot Meme Manager

`mlj.qqbot-meme` 将旧 AstrBot 表情管理固定命令迁移为 MaiBot 原生插件。插件默认真实启用，命令在聊天链前消费，并通过 `qqbot.route.claim` 保证双 bot 只执行一次。

## 命令

普通用户可使用 `表情管理` / `表情管理 查看图库`、`表情管理 图库统计` 和 `表情管理 同步状态`。管理员可使用：

- `表情管理 开启管理后台` / `关闭管理后台`
- `表情管理 添加表情 <类别>`，随后 30 秒内发送一张或多张图片
- `表情管理 恢复默认表情包 [类别]`
- `表情管理 清空指定类型 <类别>` / `清空全部` / `删除类型本身 <类别>`
- `表情管理 同步到云端` / `从云端同步`
- `表情管理 覆盖到云端` / `从云端覆盖`

所有旧别名仍由解析器接受。清空和删除操作要求同一会话、同一发送者在 30 秒内回复“确认”；回复“取消”或超时不会修改图库。

## 数据与权限

默认从 `qqbot.storage.runtime_root` 获取共享根，唯一长期事实源固定为 `<runtime_root>/meme_manager/meme_index.json` 和 `<runtime_root>/meme_manager/memes/`。旧 `memes_data.json` 只在索引尚未建立时只读导入，插件不会创建或更新它。`storage.runtime_root` 仅用于明确覆盖。

索引读改写同时持有同路径共享的进程内线程锁和索引旁跨进程文件锁，使用唯一临时文件、`fsync` 和原子替换发布，支持后台线程与多个 MaiBot 实例并发操作。管理员包括 `plugin.admin_user_ids`、MaiBot `plugin.permission` 中的对应平台用户、本地操作员和当前 bot 自身。

## 自动表情闭环

`auto_send.enabled=true` 时，插件通过 `maisaka.replyer.before_model_request` 将当前索引中可自动发送的安全粗类别注入本次主聊天请求。模型只需在适合轻松日常、玩梗、吐槽、撒娇或短情绪配图时，在有意义的纯文本末尾输出一个 `&&类别&&`；单图选择完全在本地完成，不调用额外 LLM，也不新增 provider、模型顺序或 fallback。

发送前 Hook 会移除完整标签和可识别的半截/畸形标签。仅 `auto_send.safe_categories` 中的类别可进入 selector，类别或图片的 `auto_send_enabled=false` 始终优先禁止自动发送。selector 直接读取唯一 `meme_index.json`，综合类别、`keywords`、`use_cases` / `applicable_scenes`、`avoid_when` / `disabled_scenes`、图片 `weight` 和每会话近期去重选择 `memes/` 现有图片；缺失的新字段按旧索引默认值兼容。

主文本先以清洗后的 QQ 纯文本正常发送。只有主文本发送成功，插件才通过 NapCat base64 图片段单独发送选中表情；私聊目标取自出站路由元数据，不使用当前 bot 的出站身份。图片读取或 NapCat 发送失败只记录类别、文件名和异常类型，不记录上游异常正文，也不撤销或吞掉主文本。图片发送 Hook 使用 30 秒有限超时，覆盖最大 20 MiB 图片编码和发送所需的默认 5 秒以上执行边界。`auto_send.max_text_chars` 限制自动配图的回复长度，`auto_send.recent_history_size=0` 可关闭近期去重。非持久化的机器人或插件出站只清理内部标签，不触发自动表情。

## 管理后台

后台只在私聊执行“开启管理后台”后启动，随插件卸载停止。默认监听 `127.0.0.1:5000`，每次生命周期生成新的临时密钥，以 `HttpOnly`、`SameSite=Strict` Cookie 鉴权。页面保留旧图库的桌面和移动端资产，支持分类、上传、批量移动/复制/删除和单图语义元数据编辑。端口占用或启动失败会在原会话返回明确错误。将 `bind_host` 改为公网地址前必须另行配置防火墙或反向代理访问控制。

## 云同步

`remote.provider` 留空时，所有云端命令返回明确的“图床服务未配置”错误。本地命令不依赖云端凭据。

- `stardots` 需要 key、secret、space，并要求环境已有 `requests`。
- `cloudflare_r2` 需要 account ID、access key、secret key、bucket，并要求环境已有 `boto3`、`botocore`。

远端下载最多 20 MiB。文件先进入图库目录中的唯一临时文件，完成后验证扩展名和 PNG/JPEG/GIF/WebP 文件签名，只有验证成功才会原子发布并写入索引；超大、伪装或中断的文件会清理临时产物。

示例配置不包含真实凭据。插件不导入 AstrBot、NoneBot 或旧插件目录。
