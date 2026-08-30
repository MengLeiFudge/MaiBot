# QQBot Shapez

MaiBot SDK 2.x 原生固定命令插件，用于渲染 Shapez 短代码、层级结构图和构造路径图。

插件默认 `plugin.enabled = true` 且 `cutover.write_enabled = true`。AstrBot 已停止，MaiBot 直接接管 Shapez 固定命令；命令在 Planner、普通聊天和 A-Memorix 前执行，不再保留命中后静默消费的关闭门禁。

支持入口：

- `i` / `view`：渲染 Shapez 短代码。
- `chart` / `chart1` / `chart2`：渲染层级结构图。
- `path` / `path1` / `path2`：求解并渲染构造路径。
- `p` / `puzzle` / `puzzle1` / `puzzle2`：保留旧版在线谜题入口；当前未配置登录 token 时返回确定性提示。

插件先调用公共 `qqbot.route.claim`，仅 owner 实例执行。结果通过 NapCat Adapter 的群聊或私聊公开 API 发送；本地图片读取后使用 OneBot `base64://` 数据传输，避免 NapCat 无法访问 MaiBot 所在文件系统。渲染产物是可重建缓存，默认位于 SDK 分配的插件运行时临时目录 `self.ctx.paths.runtime_dir`（MaiBot 1.1.3 下为 `temp/plugins/<plugin-id>/shapez/`）；可用 `storage.cache_root_override` 指向其他实例专用临时目录。

插件 vendored 的 `service.py`、`path_solver.py` 和 `path_renderer.py` 为纯 Python/Pillow 实现，不依赖 AstrBot、NoneBot 或旧 QQBot 插件目录。

验证：

```bash
cd /mnt/d/project/maibot
.venv/bin/python -m unittest discover -s plugins/qqbot_shapez/tests -v
.venv/bin/ruff check plugins/qqbot_shapez
.venv/bin/python -m py_compile plugins/qqbot_shapez/*.py plugins/qqbot_shapez/tests/*.py
```
