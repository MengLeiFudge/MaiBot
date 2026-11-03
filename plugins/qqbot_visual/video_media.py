"""Resolve QQ video attachments from validated original message segments."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .video_reader import MAX_BYTES, VideoError, VideoSource


async def resolve_qq_video(
    call: Callable[..., Awaitable[Any]], source: Mapping[str, Any],
) -> VideoSource | None:
    """Read a video location from QQ metadata, including merged forwards.

    Args:
        call: Public NapCat API caller with the adapter namespace.
        source: Same-chat original message obtained through the framework API.

    Returns:
        The first HTTP or QQ-owned local video, or None for messages without video.

    Raises:
        VideoError: Ownership, forward limits or video metadata are invalid.
    """
    detail = await call("adapter.napcat.message.get_msg", version="1", message_id=source["message_id"])
    info = source["message_info"]
    group = info.get("group_info")
    expected_group = str(group["group_id"]) if isinstance(group, Mapping) else ""
    if not isinstance(detail, Mapping) or str(detail.get("message_id")) != str(source["message_id"]):
        raise VideoError("QQ未返回对应原视频消息")
    sender = detail.get("sender")
    if (detail.get("message_type") != ("group" if expected_group else "private")
            or str(detail.get("group_id") or "") != expected_group
            or not isinstance(sender, Mapping) or str(sender.get("user_id")) != str(info["user_info"]["user_id"])):
        raise VideoError("QQ原视频消息不属于此会话和发送者")
    raw = detail.get("message")
    if not isinstance(raw, list):
        raise VideoError("QQ未返回结构化视频消息")
    pending = [(raw, 0)]
    visited: set[str] = set()
    count = 0
    while pending:
        segments, depth = pending.pop(0)
        for part in segments:
            count += 1
            if count > 256:
                raise VideoError("转发内容超过256个消息段，请单独发送要看的视频")
            if not isinstance(part, Mapping) or not isinstance(part.get("data"), Mapping):
                continue
            data = part["data"]
            if part.get("type") == "video":
                if int(data.get("file_size") or 0) > MAX_BYTES:
                    raise VideoError("QQ视频超过80 MiB上限")
                location = data
                for attempt in range(2):
                    for candidate in (location.get("url"), location.get("file")):
                        if not isinstance(candidate, str) or not candidate:
                            continue
                        if candidate.startswith(("http://", "https://")):
                            return VideoSource(candidate, direct=True)
                        parsed = urlsplit(candidate)
                        if parsed.scheme == "file":
                            if parsed.netloc not in ("", "localhost"):
                                continue
                            candidate = unquote(parsed.path)
                            if len(candidate) > 3 and candidate[0] == "/" and candidate[2] == ":":
                                candidate = candidate[1:]
                        path = Path(candidate)
                        if path.is_absolute() and not str(path).startswith("\\\\") and path.is_file():
                            return VideoSource("QQ视频附件", direct=True, local_path=path)
                    if attempt == 0:
                        # 转发常保留失效的原路径；文件标识仍指向QQ原始视频元素。
                        file_id = data.get("file_id") or data.get("file")
                        if not isinstance(file_id, str) or not file_id:
                            raise VideoError("QQ原视频缺少可恢复的文件标识")
                        restored = await call("adapter.napcat.file.get_file", version="1", params={"file_id": file_id})
                        if not isinstance(restored, Mapping) or restored.get("status", "ok") != "ok":
                            raise VideoError("QQ未能重新下载原视频文件")
                        location = restored.get("data", restored)
                        if not isinstance(location, Mapping):
                            raise VideoError("QQ未返回下载后的视频文件信息")
                raise VideoError("QQ原视频恢复后仍无可读取的下载地址或本机文件")
            if part.get("type") != "forward":
                continue
            if depth >= 3:
                raise VideoError("合并转发超过3层，请单独发送要看的视频")
            nodes = data.get("messages") or data.get("content")
            if not isinstance(nodes, list):
                forward_id = str(data.get("id") or "")
                if not forward_id or forward_id in visited:
                    continue
                if len(visited) >= 4:
                    raise VideoError("合并转发超过4条，请单独发送要看的视频")
                visited.add(forward_id)
                forwarded = await call("adapter.napcat.message.get_forward_msg", version="1", id=forward_id)
                if not isinstance(forwarded, Mapping):
                    raise VideoError("QQ未返回合并转发中的原视频")
                nodes = forwarded.get("messages") or forwarded.get("content")
            if not isinstance(nodes, list) or len(nodes) > 64:
                raise VideoError("转发节点不可读或超过64条，请单独发送要看的视频")
            for node in nodes:
                if not isinstance(node, Mapping):
                    continue
                content = node.get("content") or node.get("message")
                if not isinstance(content, list) and isinstance(node.get("data"), Mapping):
                    content = node["data"].get("content") or node["data"].get("message")
                if isinstance(content, list):
                    pending.append((content, depth + 1))
    return None
