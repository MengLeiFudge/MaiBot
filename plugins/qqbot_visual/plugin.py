from __future__ import annotations

from collections.abc import Mapping

from pathlib import Path
from typing import Any, ClassVar, cast

import asyncio


from maibot_sdk import ON_BOT_CONFIG_RELOAD, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder


from .history import read_history
from .video_context import VideoContext
from .video_reader import VideoReader

class PluginSection(PluginConfigBase):
    """视频和同会话历史工具的启用状态。"""

    enabled: bool = Field(default=True, description="是否启用视频分镜和历史阅读工具")
    config_version: str = Field(default="0.1.0", description="配置版本")


class VisualConfig(PluginConfigBase):
    """本人身份和称呼复用 bot 配置，不复制到插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    video_enabled: bool = Field(default=True, description="下载当前视频链接并用分镜图理解内容")





class QQBotVisualPlugin(MaiBotPlugin):
    """完整保留入站媒体，补充视频与同会话历史阅读。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = VisualConfig
    config_reload_subscriptions: ClassVar[tuple[str, ...]] = (ON_BOT_CONFIG_RELOAD,)

    def __init__(self) -> None:
        """保存当前 QQ 身份，视频读取器在加载时创建。"""
        super().__init__()
        self._bot_id = ""

        self._video_reader: VideoReader | None = None
        self._video_context: VideoContext | None = None

    async def _reload_bot_config(self) -> None:
        """读取当前 QQ 身份，不接管原生图片处理。"""
        async with asyncio.timeout(3):
            platform, bot_id = await asyncio.gather(
                self.ctx.config.get("bot.platform"), self.ctx.config.get("bot.qq_account"),
            )
        if platform != "qq" or not isinstance(bot_id, str) or not bot_id.isascii() or not bot_id.isdecimal():
            raise ValueError("视频插件需要 bot.platform=qq 和真实 bot.qq_account")
        self._bot_id = bot_id

    async def on_load(self) -> None:
        """读取身份配置并创建视频读取器，加载阶段不处理媒体。"""
        await self._reload_bot_config()
        self._video_reader = VideoReader(Path(self.ctx.paths.data_dir) / "videos")
        self._video_context = VideoContext(self.ctx, self._video_reader)
        self.ctx.logger.info("视频分镜已加载：enabled=%s tools_ready=%s", cast(VisualConfig, self.config).video_enabled, self._video_reader.tools_ready)


    async def on_unload(self) -> None:
        """等待视频任务取消并清空配置副本。"""
        if self._video_reader is not None:
            await self._video_reader.close()
            self._video_reader = None
        if self._video_context is not None:
            self._video_context.clear()
            self._video_context = None
        self._bot_id = ""


    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """跟随 bot 配置更新当前 QQ 身份。"""
        del config_data
        if scope == ON_BOT_CONFIG_RELOAD:
            await self._reload_bot_config()
            self.ctx.logger.info("视频插件配置已更新: version=%s", version)

    @HookHandler(
        "chat.receive.before_process", name="qqbot_visual_video",
        description="登记当前视频来源，由实际主模型请求读取DSH分镜",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=5000, error_policy=ErrorPolicy.SKIP,
    )
    async def read_video(self, message: object = None, **kwargs: Any) -> dict[str, object]:
        """登记视频来源，原消息与原生触发逻辑保持完整。"""
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



    @Tool("read_chat_history", description="读取当前会话更早的完整原消息与图片。默认30条，使用返回的消息ID继续向前翻页；只能读取当前会话。",
          parameters={"count": {"type": "integer", "minimum": 1, "maximum": 100, "description": "本次读取条数，默认30"},
                      "before_message_id": {"type": "string", "description": "读取此消息之前的上文；留空读取近期，后续使用返回的next_before_message_id"}},
          timeout_ms=35000)
    async def read_chat_history(self, count: object = 30, before_message_id: object = "", **kwargs: Any) -> dict:
        """会话身份由运行时提供，不接受模型指定另一群或另一私聊。"""
        session_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "")
        return await read_history(self.ctx, session_id, count, before_message_id)


def create_plugin() -> MaiBotPlugin:
    """返回由 Runner 管理生命周期的视频与历史插件。"""
    return QQBotVisualPlugin()
