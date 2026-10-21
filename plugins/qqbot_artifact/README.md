# QQBot 本地产物发布

MaiBot SDK 2.7 原生扩展插件，为本机白名单构建流程提供兼容接口：

- `GET /healthz`
- `GET /admin/api/artifacts/group-files?group_id=<群号>&folder_id=<文件夹ID>`：仅供本机核对目标目录的文件 ID、名称及大小，查询上限 30 秒；用于上传超时后核对实际状态，不能把名称/大小相同视为内容哈希确认。
- `POST /admin/api/artifacts/publish-local`
- `POST /admin/api/artifacts/group-folders`：本机 JSON `{"group_id": 319567534, "folder_name": "目标目录"}` 显式创建或复用同名目录，返回该目录 ID 和名称；重名不能唯一匹配则失败。仅在用户指定目录需创建时调用，普通上传不会创建目录。超时或失败先 GET 查询结果，不盲目重试。
- `GET /admin/api/artifacts/group-folders?group_id=<群号>`：只返回群文件夹的 ID 和名称，不返回文件或成员资料；查询上限 30 秒。

接口只监听回环地址。`listener.account_self_id` 与 `listener.owner_self_id` 相同时才启动，从而让多 MaiBot 实例中只有指定账号占用 8080。端口被占用或监听失败会让插件加载失败，不会把失败误报为 ready。

发布请求必须包含新鲜 ISO 时间戳、当前 Git branch/commit、至少一个仓库内 `.zip` 或 `.apk`、目标群和 commit detail 或文件 message。路径必须位于 `publishing.allowed_roots` 下的同一个 Git 仓库；Windows `D:\\...` 与 WSL `/mnt/d/...` 输入均可规范化。服务端独立计算文件 SHA-256；普通 zip 另计算忽略时间戳的展开内容 SHA-256，APK 使用文件 SHA-256 作为去重键，避免对大型安装包重复展开读取。客户端 `sha256` / `content_sha256` 只作一致性校验。

所有文件都通过校验后，服务在共享 `local_artifacts/.publish.lock` 上串行发布。内容 hash 与服务端状态一致时不删除、不上传、不发重复通知；新内容先完成最终文件哈希复核，再只删除同名且 uploader 等于当前 bot 的旧群文件。`/mnt/<drive>` 下的产物转换为 Windows 原生路径交给 NapCat，其余本地路径使用 `base64://`，避免 Windows NapCat 读取 WSL 私有路径。最后每群只发送一条发布说明。上传成功但说明失败时保存 `notice_sent=false`，相同请求重试只补说明而不重复上传。状态继续兼容旧 `local_artifacts/<group>/<name-hash>.json`。

发布说明引用上传文件，并依次显示「分支 短提交号」、提交标题和逐条编号的正文，行间不加空行。正文取文件 `message`，为空时取 `commit_detail`；忽略空行并统一列表编号，保留原有顺序和内容。

文件条目可选 `folder_id` 字符串，省略、空白或 `/` 表示最外层，其余值为该群已有文件夹 ID。ID 可从 `get_group_root_files` 响应的 `folders[].folder_id` 取得；不同群的目录 ID 不通用，向不同目录发布时分别填写文件条目。插件在任何删除或上传前确认目标文件夹存在；错误时失败，不自动创建或回退根目录。上传和同名旧文件清理只作用于选定目录，切换目录不会搬迁或删除原目录文件。

服务端状态按群、目录和文件名隔离；根目录继续兼容旧路径，子目录增加 `folders/<folder-id-hash>/`。通知待发状态也携带目录，补发通知不会写入其他目录缓存。响应的 `uploaded`、`deleted`、`skipped` 均包含 `folder_id`（根目录为空字符串）；子目录上传未返回文件消息 ID 时不从同名历史消息推断引用。

zip 和 APK 均受文件大小、条目数和解压总量限制，不解压到磁盘，不接受加密归档。`publishing.api_timeout_seconds` 为 NapCat 群文件动作提供显式有限的插件 RPC 边界，默认 900 秒，用于大文件上传；HTTP 客户端仍应设置自己的有限超时。日志只记录动作与错误类型，不记录请求体、构建内容或凭据。插件不响应聊天命令，也不读取 QQ 登录态、私聊、token 或运行日志。

示例配置位于 `config.example.toml`。实际 `config.toml` 被 Git 忽略。部署按 QQBot 根 AGENTS：云栖的 AstrBot 插件负责发布，夜凛的本插件保留对应源码，实际与示例配置均保持 `plugin.enabled = false`。

## 一代模组同步

`POST /admin/api/artifacts/shapez1-mods` 提供与 AstrBot 相同的本机合同：目标固定为群 `1163635014` 的现有文件夹 `s1-MOD-官方模组网站`，按名称唯一解析。当前包请求包含 `id`、`file_id`、`path`、`name`（模组名_版本.zip）、`sha256`；废弃请求为 `{"id": 模组ID, "retired": true}`。成功响应返回目标标识及 `uploaded`、`skipped`、`deleted` 数量。

`shapez_mods.ShapezModPublisher` 通过 SDK 的 NapCat 文件 API 调用协议端，仅接收 ShapezToolkit 模组 downloads 目录内不超过 64 MiB 的 ZIP，独立校验 SHA-256。运行态 `shapez1_mods` 按账号、群、目录和模组 ID 记录远端文件；列表最多读取 10000 项，达到上限则停止。NapCat 列表 ID 是临时值，长期记录使用名称、大小、上传时间和上传账号组成的唯一特征；上传前核对本地 SHA-256 并保存目录基线，取得成功上传回执后，新版须唯一匹配新增条目的名称、大小和上传账号，才使用当轮查询的 ID 删除已跟踪的旧版，再查目录确认删除结果。首次不接管人工文件。上传结果不确定时保留 pending：有成功回执时重跑继续确认，没有回执时须核对现场，不自动重传，删除失败保留待删记录，不额外发送公告。更新与仅发布入口由 ShapezToolkit 的 `mods update` / `mods publish` 提供。
