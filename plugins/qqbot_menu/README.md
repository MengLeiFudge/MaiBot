# QQBot 菜单

MaiBot SDK 2.x 原生固定命令插件。`菜单`、`帮助`、`指令` 以及无空格分类入口（例如 `菜单JM漫画`、`菜单落樱之都`、`菜单表情管理`）会在 Planner、普通聊天和 A-Memorix 前执行。

插件通过 `qqbot.route.claim` 选择唯一执行实例，动态读取各业务插件的真实启用状态，并用 Pillow 生成图片菜单。图片写入 SDK `runtime_dir/menu` 可重建缓存，经 OneBot `base64://` 发送；渲染或图片发送失败时降级为完整文本菜单。

分类覆盖群务管理、棉花糖互动（含自动复读状态）、养鲲、落樱之都、Arcaea、JM 漫画、Factorio、异形工厂和表情管理。菜单只展示真实固定入口，不再出现“尚未迁移”“仅阻断”或关闭迁移门禁的旧状态文案。

验证：

    cd /mnt/d/project/maibot
    PYTHONPATH=plugins .venv/bin/python -m unittest discover -s plugins/qqbot_menu/tests -v
    .venv/bin/ruff check plugins/qqbot_menu
    .venv/bin/python -m py_compile plugins/qqbot_menu/*.py plugins/qqbot_menu/tests/*.py
