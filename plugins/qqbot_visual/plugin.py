from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, ClassVar, cast

import asyncio

from maibot_sdk import ON_BOT_CONFIG_RELOAD, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .image_context import ImageContext
from .video_context import VideoContext
from .video_reader import VideoReader

class PluginSection(PluginConfigBase):
    """入站图片门控的启用与配置版本。"""

    enabled: bool = Field(default=True, description="是否启用图片门控、表情收集放行与视频分镜")
    config_version: str = Field(default="0.1.0", description="配置版本")


class VisualConfig(PluginConfigBase):
    """本人身份和称呼复用 bot 配置，不复制到插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    video_enabled: bool = Field(default=True, description="下载当前视频链接并用分镜图理解内容")


def _mask_media(segments: list[Any], *, collect_emoji: bool = False) -> bool:
    """递归屏蔽非定向图片；收集开启时保留表情及其二进制。"""
    changed = False
    for value in segments:
        if not isinstance(value, dict):
            continue
        part = cast(dict[str, Any], value)
        kind = part.get("type")
        if kind == "emoji" and collect_emoji:
            continue
        if kind in {"image", "emoji"}:
            content = str(part.get("data") or "").strip()
            if not content or content in {"[图片，识别中.....]", "[image]"}:
                part["data"] = "[图片]" if kind == "image" else "[表情包]"
                changed = True
            if "binary_data_base64" in part:
                del part["binary_data_base64"]
                changed = True
        elif kind == "forward" and isinstance(part.get("data"), list):
            for node in part["data"]:
                if isinstance(node, dict) and isinstance(node.get("content"), list):
                    changed = _mask_media(node["content"], collect_emoji=collect_emoji) or changed
    return changed


class QQBotVisualPlugin(MaiBotPlugin):
    """在公开接收 Hook 中门控入站图片，不接管框架内部后台识图。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = VisualConfig
    config_reload_subscriptions: ClassVar[tuple[str, ...]] = (ON_BOT_CONFIG_RELOAD,)

    def __init__(self) -> None:
        """初始化身份和收集开关；原图缓存随插件生命周期维护。"""
        super().__init__()
        self._bot_id = ""
        self._names: tuple[str, ...] = ()
        self._collect_emoji = False
        self._video_reader: VideoReader | None = None
        self._video_context: VideoContext | None = None
        self._images: ImageContext | None = None

    async def _reload_bot_config(self) -> None:
        """通过公开 config.get 读取身份和收集开关，校验后一起替换。"""
        async with asyncio.timeout(3):
            platform, bot_id, nickname, aliases, collect_emoji = await asyncio.gather(
                self.ctx.config.get("bot.platform"),
                self.ctx.config.get("bot.qq_account"),
                self.ctx.config.get("bot.nickname"),
                self.ctx.config.get("bot.alias_names"),
                self.ctx.config.get("emoji.steal_emoji"),
            )
        if platform != "qq" or not isinstance(bot_id, str) or not bot_id.isascii() or not bot_id.isdecimal():
            raise ValueError("定向看图插件需要 bot.platform=qq 和真实 bot.qq_account")
        if not isinstance(nickname, str) or not isinstance(aliases, list) or not all(isinstance(name, str) for name in aliases):
            raise ValueError("bot.nickname 和 bot.alias_names 配置类型不合法")
        if not isinstance(collect_emoji, bool):
            raise ValueError("emoji.steal_emoji 必须为布尔值")
        self._bot_id = bot_id
        self._names = tuple(dict.fromkeys(name.strip() for name in [nickname, *aliases] if name.strip()))
        self._collect_emoji = collect_emoji

    async def on_load(self) -> None:
        """读取身份配置并创建媒体读取器，加载阶段不处理媒体。"""
        await self._reload_bot_config()
        self._video_reader = VideoReader(Path(self.ctx.paths.data_dir) / "videos")
        self._video_context = VideoContext(self.ctx, self._video_reader)
        self.ctx.logger.info("视频分镜已加载：enabled=%s tools_ready=%s", cast(VisualConfig, self.config).video_enabled, self._video_reader.tools_ready)
        self._images = ImageContext(self.ctx, self._is_directed)
        self.ctx.logger.info("QQBot 定向看图已加载，表情收集=%s；在 Planner 和 Replyer 请求前补充私聊附件及群聊定向引用原图", self._collect_emoji)

    async def on_unload(self) -> None:
        """等待视频任务取消，清空身份和原图缓存。"""
        if self._video_reader is not None:
            await self._video_reader.close()
            self._video_reader = None
        if self._video_context is not None:
            self._video_context.clear()
            self._video_context = None
        if self._images is not None:
            self._images.clear()
            self._images = None
        self._bot_id = ""
        self._names = ()
        self._collect_emoji = False

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """跟随 bot 配置更新本人身份和表情收集开关。"""
        del config_data
        if scope == ON_BOT_CONFIG_RELOAD:
            await self._reload_bot_config()
            self.ctx.logger.info("QQBot 定向看图配置已更新: version=%s", version)

    @HookHandler(
        "chat.receive.before_process", name="qqbot_visual_video",
        description="登记当前视频来源，由实际主模型请求读取DSH分镜",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=5000, error_policy=ErrorPolicy.SKIP,
    )
    async def read_video(self, message: object = None, **kwargs: Any) -> dict[str, object]:
        """只处理本次消息的视频，保持聊天触发和既有图像门控。"""
        media = self._video_context
        if not cast(VisualConfig, self.config).plugin.enabled or not cast(VisualConfig, self.config).video_enabled or media is None:
            return {"action": "continue"}
        if not isinstance(message, dict) or message.get("platform") != "qq" or message.get("is_notify") is True:
            return {"action": "continue"}
        info = message.get("message_info")
        sender = info.get("user_info") if isinstance(info, Mapping) else None
        if isinstance(sender, Mapping) and str(sender.get("user_id")) == self._bot_id:
            return {"action": "continue"}
        modified = media.remember(cast(dict[str, Any], message))
        if modified is None:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": modified}}

    async def _attach_video(self, session_id: str, items: object, item_schema_version: int, kwargs: dict[str, Any], *, visual: bool = True) -> dict[str, object]:
        """把分镜原图放进公开Context Items，不调用额外摘要模型。"""
        media = self._video_context
        if (not cast(VisualConfig, self.config).plugin.enabled or not cast(VisualConfig, self.config).video_enabled
                or media is None or not session_id or item_schema_version != 1 or not isinstance(items, list)):
            return {"action": "continue"}
        enriched = await media.enrich(session_id, items, visual=visual)
        if self._video_context is not media or enriched == items:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {
            **kwargs, "session_id": session_id, "items": enriched, "item_schema_version": item_schema_version,
        }}

    @HookHandler(
        "maisaka.planner.before_request", name="qqbot_visual_video_planner",
        description="为规划模型附上同会话视频的DSH分镜原图和时间清单",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=190000, error_policy=ErrorPolicy.SKIP,
    )
    async def prepare_video_planner(self, session_id: str = "", items: object = None, item_schema_version: int = 0, **kwargs: Any) -> dict[str, object]:
        """规划模型直接读取视频画面和时间信息。"""
        return await self._attach_video(session_id, items, item_schema_version, kwargs)

    @HookHandler(
        "maisaka.replyer.before_model_request", name="qqbot_visual_video_replyer",
        description="为实际回复模型附上同会话视频的DSH分镜原图和时间清单",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=190000, error_policy=ErrorPolicy.SKIP,
    )
    async def prepare_video_replyer(self, session_id: str = "", items: object = None, item_schema_version: int = 0, **kwargs: Any) -> dict[str, object]:
        """回复模型独立取得分镜，不依赖规划器转述。"""
        return await self._attach_video(session_id, items, item_schema_version, kwargs, visual=kwargs.get("selected_model_visual") is True)

    def _is_directed(self, message: Mapping[str, Any]) -> bool:
        """只读取当前顶层正文及结构化 @/回复，不用转发内容触发识图。"""
        info = message.get("message_info")
        if not isinstance(info, Mapping):
            raise ValueError("接收消息缺少 message_info")
        sender = info.get("user_info")
        if not isinstance(sender, Mapping) or not sender.get("user_id"):
            raise ValueError("接收消息缺少真实发送者")
        if sender["user_id"] == self._bot_id:
            return False
        if info.get("group_info") is None:
            return True
        additional = info.get("additional_config")
        if message.get("is_at") is True or isinstance(additional, Mapping) and additional.get("at_bot") is True:
            return True
        for part in message["raw_message"]:
            if not isinstance(part, Mapping):
                continue
            data = part.get("data")
            if part.get("type") == "text" and isinstance(data, str) and any(name in data for name in self._names):
                return True
            if isinstance(data, Mapping):
                if part.get("type") == "at" and data.get("target_user_id") == self._bot_id:
                    return True
                if part.get("type") == "reply" and data.get("target_message_sender_id") == self._bot_id:
                    return True
        return False

    async def _reuse_quoted_descriptions(self, message: dict[str, Any]) -> bool:
        """把同会话引用图片的已有描述补入回复组件，不发起新的识图。"""
        session_id = message.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return False
        changed = False
        for part in message["raw_message"]:
            if not isinstance(part, dict) or part.get("type") != "reply" or not isinstance(part.get("data"), dict):
                continue
            data = part["data"]
            target_id = data.get("target_message_id")
            if not isinstance(target_id, str) or not target_id:
                continue
            quoted = await self.ctx.message.get_by_id(target_id, chat_id=session_id, include_binary_data=False)
            if (
                not isinstance(quoted, Mapping)
                or quoted.get("session_id") != session_id
                or quoted.get("message_id") != target_id
                or quoted.get("platform") != message.get("platform")
            ):
                continue
            segments = quoted.get("raw_message")
            if not isinstance(segments, list):
                continue
            descriptions = [
                str(segment["data"]).strip()
                for segment in segments
                if isinstance(segment, Mapping)
                and segment.get("type") in {"image", "emoji"}
                and isinstance(segment.get("data"), str)
                and str(segment["data"]).strip()
                not in {"", "[图片]", "[image]", "[图片，识别中.....]", "[表情包]"}
            ]
            content = str(data.get("target_message_content") or quoted.get("processed_plain_text") or "")
            updated = False
            for description in descriptions:
                if description not in content:
                    content = f"{content}\n[引用图片已有描述] {description}".strip()
                    updated = True
            if updated:
                data["target_message_content"] = content
                changed = True
        return changed

    @HookHandler(
        "chat.receive.before_process",
        name="qqbot_visual_receive_gate",
        description="非定向图片使用占位；收集开启时放行表情，定向附件沿用原生处理",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def gate_visuals(self, message: object = None, **kwargs: Any) -> dict[str, object]:
        """只改返回的消息副本，保留原发送者、路由、时间和其他组件。"""
        if not cast(VisualConfig, self.config).plugin.enabled:
            return {"action": "continue"}
        if not isinstance(message, dict) or message.get("platform") != "qq" or message.get("is_notify") is True:
            return {"action": "continue"}
        current = cast(dict[str, Any], message)
        if not isinstance(current.get("raw_message"), list):
            raise ValueError("接收消息的 raw_message 必须为列表")
        modified = deepcopy(current)
        if self._is_directed(current):
            try:
                async with asyncio.timeout(3):
                    changed = await self._reuse_quoted_descriptions(modified)
            except Exception as exc:
                self.ctx.logger.warning("引用图片描述查询失败，保持原引用: error_type=%s", type(exc).__name__)
                return {"action": "continue"}
        else:
            changed = _mask_media(modified["raw_message"], collect_emoji=self._collect_emoji)
        if not changed:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": modified}}

    async def _attach_images(
        self, session_id: str, items: object, item_schema_version: int, kwargs: dict[str, Any],
    ) -> dict[str, object]:
        """两个模型请求共用同一媒体读取链，不调用额外识别模型。"""
        media = self._images
        if (not cast(VisualConfig, self.config).plugin.enabled or media is None
                or not session_id or item_schema_version != 1 or not isinstance(items, list)):
            return {"action": "continue"}
        async with asyncio.timeout(10):
            enriched = await media.enrich(session_id, items)
        if self._images is not media or enriched == items:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {
            **kwargs, "session_id": session_id, "items": enriched, "item_schema_version": item_schema_version,
        }}

    @HookHandler(
        "maisaka.planner.before_request", name="qqbot_visual_private_planner",
        description="把私聊附件和群聊定向引用原图补入规划请求",
        mode=HookMode.BLOCKING, order=HookOrder.NORMAL, timeout_ms=12000, error_policy=ErrorPolicy.SKIP,
    )
    async def prepare_private_planner(
        self, session_id: str = "", items: object = None, item_schema_version: int = 0, **kwargs: Any,
    ) -> dict[str, object]:
        """规划直接观察私聊附件及群聊定向消息的同会话引用图。"""
        return await self._attach_images(session_id, items, item_schema_version, kwargs)

    @HookHandler(
        "maisaka.replyer.before_model_request", name="qqbot_visual_private_replyer",
        description="把私聊附件和群聊定向引用原图补入回复模型请求",
        mode=HookMode.BLOCKING, order=HookOrder.NORMAL, timeout_ms=12000, error_policy=ErrorPolicy.SKIP,
    )
    async def prepare_private_replyer(
        self, session_id: str = "", items: object = None, item_schema_version: int = 0, **kwargs: Any,
    ) -> dict[str, object]:
        """回复器独立取得附件，不依赖规划器把视觉细节转述进摘要。"""
        if kwargs.get("selected_model_visual") is not True:
            return {"action": "continue"}
        return await self._attach_images(session_id, items, item_schema_version, kwargs)


def create_plugin() -> MaiBotPlugin:
    """返回由 Runner 管理生命周期的定向看图插件。"""
    return QQBotVisualPlugin()
