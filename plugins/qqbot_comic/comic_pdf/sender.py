from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncio
import base64

from qqbot_common.api_results import require_api_result

from .models import ComicPdfArtifact, ComicPdfError


ApiCaller = Callable[..., Awaitable[Any]]


async def send_private_pdfs_with_password(
    call_api: ApiCaller,
    user_id: int,
    album_id: str,
    artifacts: Iterable[ComicPdfArtifact],
    *,
    title: str,
    author: str,
    tags: Iterable[str] = (),
) -> int:
    """Announce, send every encrypted part through NapCat, then confirm."""
    password = str(album_id or "").strip()
    if not password.isdigit():
        raise ComicPdfError("JM PDF 密码无法由作品 ID 生成。")
    paths = tuple(_validated_pdf_path(item) for item in artifacts)
    if not paths:
        raise ComicPdfError("待发送 PDF 不存在，任务已终止。")
    tag_text = "、".join(dict.fromkeys(str(item or "").strip() for item in tags if str(item or "").strip())) or "未提供"
    summary = (
        f"JM{password} 加密完成，准备发送。\n"
        f"名称：JM{password}\n"
        f"标题：{str(title or '').strip() or f'JM{password}'}\n"
        f"作者：{str(author or '').strip() or '未知作者'}\n"
        f"标签：{tag_text}\n"
        f"文件切片：共 {len(paths)} 份\n"
        f"密码：{password}"
    )
    await _send(call_api, user_id, [{"type": "text", "data": {"text": summary}}], "发送 JM 元数据")
    for path in paths:
        encoded = await asyncio.to_thread(_base64_file, path)
        await _send(
            call_api,
            user_id,
            [{"type": "file", "data": {"file": encoded, "name": path.name}}],
            f"发送 JM 文件 {path.name}",
        )
    await _send(
        call_api,
        user_id,
        [{"type": "text", "data": {"text": f"JM{password}发送完成"}}],
        "发送 JM 完成通知",
    )
    return len(paths)


async def _send(call_api: ApiCaller, user_id: int, message: list[dict[str, object]], label: str) -> None:
    result = await call_api(
        "adapter.napcat.message.send_private_msg",
        params={"user_id": str(int(user_id)), "message": message},
    )
    require_api_result(result, label)


def _validated_pdf_path(artifact: ComicPdfArtifact) -> Path:
    path = artifact.path.resolve()
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ComicPdfError("待发送 PDF 不存在，任务已终止。")
    return path


def _base64_file(path: Path) -> str:
    return "base64://" + base64.b64encode(path.read_bytes()).decode("ascii")
