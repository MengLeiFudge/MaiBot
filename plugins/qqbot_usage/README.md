# QQBot Sub2API Usage

MaiBot SDK 2.x 原生固定命令插件，恢复 QQBot 的“用量”入口。

“用量”在群聊和私聊中都由 Command 组件确定性拦截，先调用公共 qqbot.route.claim，再从后台缓存读取报告；不会进入 Planner、普通聊天模型或 A-Memorix。群聊多机器人只由 claim owner 回复，私聊按当前 self_id 独立执行。

报告内容保持旧实现：

- 第一张图按账号展示 5h / 7d 额度和当前账号 7d 周期用户消费榜，底部展示全账号当日 / 本周 / 30d 消费榜。
- 第二张图展示 CodexRadar 公开智力效率数据；该公开数据首次刷新失败时仍发送第一张图。
- Sub2API 用户榜和账号额度由后台刷新，命令只读缓存，不临时阻塞调用上游。
- 5h 80% / 90% / 95% 主动提醒默认关闭；需要时显式配置 alerts.enabled 和 group_ids。

sub2api.admin_api_key 只写入被 .gitignore 排除的 config.toml。可重建图片缓存默认使用 SDK 分配的 self.ctx.paths.runtime_dir，不写插件源码或共享业务数据目录。本地图在发送前读取并编码为 OneBot base64://，兼容 WSL MaiBot 到 Windows NapCat 的边界。

sub2api_usage.py、sub2api_usage_image.py 和 codexradar_efficiency.py 从原 AstrBot 功能插件 vendored，业务聚合与渲染算法保持一致；plugin.py 只承载 MaiBot 生命周期、command claim、缓存调度和发送适配。

验证命令：

    cd /mnt/d/project/maibot
    .venv/bin/python -m unittest discover -s plugins/qqbot_usage/tests -v
    .venv/bin/ruff check plugins/qqbot_usage
    .venv/bin/python -m py_compile plugins/qqbot_usage/*.py plugins/qqbot_usage/tests/*.py
