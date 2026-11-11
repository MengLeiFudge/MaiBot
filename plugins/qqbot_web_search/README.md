# QQBot 联网搜索

在普通模型请求改用 CPA 后，通过 `web_search` 函数工具保留 DeepSeek 官网联网搜索。工具只接收本次查询，返回搜索摘要和引用链接；普通聊天不触发官网请求。

## 配置

1. 将 `config.example.toml` 复制为本机 `config.toml`。
2. 在 `[search].api_key` 填写原有 DeepSeek 官网密钥，保留原模型名称；这个字段不使用 CPA 客户端密钥。
3. 设置 `[plugin].enabled = true`。夜凛的启动允许列表包含此只读插件；蓝铃按本机配置启用。
4. `config/model_config.toml` 中普通文本及视觉模型继续使用 `openai_responses`，provider 地址为 `http://127.0.0.1:8317/v1`、密钥为 CPA 客户端密钥。删除普通模型 `extra_params.tools` 中的原生 `web_search` 项，由本插件提供同名函数工具。

插件通过公开 Planner Hook 提示模型使用 `tool_search` 发现 `web_search`。模型实际调用后，插件向固定官网 Responses 地址发送查询；工具不发送聊天历史或人设，不持久化查询和结果，不执行网页内容中的指令。

## 结果与失败

只有官网响应完成、包含已完成的 `web_search_call` 且有回答正文时，工具才返回成功。引用链接保存在 `sources`，过长摘要通过 `truncated` 标明截断。HTTP 错误、超时、无搜索记录和空正文分别返回失败，不能用普通模型生成内容冒充检索结果。

搜索沿用 `deepseek-flash`、`reasoning.effort = "max"` 和 8192 输出 token 上限。每次工具执行只有一次官网请求，总超时由 `search.timeout_seconds` 控制，范围 5–150 秒；失败不自动重试。官网搜索费用在官网账单查看，该直接 HTTP 请求不进入 MaiBot 的 `llm_usage`。

更新 CPA 时，需要核对其兼容渠道是否真正保留 Responses 原生 `web_search` 及搜索结果事件；仅有 `/responses` 地址或同名模型不足以证明支持。确实支持后才能停用本插件、恢复普通模型原生搜索，避免重复查询。插件加载和静态检查不证明真实网络查询成功。
