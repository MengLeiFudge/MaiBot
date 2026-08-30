# QQBot Factorio Download

MaiBot 固定命令插件，用于获取 Factorio Space Age 当前稳定版 Windows 安装包链接。插件默认启用，并在 Planner/聊天链之前消费命令。

## 命令

以下完整消息及其大小写、空格变体均可触发，群聊和私聊都可使用；群聊允许在命令前 @ 当前机器人：

- `Factorio下载链接`
- `异星下载链接`
- `太空时代下载链接`
- `Space Age 下载链接`
- `SpaceAge 安装包地址`

双机器人收到同一群命令时，插件先调用 `qqbot.route.claim`，只有获得 claim 的机器人发送回复。

## 配置

首次加载后在插件 WebUI 配置中填写 Factorio.com 凭据。配置保存在插件运行目录的、被 Git 忽略的 `config.toml`，不要把真实凭据写入 `config.example.toml`：

```toml
[plugin]
enabled = true
config_version = "0.1.0"

[factorio]
username = ""
token = ""
timeout_seconds = 30
```

`token` 在 WebUI 中使用 password 输入框。插件不读取 `FACTORIO_USERNAME`、`FACTORIO_TOKEN` 或工作区 `.env`。默认启用但缺少任一凭据时，命令会明确提示在插件配置中填写 `username` 和 `token`。

## 请求流程

插件先请求 `https://factorio.com/api/latest-releases` 并读取 `stable.expansion`，再请求对应版本的 `expansion/win64` 下载端点，禁止自动跟随第一次重定向，并把官网返回的重定向目标作为最终下载链接发送。

错误会区分版本接口异常、凭据无效、账号无 Space Age 权限、当前版本没有 Windows 包、网络失败和超时。日志仅记录群聊/私聊、错误分类和耗时，不记录 username、token 或带认证 query 的 URL。
