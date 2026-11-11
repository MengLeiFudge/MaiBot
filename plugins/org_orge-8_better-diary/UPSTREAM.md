# better-diary 本地维护来源

蓝铃使用随本 fork 跟踪的 better-diary 本地版本。上游为 <https://github.com/orge-8/better-diary>，基线版本 1.9.0，提交 `22fcb03bc6d1606a1254e1c7d9a1a86f72dc0c7d`，许可为本目录 [LICENSE](LICENSE) 中的 MIT License。上游 README 和 CHANGELOG 保留。

本地改动仅涉及 `plugin.py` 的 `_run_max_tokens_probe` 与 `_call_llm`：将 `[llm].task_name` 的值通过公开 `llm.generate` 的 `model` 参数传递。当前 MaiBot 1.2.3 的此能力读取 `model/model_name`，不读取 `task_name`；传空 model 会落到首个可用任务，在本部署中是 embedding，无法生成日记。

更新此目录时，先比较上游相对上述提交的变化，保留这个模型选择修复，或确认上游已经采用当前公开接口。不得直接用插件中心更新覆盖本地维护版本。完成 Python 编译、适用类型检查及配置静态核对后，通过既有插件重载或项目重启生效；不修改 MaiBot Core，也不以重复模型调用代替接口核对。本机关闭 `probe_max_tokens`，当前公开 SDK 与 Host 都已接受该参数。

该目录随蓝铃 fork 跟踪，不使用嵌套 Git 仓库。真实 `config.toml`、日记、cookies、缓存和上游测试目录均不纳入此部署提交；已安装的上游测试文件未改动，也未运行。部署取材、空间设置和工作区副本见 [qqbot_diary](../qqbot_diary/README.md)。
