from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import asyncio
import base64
import hashlib


MAX_SOURCE_BYTES = 30 * 1024 * 1024


def has_missing_media(message: Mapping[str, Any]) -> bool:
    """只把适配器确认含图片的消息中的降级占位当作下载失败。"""
    additional = message["message_info"].get("additional_config", {})
    if not isinstance(additional, Mapping):
        return False
    original_types = additional.get("napcat_segment_types", [])
    return isinstance(original_types, list) and "image" in original_types and any(
        isinstance(part, Mapping) and part.get("type") == "text" and part.get("data") in ("[image]", "[emoji]")
        for part in message.get("raw_message", [])
    )


def _read_image(data: Mapping[str, Any]) -> bytes:
    """只读 get_image 返回的本机文件或 base64，不读取聊天正文中的路径或 URL。"""
    encoded = data.get("base64")
    if isinstance(encoded, str) and encoded:
        if len(encoded) > (MAX_SOURCE_BYTES + 2) // 3 * 4:
            raise ValueError("原媒体超过 30 MiB")
        binary = base64.b64decode(encoded, validate=True)
    else:
        filename = data.get("file")
        if not isinstance(filename, str) or not filename:
            raise ValueError("NapCat 未返回本机媒体文件")
        path = Path(filename)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("NapCat 媒体路径不是本机绝对文件路径")
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("原媒体超过 30 MiB")
        with path.open("rb") as stream:
            binary = stream.read(MAX_SOURCE_BYTES + 1)
    if not binary or len(binary) > MAX_SOURCE_BYTES:
        raise ValueError("原媒体为空或超过 30 MiB")
    return binary


async def recover_private_media(ctx: Any, source: Mapping[str, Any]) -> list[dict[str, str]]:
    """用原消息的文件标识请求 QQ 本机下载，绕开下载失败的远程图片链接。"""
    message_id = source["message_id"]
    detail = await ctx.api.call("adapter.napcat.message.get_msg", version="1", message_id=message_id)
    if not isinstance(detail, Mapping) or str(detail.get("message_id")) != message_id:
        raise ValueError("NapCat 未返回对应原消息")
    sender = detail.get("sender")
    owner = source["message_info"]["user_info"]["user_id"]
    if (detail.get("message_type") != "private" or detail.get("group_id")
            or not isinstance(sender, Mapping) or str(sender.get("user_id")) != str(owner)):
        raise ValueError("NapCat 原消息不属于此私聊发送者")
    raw = detail.get("message")
    if not isinstance(raw, list):
        raise ValueError("NapCat 未返回结构化原消息")
    segments: list[dict[str, str]] = []
    for part in raw:
        if not isinstance(part, Mapping) or part.get("type") != "image":
            continue
        data = part.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("NapCat 图片段缺少文件信息")
        kind = "image" if str(data.get("sub_type", 0)) in ("0", "4", "9") else "emoji"
        file_id = data.get("file") or data.get("file_id")
        try:
            if not isinstance(file_id, str) or not file_id:
                raise ValueError("NapCat 图片段缺少文件标识")
            response = await ctx.api.call("adapter.napcat.file.get_image", version="1", params={"file": file_id})
            if (not isinstance(response, Mapping) or response.get("status") != "ok"
                    or not isinstance(response.get("data"), Mapping)):
                raise ValueError("NapCat 本机媒体下载失败")
            binary = await asyncio.to_thread(_read_image, response["data"])
            segments.append({
                "type": kind, "hash": hashlib.sha256(binary).hexdigest(),
                "binary_data_base64": base64.b64encode(binary).decode("ascii"),
            })
        except Exception as exc:
            ctx.logger.warning("私聊原媒体恢复失败: msg_id=%s error_type=%s", message_id, type(exc).__name__)
            segments.append({"type": kind})
        if len(segments) >= 8:
            break
    if not segments:
        raise ValueError("NapCat 原消息不含图片段")
    return segments
