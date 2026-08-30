# 夜凛本机配置参考

MaiBot 运行时配置位于项目根 `config/`，整个目录包含 provider key 等本机信息，因此不进入 Git。首次部署先按当前稳定版生成完整 `config/bot_config.toml` 和 `config/model_config.toml`，再应用本目录中的固定部署值：

- `bot-identity.example.toml`：夜凛 QQ、显示名和 WebUI 端口。
- `provider.example.toml`：只展示 provider 字段形状；`api_key` 必须在本机实际配置中填写。

这些文件是覆盖参考，不是可直接替代上游完整配置的最小配置。实际配置还必须保留当前 MaiBot 版本生成的全部必需 section。

启动前还要确认：

- `plugins/napcat_adapter/config.toml` 使用 `127.0.0.1:6201` 和 `connection_id = "yelin"`。
- 18 个 `plugins/qqbot_*/config.toml` 的顶层 `plugin.enabled` 为 `false`。
- `src/plugins/built_in/plugin_management/config.toml` 为禁用状态。

`scripts/enforce_chat_only.py` 会在 Core 启动前复核并纠正后两项；配置无法解析时会阻止启动。
