from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping

import asyncio
import base64
import re
import secrets
import time

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase
from qqbot_common.api_results import require_api_result

from .service import render_shape_chart, render_shape_code, render_shape_path


SHAPEZ_COMMAND_PATTERN = (
    r"^(?:@\S+\s*)?(?:i|view|chart|chart1|chart2|path|path1|path2|p|puzzle|puzzle1|puzzle2)"
    r"(?:\s+[\s\S]*)?$"
)
_PUZZLE_COMMANDS = frozenset({"p", "puzzle", "puzzle1", "puzzle2"})
_CHART_COMMANDS = frozenset({"chart", "chart1", "chart2"})
_PATH_COMMANDS = frozenset({"path", "path1", "path2"})
_IMAGE_SUMMARIES = ("异形图送到", "结构图完成", "渲染完成", "结果来了", "图已生成")
_PUZZLE_UNAVAILABLE = "没获取到 shapez 谜题：在线谜题下载需要 shapez 登录 token，当前未配置。"
_INVALID_ARGUMENT = "shapez 参数无效，请检查短代码。"
_RENDER_FAILED = "shapez 渲染失败，请稍后重试。"


class PluginSection(PluginConfigBase):
    """Shapez 插件基础配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否注册 Shapez 固定命令插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class CutoverSection(PluginConfigBase):
    """Shapez 单执行接管门禁。"""

    __ui_label__ = "迁移接管"
    __ui_order__ = 1

    write_enabled: bool = Field(default=True, description="AstrBot 已停用，默认由 MaiBot 接管 Shapez 命令")


class StorageSection(PluginConfigBase):
    """可重建渲染缓存配置。"""

    __ui_label__ = "缓存"
    __ui_order__ = 2

    cache_root_override: str = Field(default="", description="留空时使用插件运行时临时目录")


class ShapezConfig(PluginConfigBase):
    """Shapez 插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    cutover: CutoverSection = Field(default_factory=CutoverSection)
    storage: StorageSection = Field(default_factory=StorageSection)


class QQBotShapezPlugin(MaiBotPlugin):
    """确定性处理 Shapez 短代码渲染命令。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = ShapezConfig

    async def on_load(self) -> None:
        self.ctx.logger.info(
            "QQBot Shapez 插件已加载，业务接管=%s",
            self.config.cutover.write_enabled,
        )

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("QQBot Shapez 配置已更新: %s", version)

    @Command("qqbot_shapez", description="Shapez 短代码、结构图、路径图和谜题入口", pattern=SHAPEZ_COMMAND_PATTERN)
    async def handle_shapez_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """仲裁并处理一次 Shapez 固定命令。"""

        del stream_id, kwargs
        command_text = _text_segments(message) or _strip_leading_mention(text)
        facts = _message_facts(message, group_id, user_id)
        if not facts["self_id"]:
            response = "Shapez 命令暂时无法确认当前机器人身份。"
            await self._send(group_id, user_id, text=response)
            return True, response, True
        try:
            claimed = await self._claim(command_text, facts)
        except Exception:
            response = "Shapez 命令仲裁失败，请稍后重试。"
            await self._send(group_id, user_id, text=response)
            return True, response, True
        if not claimed:
            return True, "", True
        if not self.config.plugin.enabled:
            response = "Shapez 功能当前未启用。"
            await self._send(group_id, user_id, text=response)
            return True, response, True
        if not self.config.cutover.write_enabled:
            response = "Shapez 功能接管当前已关闭。"
            await self._send(group_id, user_id, text=response)
            return True, response, True

        command, argument = _parse_command(command_text)
        if command in _PUZZLE_COMMANDS:
            await self._send(group_id, user_id, text=_PUZZLE_UNAVAILABLE)
            return True, _PUZZLE_UNAVAILABLE, True
        if not argument:
            await self._send(group_id, user_id, text=_INVALID_ARGUMENT)
            return True, _INVALID_ARGUMENT, True

        try:
            image_path, response = await asyncio.to_thread(
                _render_command,
                command,
                argument,
                self._cache_root(),
            )
        except ValueError:
            await self._send(group_id, user_id, text=_INVALID_ARGUMENT)
            return True, _INVALID_ARGUMENT, True
        except Exception:
            self.ctx.logger.exception("Shapez 渲染失败")
            await self._send(group_id, user_id, text=_RENDER_FAILED)
            return True, _RENDER_FAILED, True

        await self._send(group_id, user_id, text=response, image_path=image_path)
        return True, response, True

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="shapez",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "Shapez 命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("Shapez 命令仲裁返回格式无效")
        return bool(payload.get("claimed"))

    def _cache_root(self) -> Path:
        override = self.config.storage.cache_root_override.strip()
        if override:
            return Path(override).expanduser().resolve()
        return Path(self.ctx.paths.runtime_dir).resolve()

    async def _send(
        self,
        group_id: str,
        user_id: str,
        *,
        text: str,
        image_path: Path | None = None,
    ) -> None:
        segments: list[dict[str, object]] = []
        if image_path is not None:
            image_data = await asyncio.to_thread(image_path.read_bytes)
            image_file = "base64://" + base64.b64encode(image_data).decode("ascii")
            segments.append(
                {
                    "type": "image",
                    "data": {
                        "file": image_file,
                        "summary": secrets.choice(_IMAGE_SUMMARIES),
                    },
                }
            )
        if text:
            segments.append({"type": "text", "data": {"text": text}})

        if group_id:
            api_name = "adapter.napcat.group.send_group_msg"
            params = {"group_id": group_id, "message": segments}
        else:
            api_name = "adapter.napcat.message.send_private_msg"
            params = {"user_id": user_id, "message": segments}
        result = await self.ctx.api.call(api_name, params=params)
        require_api_result(result, "发送 Shapez 结果")


def _render_command(command: str, argument: str, output_root: Path) -> tuple[Path, str]:
    if command in _PATH_COMMANDS:
        tree, output, path_text = render_shape_path(output_root, argument)
        return output, f"\n短代码：{tree.shortcode}\n{path_text}"
    if command in _CHART_COMMANDS:
        shape, output, shape_text = render_shape_chart(output_root, argument)
        return output, f"\n短代码：{shape.short_key}\n{shape_text}"
    shape, output = render_shape_code(output_root, argument)
    return output, f"\n短代码：{shape.short_key}"


def _parse_command(text: str) -> tuple[str, str]:
    parts = _strip_leading_mention(text).split(maxsplit=1)
    return parts[0].lower() if parts else "", parts[1].strip() if len(parts) == 2 else ""


def _message_facts(message: object, group_id: str, user_id: str) -> dict[str, Any]:
    message_dict = message if isinstance(message, Mapping) else {}
    message_info = message_dict.get("message_info")
    message_info_dict = message_info if isinstance(message_info, Mapping) else {}
    additional = message_info_dict.get("additional_config")
    additional_dict = additional if isinstance(additional, Mapping) else {}
    raw_message = message_dict.get("raw_message")
    at_target_ids: list[str] = []
    if isinstance(raw_message, list):
        for segment in raw_message:
            if not isinstance(segment, Mapping) or segment.get("type") != "at":
                continue
            data = segment.get("data")
            target_id = str(data.get("target_user_id") or "").strip() if isinstance(data, Mapping) else ""
            if target_id and target_id not in at_target_ids:
                at_target_ids.append(target_id)
    try:
        timestamp = float(message_dict.get("timestamp") or time.time())
    except (TypeError, ValueError):
        timestamp = time.time()
    return {
        "user_id": user_id,
        "group_id": group_id,
        "self_id": str(additional_dict.get("self_id") or "").strip(),
        "timestamp": timestamp,
        "at_target_ids": at_target_ids,
    }


def _text_segments(message: object) -> str:
    if not isinstance(message, Mapping):
        return ""
    raw_message = message.get("raw_message")
    if not isinstance(raw_message, list):
        return ""
    parts: list[str] = []
    for segment in raw_message:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        if isinstance(data, Mapping):
            parts.append(str(data.get("text") or data.get("content") or ""))
        else:
            parts.append(str(data or ""))
    return "".join(parts).strip()


def _strip_leading_mention(text: str) -> str:
    return re.sub(r"^@\S+\s*", "", text.strip(), count=1)


def create_plugin() -> QQBotShapezPlugin:
    """创建 Shapez 插件实例。"""

    return QQBotShapezPlugin()
