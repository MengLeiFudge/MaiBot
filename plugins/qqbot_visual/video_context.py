from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from html import unescape
from typing import Any

import asyncio
import base64
import re
import time

from maibot_sdk import PluginContext

from .video_reader import CACHE_SECONDS, TOTAL_TIMEOUT, VIDEO_CAPABILITY, VideoError, VideoReader, VideoSource, find_video_sources
from .video_media import resolve_qq_video

MESSAGE_PREFIX = re.compile(r'^<message msg_id="([^"]+)"(?:\s|>)')


def _source_text(raw: list[Any]) -> str:
    """有界读取顶层及转发节点的文本，保留原生视频标记。"""
    pending = [(raw, 0)]
    texts: list[str] = []
    count = 0
    while pending and count < 256:
        segments, depth = pending.pop(0)
        for part in segments:
            count += 1
            if count > 256:
                break
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "text" and isinstance(part.get("data"), str):
                texts.append(part["data"])
            elif part.get("type") == "forward" and depth < 3 and isinstance(part.get("data"), list):
                for node in part["data"][:64]:
                    if isinstance(node, Mapping) and isinstance(node.get("content"), list):
                        pending.append((node["content"], depth + 1))
    return "\n".join(texts)


class VideoContext:
    """按真实会话和消息绑定视频，直接为两个主模型请求补充图片。"""

    def __init__(self, ctx: PluginContext, reader: VideoReader) -> None:
        self.ctx = ctx
        self.reader = reader
        self._sources: OrderedDict[tuple[str, str], tuple[float, VideoSource | None]] = OrderedDict()
        self._failures: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()

    def clear(self) -> None:
        """卸载时清空消息来源映射；图片由 reader 管理。"""
        self._sources.clear()
        self._failures.clear()

    def remember(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """入站只登记来源，下载延后到实际聊天请求，避免阻塞适配器。"""
        raw = message.get("raw_message")
        if not isinstance(raw, list):
            return None
        text = _source_text(raw)
        sources = find_video_sources(text)
        session_id, message_id = message.get("session_id"), message.get("message_id")
        if ((sources or "[视频]" in text or "[视频文件]" in text)
                and isinstance(session_id, str) and session_id and isinstance(message_id, str) and message_id):
            key = (session_id, message_id)
            self._sources[key] = (time.monotonic(), sources[0] if sources else None)
            self._sources.move_to_end(key)
            while len(self._sources) > 128:
                self._sources.popitem(last=False)
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
        for key, (at, _) in list(self._failures.items()):
            if now - at >= 120:
                del self._failures[key]
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
            if source is None and (not isinstance(text, str) or not (
                    find_video_sources(text) or "[视频]" in text or "[视频文件]" in text)):
                continue
            if any(isinstance(part, Mapping) and part.get("type") == "text" and str(part.get("text", "")).startswith("[视频分镜已读取") for part in parts[1:]):
                return enriched
            failure = self._failures.get((session_id, message_id))
            if failure is not None:
                enriched[index] = {**item, "parts": [*parts, {"type": "text", "text": failure[1]}]}
                return enriched
            deadline = time.monotonic() + TOTAL_TIMEOUT
            phase = "QQ原视频取得"
            try:
                # 重启后只恢复本轮上下文中的真实消息，不扫描别的会话。
                if source is None or source[1] is None:
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
                video = source[1]
                if video is None:
                    async with asyncio.timeout(min(15, max(0.01, deadline - time.monotonic()))):
                        video = await resolve_qq_video(self.ctx.api.call, loaded)
                    if video is None:
                        raise VideoError("原消息或合并转发中没有可读取的视频段")
                    self._sources[(session_id, message_id)] = (time.monotonic(), video)
                phase = "视频下载与分镜、字幕处理"
                async with asyncio.timeout(max(0.01, deadline - time.monotonic())):
                    overview = await self.reader.analyze(video)
                additions = [{"type": "text", "text": overview.prompt}]
                additions.extend({"type": "image", "image_format": "png", "image_base64": base64.b64encode(image).decode("ascii")} for image in overview.images)
                self.ctx.logger.info("DSH分镜已附到主模型: msg_id=%s images=%s cells=%s duration=%.2fs", message_id, len(overview.images), overview.evidence["cells_total"], overview.duration)
            except VideoError as exc:
                additions = [{"type": "text", "text": f"[视频未读取] {exc}。请明确说明实际失败原因，不能根据标题或其他图片编造内容。"}]
                self.ctx.logger.warning("视频读取失败: msg_id=%s error=%s", message_id, exc)
            except TimeoutError:
                additions = [{"type": "text", "text": f"[视频未读取] 已收到视频链接或附件记录，但{phase}超时，未取得可读结果。请说明实际下载/处理失败，不能说用户没发视频或只发路径，也不能声称已看过。"}]
                self.ctx.logger.warning("视频读取超时: msg_id=%s phase=%s", message_id, phase)
            except Exception as exc:
                additions = [{"type": "text", "text": "[视频未读取] 下载或DSH分镜处理失败，请重新发送可访问的短视频；不能声称已经看过。"}]
                self.ctx.logger.warning("视频分镜失败: msg_id=%s error_type=%s", message_id, type(exc).__name__)
            if visual and phase == "QQ原视频取得" and additions[0]["text"].startswith("[视频未读取]"):
                # QQ原文件下载可在协议端继续等待，120秒内不重复发起同一下载。
                self._failures[(session_id, message_id)] = (time.monotonic(), additions[0]["text"])
                self._failures.move_to_end((session_id, message_id))
                while len(self._failures) > 128:
                    self._failures.popitem(last=False)
            enriched[index] = {**item, "parts": [*parts, *additions]}
            return enriched
        return enriched
