"""按消息组件顺序解析明确生图指令，在扣分前取得原图。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import asyncio
import base64
import re

import httpx

from .draw_logic import RightCodesDrawRequest, load_draw_reference_image


STANDARD_DRAW_PREFIX = re.compile(r"^\s*(文生图|图生图|头像生图)")
DOUDOUYAN_AVATAR_COMMAND = "生成豆豆眼头像"
OWN_AVATAR = re.compile(r"(?:我(?:的)?|本人(?:的)?|自己(?:的)?)\s*头像")
REFERENCE_DEADLINE = 30.0
OneBotCall = Callable[..., Awaitable[object]]


def explicit_command_text(segments: Sequence[Mapping]) -> str:
    """仅拼接当前消息文本，保留内部空白和换行，不混入引用正文。"""
    parts = []
    for segment in segments:
        if segment.get("type") != "text":
            continue
        data = segment.get("data")
        parts.append(str(data.get("text") or data.get("content") or "") if isinstance(data, Mapping) else str(data or ""))
    return "".join(parts)


async def doudouyan_command_parts(segments: Sequence[Mapping], *, call_action: OneBotCall) -> list[Mapping] | None:
    """调用方已确定生图意图后，将豆豆眼和明确来源映射为单图指令；其他请求返回None。"""
    text = explicit_command_text(segments).strip()
    if text == DOUDOUYAN_AVATAR_COMMAND:
        return [{"type": "text", "data": {"text": "头像生图 豆豆眼"}}]
    if "豆豆眼" not in text:
        return None
    match = STANDARD_DRAW_PREFIX.match(text)
    if match:
        return None if match.group(1) == "文生图" else list(segments)
    if OWN_AVATAR.search(text):
        return [{"type": "text", "data": {"text": "头像生图 豆豆眼"}}]
    images = [part for part in segments if part.get("type") == "image"]
    if images:
        return [{"type": "text", "data": {"text": "图生图 豆豆眼"}}, *images]
    replies = [part for part in segments if part.get("type") == "reply"]
    if not replies:
        return None
    if len(replies) != 1:
        raise ValueError("请只引用一条图片消息")
    data = replies[0].get("data")
    if not isinstance(data, Mapping):
        raise ValueError("无法读取引用消息，请重新引用或附图")
    chain = data.get("chain")
    if not isinstance(chain, list):
        reply_id = data.get("id") or data.get("target_message_id")
        if not reply_id:
            raise ValueError("引用消息缺少标识，请重新引用或附图")
        async with asyncio.timeout(5):
            detail = await call_action("get_msg", message_id=reply_id)
        if isinstance(detail, Mapping):
            chain = detail.get("message") or detail.get("raw_message")
    if not isinstance(chain, list):
        raise ValueError("无法读取引用消息，请重新引用或附图")
    if not any(isinstance(part, Mapping) and part.get("type") == "image" for part in chain):
        return None
    # 复用已核对的引用组件，后续来源准备不重复读取引用消息。
    reply = {**replies[0], "data": {**data, "chain": chain}}
    return [{"type": "text", "data": {"text": "图生图 豆豆眼"}}, reply]


async def prepare_explicit_draw_request(
    segments: Sequence[Mapping],
    *,
    sender_id: str,
    model: str,
    call_action: OneBotCall,
) -> RightCodesDrawRequest:
    """从有序 OneBot/SDK 组件取得原文提示词和单张图，目标及来源歧义直接报错。"""
    text = explicit_command_text(segments)
    match = STANDARD_DRAW_PREFIX.match(text)
    if match is None:
        raise ValueError("没有找到文生图或图生图指令")
    mode = match.group(1)
    prompt = text[match.end():].strip()
    mentions = []
    offset = 0
    for segment in segments:
        kind = segment.get("type")
        data = segment.get("data")
        if kind == "text":
            offset += len(explicit_command_text([segment]))
        elif kind == "at" and offset >= match.end():
            qq = str(data.get("qq") or data.get("target_user_id") or "") if isinstance(data, Mapping) else str(data or "")
            mentions.append((offset, qq))
    target = sender_id
    if mentions:
        if mode != "头像生图":
            raise ValueError("提示词中请用文字描述对象；指定头像请使用“头像生图 @某人 提示词”")
        if len(mentions) != 1 or text[match.end():mentions[0][0]].strip():
            raise ValueError("头像目标不唯一或位置不明确，请将唯一的 @ 紧跟在“头像生图”后")
        target = mentions[0][1]
    if not prompt:
        raise ValueError("请在生图指令后填写提示词")
    if mode == "文生图":
        return RightCodesDrawRequest(prompt=prompt, model=model)

    if "豆豆眼" in prompt:
        try:
            # 按原字节解码，保留固定预设的全部空白和换行。
            prompt = (Path(__file__).parent / "prompts" / "doudouyan.txt").read_bytes().decode("utf-8")
            if not prompt:
                raise ValueError("固定预设为空")
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("豆豆眼固定提示词读取失败，请联系管理员") from exc

    async with asyncio.timeout(REFERENCE_DEADLINE):
        if mode == "头像生图":
            if not re.fullmatch(r"[1-9][0-9]{4,11}", target):
                raise ValueError("无法取得有效的头像目标QQ号，请使用真实的单人艾特")
            source = f"https://q1.qlogo.cn/g?b=qq&nk={target}&s=640"
        else:
            images = [part for part in segments if part.get("type") == "image"]
            if not images:
                replies = [part for part in segments if part.get("type") == "reply"]
                if len(replies) > 1:
                    raise ValueError("请只引用一条图片消息")
                if replies:
                    data = replies[0].get("data")
                    if not isinstance(data, Mapping):
                        raise ValueError("无法读取引用消息，请重新引用或附图")
                    chain = data.get("chain")
                    if not isinstance(chain, list):
                        reply_id = data.get("id") or data.get("target_message_id")
                        if not reply_id:
                            raise ValueError("引用消息缺少标识，请重新引用或附图")
                        detail = await call_action("get_msg", message_id=reply_id)
                        if not isinstance(detail, Mapping):
                            raise ValueError("无法读取引用消息，请重新引用或附图")
                        chain = detail.get("message") or detail.get("raw_message")
                    if not isinstance(chain, list):
                        raise ValueError("引用消息没有可读取的图片组件")
                    images = [part for part in chain if isinstance(part, Mapping) and part.get("type") == "image"]
            if len(images) != 1:
                raise ValueError("图生图需要一张原图，请在同一条消息附一张图片，或引用只含一张图的消息")
            image = images[0]
            data = image.get("data")
            data = data if isinstance(data, Mapping) else {"file": data}
            candidates = [str(data.get(key) or "") for key in ("url", "file", "path")]
            encoded = image.get("binary_data_base64")
            if encoded:
                candidates.insert(0, f"base64://{encoded}")
            source = next((value for value in candidates if re.match(r"^(?:https?://|file://|data:image/|base64://|/|[A-Za-z]:[\\/])", value)), "")
            if not source:
                file_id = data.get("file")
                if not file_id:
                    raise ValueError("图片缺少可读取的地址，请重新发送")
                detail = await call_action("get_image", file=file_id)
                if not isinstance(detail, Mapping):
                    raise ValueError("无法取得图片地址，请重新发送")
                source = str(detail.get("url") or detail.get("file") or detail.get("path") or "")
                if not re.match(r"^(?:https?://|file://|/|[A-Za-z]:[\\/])", source):
                    raise ValueError("图片地址已失效，请重新发送")
        return await preload_draw_references(RightCodesDrawRequest(prompt=prompt, model=model, image_urls=(source,)))


async def preload_draw_references(request: RightCodesDrawRequest) -> RightCodesDrawRequest:
    """在积分预扣前限时下载并验证已选参考图，返回供 CPA 上传复用的 Base64 来源。"""
    if not request.image_urls:
        return request
    sources = []
    async with asyncio.timeout(REFERENCE_DEADLINE):
        async with httpx.AsyncClient(timeout=REFERENCE_DEADLINE, trust_env=False) as client:
            for source in request.image_urls:
                image, mime = await load_draw_reference_image(source, client)
                sources.append(f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}")
    return replace(request, image_urls=tuple(sources))
