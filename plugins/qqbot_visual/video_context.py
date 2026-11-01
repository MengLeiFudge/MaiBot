from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from html import unescape
from typing import Any

import asyncio
import base64
import re
import time

from maibot_sdk import PluginContext

from .video_reader import CACHE_SECONDS, VIDEO_CAPABILITY, VideoError, VideoReader, VideoSource, find_video_sources

MESSAGE_PREFIX = re.compile(r'^<message msg_id="([^"]+)"(?:\s|>)')


class VideoContext:
    """按真实会话和消息绑定视频，直接为两个主模型请求补充图片。"""

    def __init__(self, ctx: PluginContext, reader: VideoReader) -> None:
        self.ctx = ctx
        self.reader = reader
        self._sources: OrderedDict[tuple[str, str], tuple[float, VideoSource]] = OrderedDict()

    def clear(self) -> None:
        """卸载时清空消息来源映射；图片由 reader 管理。"""
        self._sources.clear()

    def remember(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """入站只登记来源，下载延后到实际聊天请求，避免阻塞适配器。"""
        raw = message.get("raw_message")
        if not isinstance(raw, list):
            return None
        text = "\n".join(str(part.get("data") or "") for part in raw
                         if isinstance(part, Mapping) and part.get("type") == "text")
        sources = find_video_sources(text)
        session_id, message_id = message.get("session_id"), message.get("message_id")
        if sources and isinstance(session_id, str) and session_id and isinstance(message_id, str) and message_id:
            key = (session_id, message_id)
            self._sources[key] = (time.monotonic(), sources[0])
            self._sources.move_to_end(key)
            while len(self._sources) > 128:
                self._sources.popitem(last=False)
        elif not sources and ("[视频]" in text or "[视频文件]" in text):
            modified = deepcopy(message)
            modified["raw_message"].append({"type": "text", "data": "\n[视频未读取] 当前附件没有可用下载地址，请改发视频链接或重新发送原视频。不能声称已经看过。"})
            return modified
        return None

    async def enrich(self, session_id: str, items: list[Any], *, visual: bool = True) -> list[Any]:
        """只把分镜附到原消息；群聊和私聊共用，优先本轮最新的视频。"""
        enriched = list(items)
        if self.reader.tools_ready and visual:
            for index, item in enumerate(items):
                if not isinstance(item, Mapping) or item.get("item_type") != "SystemMessageItem":
                    continue
                parts = item.get("parts")
                if isinstance(parts, list) and not any(isinstance(part, Mapping) and part.get("text") == VIDEO_CAPABILITY for part in parts):
                    enriched[index] = {**item, "parts": [*parts, {"type": "text", "text": VIDEO_CAPABILITY}]}
                break
        now = time.monotonic()
        for key, (at, _) in list(self._sources.items()):
            if now - at >= CACHE_SECONDS:
                del self._sources[key]
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if not isinstance(item, Mapping) or item.get("item_type") != "UserMessageItem":
                continue
            parts = item.get("parts")
            if not isinstance(parts, list) or not parts or not isinstance(parts[0], Mapping):
                continue
            text = parts[0].get("text")
            match = MESSAGE_PREFIX.match(text) if isinstance(text, str) else None
            if match is None:
                continue
            message_id = unescape(match[1])
            source = self._sources.get((session_id, message_id))
            if source is None and (not isinstance(text, str) or not find_video_sources(text)):
                continue
            if any(isinstance(part, Mapping) and part.get("type") == "text" and str(part.get("text", "")).startswith("[视频分镜已读取") for part in parts[1:]):
                return enriched
            try:
                # 重启后只恢复本轮上下文中的真实消息，不扫描别的会话。
                if source is None:
                    async with asyncio.timeout(3):
                        loaded = await self.ctx.message.get_by_id(message_id, chat_id=session_id, include_binary_data=False)
                    if not isinstance(loaded, dict) or loaded.get("session_id") != session_id or loaded.get("message_id") != message_id or loaded.get("platform") != "qq":
                        raise VideoError("无法取得同会话的原视频消息，请重新发送视频链接")
                    self.remember(loaded)
                    source = self._sources.get((session_id, message_id))
                    if source is None:
                        raise VideoError("原视频消息没有可用下载地址，请重新发送视频链接")
                if not visual:
                    raise VideoError("当前聊天模型未启用视觉能力，请启用该模型的visual配置后重试")
                overview = await self.reader.analyze(source[1])
                additions = [{"type": "text", "text": overview.prompt}]
                additions.extend({"type": "image", "image_format": "png", "image_base64": base64.b64encode(image).decode("ascii")} for image in overview.images)
                self.ctx.logger.info("DSH分镜已附到主模型: msg_id=%s images=%s cells=%s duration=%.2fs", message_id, len(overview.images), overview.evidence["cells_total"], overview.duration)
            except VideoError as exc:
                additions = [{"type": "text", "text": f"[视频未读取] {exc}。请明确说明实际失败原因，不能根据标题或其他图片编造内容。"}]
                self.ctx.logger.warning("视频读取失败: msg_id=%s error=%s", message_id, exc)
            except Exception as exc:
                additions = [{"type": "text", "text": "[视频未读取] 下载或DSH分镜处理失败，请重新发送可访问的短视频；不能声称已经看过。"}]
                self.ctx.logger.warning("视频分镜失败: msg_id=%s error_type=%s", message_id, type(exc).__name__)
            enriched[index] = {**item, "parts": [*parts, *additions]}
            return enriched
        return enriched
