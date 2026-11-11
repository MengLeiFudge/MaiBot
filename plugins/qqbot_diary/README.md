# better-diary 与蓝铃工作区

蓝铃的每日成品由已安装的 `org.orge-8.better-diary` 生成和发布。`qqbot_diary` 只把成品正文导出为 Markdown，不调用模型、不发送聊天、不发布空间，也不改变长期记忆。

## 本机配置

1. 使用本 fork 跟踪的 better-diary 1.9.0 本地维护版本。上游来源、两处 SDK 模型参数修复及更新要求见 [UPSTREAM.md](../org_orge-8_better-diary/UPSTREAM.md)。
2. 将 [better-diary.example.toml](../../deployment/config-examples/better-diary.example.toml) 的部署值合并进上游生成的完整配置。`target_chats` 与 `admin_ids` 都填写身份插件所配置的主人；只取材该私聊，不开启全库取材。
3. 将本目录 `config.example.toml` 复制为 `config.toml`，设置 `plugin.enabled = true`。默认每 60 秒把成品保存到 `D:/project/lanling/日记/better-diary/YYYY-MM-DD.md`。

better-diary 每天 23:30 自动生成；消息不足时按其原有规则跳过。保留三天内缺档补写，历史日期只归档。管理员可使用 `/日记`、`/日记查看 日期`、`/日记 日期 重写`；手动重写当天已发布的日记可能再次发布空间，按插件命令提示操作。

QQ 空间开关在 better-diary 的 `[qzone].enabled`。启用时，当天成品按其去重规则自动公开发布；空间登录态通过现有 NapCat 公开 `get_cookies` API 取得。本机使用独立的公开日记写作说明，省略亲密、性、健康隐私、私聊逐字引文和身份信息。生成模型仍可能理解有误，空间发布成功与否以 better-diary 的状态为准，工作区有文件只证明已经存档。本集成不试发空间。

## 文件职责

- better-diary 标准数据目录里的 `diaries.json` 是成品真源，`continuity.json` 等仍归原插件维护。
- `日记/better-diary/` 是正文副本，蓝铃可用已有 `lanling_read_file` 回看；旧手写日记留在原处。
- 临时感想、学习资料和额外补充继续写工作区笔记，不另设一套每日成品生成流程。
- `data/plugins/mlj.qqbot-diary/exports.json` 只保存已导出内容的 SHA-256，不包含正文。

上游只读 `get_day_digest` API 不提供成品正文，所以导出器只读其 README 已说明的 `diaries.json` 存档格式（日期键 → `content`）。不导入上游私有实现，不读取 cookies、选材事件或 META 到工作区。上游升级改变这个格式时，先核对格式，再调整导出器；解析失败保留现有文件并报错。

导出器只更新上次由自己写入且内容未改动的文件。遇到手工改动或未知同名文件时保留该文件并记录日期；将冲突文件移到笔记目录后，下一次同步会补出当前成品。上游删除一篇日记不会删除工作区历史。写入采用临时文件替换，卸载会等待当前磁盘操作结束。实际配置、日记、摘要记录和空间登录态均不进 Git。
