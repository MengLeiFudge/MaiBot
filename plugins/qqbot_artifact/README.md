# QQBot 本地产物发布

MaiBot SDK 2.7 原生扩展插件，为本机白名单构建流程提供兼容接口：

- `GET /healthz`
- `POST /admin/api/artifacts/publish-local`

接口只监听回环地址。`listener.account_self_id` 与 `listener.owner_self_id` 相同时才启动，从而让多 MaiBot 实例中只有指定账号占用 8080。端口被占用或监听失败会让插件加载失败，不会把失败误报为 ready。

发布请求必须包含新鲜 ISO 时间戳、当前 Git branch/commit、至少一个仓库内 `.zip` 或 `.apk`、目标群和 commit detail 或文件 message。路径必须位于 `publishing.allowed_roots` 下的同一个 Git 仓库；Windows `D:\\...` 与 WSL `/mnt/d/...` 输入均可规范化。服务端独立计算文件 SHA-256；普通 zip 另计算忽略时间戳的展开内容 SHA-256，APK 使用文件 SHA-256 作为去重键，避免对大型安装包重复展开读取。客户端 `sha256` / `content_sha256` 只作一致性校验。

所有文件都通过校验后，服务在共享 `local_artifacts/.publish.lock` 上串行发布。内容 hash 与服务端状态一致时不删除、不上传、不发重复通知；新内容先完成最终文件哈希复核，再只删除同名且 uploader 等于当前 bot 的旧群文件。`/mnt/<drive>` 下的产物转换为 Windows 原生路径交给 NapCat，其余本地路径使用 `base64://`，避免 Windows NapCat 读取 WSL 私有路径。最后每群只发送一条发布说明。上传成功但说明失败时保存 `notice_sent=false`，相同请求重试只补说明而不重复上传。状态继续兼容旧 `local_artifacts/<group>/<name-hash>.json`。

zip 和 APK 均受文件大小、条目数和解压总量限制，不解压到磁盘，不接受加密归档。`publishing.api_timeout_seconds` 为 NapCat 群文件动作提供显式有限的插件 RPC 边界，默认 900 秒，用于大文件上传；HTTP 客户端仍应设置自己的有限超时。日志只记录动作与错误类型，不记录请求体、构建内容或凭据。插件不响应聊天命令，也不读取 QQ 登录态、私聊、token 或运行日志。

示例配置位于 `config.example.toml`。实际 `config.toml` 被 Git 忽略；当前云栖实例应设置 `account_self_id = owner_self_id = "1443944862"`，其他实例只设置各自 `account_self_id` 并保留同一 owner。
