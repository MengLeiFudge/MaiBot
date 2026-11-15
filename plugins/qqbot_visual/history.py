"""通过公开消息 API 向前翻阅本会话历史，媒体作为实际工具内容返回。"""
from collections.abc import Mapping
from datetime import datetime
from io import BytesIO
from typing import Any

import asyncio
import base64
import json
import time

from PIL import Image

from .media_recovery import has_missing_media, recover_media




def _timestamp(message: Mapping[str, Any]) -> float:
    """公开消息 API 使用 ISO 时间，转换后保留微秒和原时区。"""
    return datetime.fromisoformat(str(message["timestamp"])).timestamp()


def _segments(raw: list[Any], source: str, media: list[dict[str, Any]]) -> list[Any]:
    """保留来源树，把二进制交给工具媒体通道，避免作为文本里的 base64。"""
    result = []
    for value in raw:
        if not isinstance(value, Mapping):
            result.append(value)
            continue
        part = dict(value)
        encoded = part.pop("binary_data_base64", None)
        if part.get("type") in {"image", "emoji"}:
            if isinstance(encoded, str) and encoded:
                try:
                    with Image.open(BytesIO(base64.b64decode(encoded, validate=True))) as image:
                        mime = Image.MIME.get(image.format or "", "")
                    if not mime:
                        raise ValueError("历史图片格式无法识别")
                    media.append({"content_type": "image", "data": encoded, "mime_type": mime,
                                  "description": f"历史消息 {source} 的原图", "metadata": {"message_id": source}})
                    part["media_status"] = "原图已附在工具媒体中"
                except Exception as exc:
                    part["media_status"] = f"原图无法解码：{type(exc).__name__}，不代表已经看过"
            else:
                part["media_status"] = "原图未取得，不代表已经看过"
        elif part.get("type") == "forward" and isinstance(part.get("data"), list):
            part["data"] = [{**node, "content": _segments(node.get("content", []), source, media)}
                            if isinstance(node, Mapping) else node for node in part["data"]]
        elif encoded:
            part["media_status"] = "已保留原附件来源，可按消息ID继续读取"
        result.append(part)
    return result


async def read_history(ctx: Any, session_id: str, count: object = 30, before_message_id: object = "") -> dict[str, Any]:
    """按时间及同一时刻的消息 ID 稳定翻页，禁止接受模型指定其他会话。"""
    if not session_id or type(count) is not int or not 1 <= count <= 100 or not isinstance(before_message_id, str):
        return {"success": False, "content": "需要当前真实会话；count须为1–100的整数，before_message_id须为文本。"}
    try:
        async with asyncio.timeout(30):
            stamp = time.time()
            tied = []
            if before_message_id:
                anchor = await ctx.message.get_by_id(before_message_id, chat_id=session_id, include_binary_data=False)
                if (not isinstance(anchor, Mapping) or anchor.get("session_id") != session_id
                        or anchor.get("message_id") != before_message_id):
                    raise ValueError("翻页消息不属于当前会话或已经无法取得")
                stamp = _timestamp(anchor)
                # 同一时刻可能有多条消息，单独读取此刻，避免只减时间造成漏页。
                tied = await ctx.call_capability("message.get_by_time_in_chat", chat_id=session_id,
                                                start_time=stamp, end_time=stamp, limit=0,
                                                filter_mai=False, include_binary_data=True)
                if not isinstance(tied, list) or any(not isinstance(row, Mapping)
                        or row.get("session_id") != session_id for row in tied):
                    raise ValueError("历史查询没有返回消息列表")
                tied = [row for row in tied if str(row.get("message_id", "")) < before_message_id]
                stamp -= 0.000001
            rows = await ctx.call_capability("message.get_by_time_in_chat", chat_id=session_id,
                                             start_time=0.0, end_time=stamp, limit=count + 1,
                                             filter_mai=False, include_binary_data=True)
            if not isinstance(rows, list):
                raise ValueError("历史查询没有返回消息列表")
            rows = [*rows, *tied]
            if any(not isinstance(row, Mapping) or row.get("session_id") != session_id for row in rows):
                raise ValueError("历史查询返回了其他会话，已停止读取")
            if rows:
                # 每一页都补齐边界的同刻消息，数据库不保证相同时间的返回次序。
                boundary = min(_timestamp(row) for row in rows)
                same_time = await ctx.call_capability("message.get_by_time_in_chat", chat_id=session_id,
                                                     start_time=boundary, end_time=boundary, limit=0,
                                                     filter_mai=False, include_binary_data=True)
                if not isinstance(same_time, list) or any(
                    not isinstance(row, Mapping) or row.get("session_id") != session_id for row in same_time
                ):
                    raise ValueError("历史查询边界不属于当前会话")
                rows = list({row["message_id"]: row for row in [*rows, *same_time]}.values())
            if before_message_id:
                rows = [row for row in rows if (_timestamp(row), str(row["message_id"]))
                        < (_timestamp(anchor), before_message_id)]
            rows.sort(key=lambda row: (_timestamp(row), str(row["message_id"])))
            has_more = len(rows) > count
            rows = rows[-count:]
            messages, media = [], []
            for row in rows:
                raw = row.get("raw_message", [])
                missing = has_missing_media(row) or any(
                    isinstance(part, Mapping) and part.get("type") in {"image", "emoji"}
                    and not part.get("binary_data_base64") for part in raw)
                if missing:
                    try:
                        restored = await recover_media(ctx, row)
                        originals = iter(restored)
                        raw = [{**part, **next(originals, {})} if isinstance(part, Mapping) and part.get("type") in {"image", "emoji"}
                               else part for part in raw]
                        if not any(isinstance(part, Mapping) and part.get("type") in {"image", "emoji"} for part in raw):
                            raw = [*raw, *restored]
                    except Exception as exc:
                        raw = [*raw, {"type": "text", "data": f"[此消息原附件未取得：{type(exc).__name__}，不能声称看过]"}]
                info = row.get("message_info", {})
                messages.append({"message_id": row["message_id"], "timestamp": row["timestamp"],
                                 "sender": info.get("user_info", {}), "reply_to": row.get("reply_to"),
                                 "content": _segments(raw, row["message_id"], media)})
            return {"success": True, "content": json.dumps({"messages": messages, "has_more": has_more,
                    "next_before_message_id": rows[0]["message_id"] if has_more and rows else ""}, ensure_ascii=False),
                    "content_items": media}
    except Exception as exc:
        ctx.logger.warning("历史查阅失败: error_type=%s", type(exc).__name__)
        return {"success": False, "content": f"本会话历史读取失败：{type(exc).__name__}。没有取得内容，不能当作没有历史。"}
