# QQBot 群务

MaiBot SDK 2.x 原生群务插件，负责 OneBot 好友申请、群邀请、入群通知、新成员欢迎和受控群文件清理。

协议事件通过修改后的 NapCat Adapter 作为 `is_notify=true` 的结构化消息进入 `chat.receive.before_process`。插件只读取 `additional_config.napcat_request_payload` / `napcat_notice_payload`，处理完成后总是中止主消息链，因此请求 flag、入群通知和管理事件不会进入 Planner、普通聊天或 A-Memorix。审批、邀请者通知和新成员欢迎按动作写入共享 SQLite 的 `maibot_group_protocol_audit` 表，记录事件类型、动作、`sub_type`、`success/failure/skipped` 结果和安全失败类别；request flag 仅作为 OneBot API 参数和该结构化审计表字段使用，不进入日志或聊天文本。

默认行为：

- 自动同意好友申请。
- 自动同意 `group/invite` 请求，并按 `self_id + group_id` 在共享 SQLite 中暂存邀请者。
- 当前机器人入群后私聊通知邀请者；无法解析邀请者时通知固定主人。
- 真人新成员入群时，各机器人按自己的 `self_id` 选择配置模板并独立随机生成以“群地位”开头、等价于减 1 的表达式。
- 配置中的机器人账号入群不触发欢迎。

“通知清理文件”命令默认 `cleanup.write_enabled=true`。AstrBot 已停止，MaiBot 是该固定命令的唯一执行链；仅固定主人可执行，命令先调用 `qqbot.route.claim`，只统计超过宽限期的外层群文件，按上传者汇总并按文件体积计算禁言时长；待处理记录写入 `db/qqbot_features.sqlite3` 的插件专属表。

依赖：

- `mlj.qqbot-common >= 0.1.2`
- `maibot-team.napcat-adapter >= 1.3.3`，并包含 OneBot request 入站补丁

验证：

    cd /mnt/d/project/maibot
    PYTHONPATH=plugins .venv/bin/python -m unittest discover -s plugins/qqbot_group/tests -v
    .venv/bin/ruff check plugins/qqbot_group
    .venv/bin/python -m py_compile plugins/qqbot_group/*.py plugins/qqbot_group/tests/*.py
