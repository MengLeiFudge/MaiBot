from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import base64

from maibot_sdk import PluginContext

from .video_reader import VideoError, VideoOverview, VideoReader, find_video_sources


async def enrich_video_message(ctx: PluginContext, reader: VideoReader, message: dict[str, Any]) -> dict[str, Any] | None:
    """把当前视频的画面描述追加到消息副本，失败时明确标注尚未读取。"""
    raw = message.get("raw_message")
    if not isinstance(raw, list):
        return None
    text = "\n".join(
        str(part.get("data") or "") for part in raw
        if isinstance(part, Mapping) and part.get("type") == "text"
    )
    sources = find_video_sources(text)
    if not sources and "[视频]" not in text and "[视频文件]" not in text:
        return None

    async def describe(overview: VideoOverview) -> str:
        """使用当前宿主的 vlm 任务读取一张分镜图，不另建模型配置。"""
        result = await ctx.llm.generate(
            prompt=[
                {"role": "system", "content": "根据分镜图提供可信的视频画面描述，明确区分画面与未提供的声音信息。"},
                {"role": "user", "content": [
                    {"type": "text", "text": overview.prompt},
                    {"type": "image", "image_format": "jpeg", "image_base64": base64.b64encode(overview.image).decode("ascii")},
                ]},
            ], model="vlm", temperature=0.2, max_tokens=2400, timeout_ms=45000,
        )
        if not isinstance(result, dict) or not result.get("success"):
            raise VideoError("视频识图失败，请检查现有vlm模型配置")
        return str(result.get("response") or "")

    if not sources:
        description = "[视频未读取] 当前附件没有可用下载地址，请改发视频链接或重新发送原视频。不能声称已经看过。"
    else:
        try:
            description = await reader.analyze(sources[0], describe)
            ctx.logger.info("视频分镜描述已完成")
        except VideoError as exc:
            description = f"[视频未读取] {exc}。请明确说明尚未看到视频，不能根据标题或其他图片编造内容。"
            ctx.logger.warning("视频读取失败：%s", exc)
        except Exception as exc:
            description = "[视频未读取] 下载、抽帧或视觉模型调用失败，请重发可访问的短视频；不能声称已经看过。"
            ctx.logger.warning("视频分析失败：error_type=%s", type(exc).__name__)
    modified = deepcopy(message)
    modified["raw_message"].append({"type": "text", "data": "\n" + description})
    return modified
