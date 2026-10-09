# 夜凛 Chat-focused Deployment

本分支是 MaiBot 1.2.3 的夜凛部署候选。MaiBot 身份固定为夜凛（QQ `2629227874`），WebUI 固定监听 `127.0.0.1:8003`。项目保留原生聊天、上下文、长期记忆、表情和图片理解，并通过本地插件提供拍一拍、只读知识、受信身份和定向看图；其他业务插件保持禁用。

## 运行态归属

`maibot-yelin/` 是独立运行边界。Core 只使用本目录中的 `config/`、`data/`、`logs/`、`plugins/`、`.venv/` 和 `.runtime/`，不读取 QQBot 根目录，也不复用其他角色的数据或日志。

NapCat 不由本项目启动。外层编排负责启动 NapCat；`napcat_adapter` 连接 `127.0.0.1:6201`，连接标识为 `yelin`。`qqbot_poke` 处理夜凛拍一拍，`qqbot_knowledge` 通过 `127.0.0.1:8081` 调用云栖持有的 DSP 向量知识库；夜凛不维护第二份 DSP 索引。

实际 `config/*.toml`、插件 `config.toml`、密钥、数据、日志、虚拟环境和 PID 文件均是本机运行态并由 `.gitignore` 排除。`deployment/config-examples/` 只记录夜凛身份、端口和 provider 占位结构；`napcat_adapter`、各 `qqbot_*` 的源码及 `config.example.toml` 可以随 fork 跟踪。所有示例不得包含令牌或密钥。

## 插件允许策略

启动允许集合为 `plugins/napcat_adapter`、`plugins/qqbot_poke`、`plugins/qqbot_knowledge`、`plugins/qqbot_identity` 和 `plugins/qqbot_visual`。`scripts/enforce_chat_only.py` 在 Core 启动前扫描 `plugins/` 的一级目录，将集合外插件实际配置中的顶层 `[plugin].enabled` 强制改为 `false`；缺少配置时创建最小禁用配置。同时强制禁用内置 `src/plugins/built_in/plugin_management`，避免聊天命令重新加载业务插件。

策略会先解析现有 TOML，再保留其余配置内容做定点改写，写入后再次解析确认，并使用同目录临时文件原子替换。无法解析、重复定义或无法确认禁用时退出非零，Core 不会启动。仅在值不符合策略时写盘并打印纠正清单，重复执行不会再次修改文件。

Core 原生加载器仍会导入禁用插件的模块，随后在激活阶段根据 `plugin.enabled=false` 返回 `INACTIVE`，不执行其生命周期和组件注册。导入异常记录为 `FAILED`，Runner 继续报告 ready；禁用不等于绝不导入。框架源码保持上游版本，策略不依靠私有加载器补丁。

## 启动

在 Windows PowerShell 中执行：

```powershell
./scripts/start.ps1
```

默认先执行 `uv sync --frozen`，再通过项目内 uv 环境运行策略和 `bot.py`。脚本等待 WebUI 8003 就绪后返回；若已有本项目进程则复用。需要重启本项目进程时使用：

```powershell
./scripts/start.ps1 -ForceRestart
```

已有完整 `.venv` 且明确不需要同步依赖时可加 `-SkipInstall`。缺少 uv、环境、策略执行失败或 WebUI 超时都会明确失败；启动输出分别写入 `logs/yelin-launcher.out.log` 和 `logs/yelin-launcher.err.log`。

## 更新稳定版

更新不会在启动时自动发生。保持已跟踪工作树干净并位于 `deployment` 分支，然后执行：

```powershell
./scripts/update.ps1
```

脚本从 GitHub `latest Release` API 获取官方 `Mai-with-u/MaiBot` 的最新非 draft、非 prerelease tag，显式从 `upstream` 获取该 tag，合入 `deployment`，最后执行 `uv sync --frozen`。脚本不会更新 NapCat、不会切换分支，也不会 push；合入后的审查和推送由维护者另行处理。
