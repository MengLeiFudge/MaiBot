from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncio
import base64

from qqbot_common.api_results import require_api_result

from .hashing import calculate_artifact_content_sha256, calculate_sha256, calculate_zip_content_sha256
from .models import (
    ArtifactFile,
    ArtifactRequestError,
    PublicationState,
    PublishContext,
    PublishRequest,
    PublishResult,
)
from .process_lock import interprocess_lock
from .state import ArtifactStateStore


ApiCaller = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class PublisherLimits:
    max_zip_entries: int = 10000
    max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    lock_timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class _VerifiedArtifact:
    artifact: ArtifactFile
    sha256: str
    content_sha256: str


@dataclass(slots=True)
class _PendingNotice:
    reply_message_id: str = ""
    file_message: str = ""
    states: list[tuple[str, str]] = field(default_factory=list)


class ArtifactPublisher:
    """Serialize deduplicated group-file publication through NapCat."""

    def __init__(
        self,
        call_api: ApiCaller,
        *,
        runtime_root: Path,
        self_id: str,
        limits: PublisherLimits,
    ) -> None:
        self._call_api = call_api
        self._self_id = str(self_id).strip()
        self._limits = limits
        self._state = ArtifactStateStore(runtime_root)
        self._local_lock = asyncio.Lock()

    async def publish(self, request: PublishRequest) -> PublishResult:
        async with self._local_lock:
            async with interprocess_lock(
                self._state.lock_path,
                timeout_seconds=self._limits.lock_timeout_seconds,
            ):
                verified = [await self._verify_artifact(item) for item in request.files]
                return await self._publish_verified(verified, request.context)

    async def _verify_artifact(self, artifact: ArtifactFile) -> _VerifiedArtifact:
        sha256_task = asyncio.to_thread(calculate_sha256, artifact.path)
        if artifact.path.suffix.lower() == ".apk":
            sha256 = await sha256_task
            content_sha256 = await asyncio.to_thread(
                calculate_artifact_content_sha256,
                artifact.path,
                sha256,
                max_entries=self._limits.max_zip_entries,
                max_uncompressed_bytes=self._limits.max_uncompressed_bytes,
            )
        else:
            sha256, content_sha256 = await asyncio.gather(
                sha256_task,
                asyncio.to_thread(
                    calculate_zip_content_sha256,
                    artifact.path,
                    max_entries=self._limits.max_zip_entries,
                    max_uncompressed_bytes=self._limits.max_uncompressed_bytes,
                ),
            )
        if artifact.sha256 and artifact.sha256 != sha256:
            raise ArtifactRequestError("Artifact sha256 does not match server calculation.")
        if artifact.content_sha256 and artifact.content_sha256 != content_sha256:
            raise ArtifactRequestError("Artifact content_sha256 does not match server calculation.")
        return _VerifiedArtifact(artifact=artifact, sha256=sha256, content_sha256=content_sha256)

    async def _publish_verified(
        self,
        verified_artifacts: list[_VerifiedArtifact],
        context: PublishContext,
    ) -> PublishResult:
        result = PublishResult()
        notices: dict[int, _PendingNotice] = {}
        for verified in verified_artifacts:
            file_reference = ""
            for group_id in verified.artifact.targets:
                previous = self._state.load(group_id, verified.artifact.name)
                if previous is not None and previous.content_sha256 == verified.content_sha256:
                    result.skipped.append(
                        {
                            "group_id": group_id,
                            "name": verified.artifact.name,
                            "sha256": verified.sha256,
                            "content_sha256": verified.content_sha256,
                            "reason": "artifact content sha256 unchanged",
                        }
                    )
                    if not previous.notice_sent:
                        notice = notices.setdefault(group_id, _PendingNotice())
                        notice.reply_message_id = previous.reply_message_id
                        notice.file_message = verified.artifact.message
                        notice.states.append((verified.artifact.name, verified.content_sha256))
                    continue

                if not file_reference:
                    file_reference = await asyncio.to_thread(
                        _build_verified_file_reference,
                        verified.artifact.path,
                        verified.sha256,
                    )
                deleted_names = await self._delete_same_named_bot_files(group_id, verified.artifact.name)
                result.deleted.extend({"group_id": group_id, "name": name} for name in deleted_names)
                upload_payload = await self._call(
                    "adapter.napcat.file.upload_group_file",
                    {
                        "group_id": group_id,
                        "file": file_reference,
                        "name": verified.artifact.name,
                    },
                    "上传群文件",
                )
                reply_message_id = _extract_message_id(upload_payload)
                if not reply_message_id:
                    reply_message_id = await self._find_uploaded_file_message_id(
                        group_id,
                        verified.artifact.name,
                    )
                self._state.save(
                    group_id,
                    PublicationState(
                        name=verified.artifact.name,
                        content_sha256=verified.content_sha256,
                        reply_message_id=reply_message_id,
                        notice_sent=False,
                    ),
                )
                notice = notices.setdefault(group_id, _PendingNotice())
                notice.reply_message_id = reply_message_id
                notice.file_message = verified.artifact.message
                notice.states.append((verified.artifact.name, verified.content_sha256))
                result.uploaded.append(
                    {
                        "group_id": group_id,
                        "name": verified.artifact.name,
                        "sha256": verified.sha256,
                        "content_sha256": verified.content_sha256,
                    }
                )

        for group_id, notice in notices.items():
            await self._send_notice(group_id, context, notice)
            for file_name, content_sha256 in notice.states:
                self._state.mark_notice_sent(group_id, file_name, content_sha256)
        return result

    async def _delete_same_named_bot_files(self, group_id: int, file_name: str) -> list[str]:
        payload = await self._call(
            "adapter.napcat.file.get_group_root_files",
            {"group_id": group_id},
            "读取群文件列表",
        )
        deleted: list[str] = []
        for file_info in _extract_group_files(payload):
            if _first_text(file_info, "file_name", "name") != file_name:
                continue
            uploader = _first_text(file_info, "uploader", "uploader_id", "user_id", "sender_id")
            if not uploader or uploader != self._self_id:
                continue
            file_id = _first_text(file_info, "file_id", "id")
            if not file_id:
                continue
            params: dict[str, object] = {"group_id": group_id, "file_id": file_id}
            busid = _first_value(file_info, "busid", "bus_id")
            if busid is not None:
                params["busid"] = busid
            await self._call(
                "adapter.napcat.file.delete_group_file",
                params,
                "删除旧群文件",
            )
            deleted.append(file_name)
        return deleted

    async def _find_uploaded_file_message_id(self, group_id: int, file_name: str) -> str:
        for attempt in range(5):
            try:
                payload = await self._call(
                    "adapter.napcat.message.get_group_msg_history",
                    {"group_id": group_id, "count": 20},
                    "读取群消息历史",
                )
            except Exception:
                payload = None
            message_id = _extract_file_message_id(payload, file_name)
            if message_id:
                return message_id
            if attempt < 4:
                await asyncio.sleep(0.3)
        return ""

    async def _send_notice(self, group_id: int, context: PublishContext, notice: _PendingNotice) -> None:
        text = build_publish_notice(context, notice.file_message)
        message: list[dict[str, object]] = []
        if notice.reply_message_id:
            message.append({"type": "reply", "data": {"id": notice.reply_message_id}})
        message.append({"type": "text", "data": {"text": text}})
        await self._call(
            "adapter.napcat.group.send_group_msg",
            {"group_id": group_id, "message": message},
            "发送构建发布说明",
        )

    async def _call(self, api_name: str, params: dict[str, object], label: str) -> Any:
        result = await self._call_api(api_name, params=params)
        payload = require_api_result(result, label)
        if isinstance(payload, Mapping) and "data" in payload and (
            "status" in payload or "retcode" in payload
        ):
            return payload.get("data")
        return payload


def build_publish_notice(context: PublishContext, file_message: str = "") -> str:
    commit = context.commit_hash[:7]
    subject = context.commit_subject.strip()
    headline = f"{commit} {subject}".strip() or "本次构建"
    lines = [headline]
    if context.branch.strip():
        lines.append(f"分支：{context.branch.strip()}")
    detail = file_message.strip() or context.commit_detail.strip()
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def _build_verified_file_reference(path: Path, expected_sha256: str) -> str:
    if calculate_sha256(path) != expected_sha256:
        raise ArtifactRequestError("Artifact changed while the publish request was running.")
    windows_path = _wsl_path_to_windows(path)
    if windows_path:
        return windows_path
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ArtifactRequestError("Artifact file cannot be read.", status=404) from exc
    import hashlib

    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ArtifactRequestError("Artifact changed while the publish request was running.")
    return "base64://" + base64.b64encode(content).decode("ascii")


def _wsl_path_to_windows(path: Path) -> str:
    parts = path.resolve().parts
    if len(parts) < 4 or parts[1] != "mnt" or len(parts[2]) != 1 or not parts[2].isalpha():
        return ""
    tail = "\\".join(parts[3:])
    return f"{parts[2].upper()}:\\{tail}"


def _extract_group_files(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, Mapping):
        return []
    for key in ("files", "file", "items", "data"):
        value = payload.get(key)
        nested = _extract_group_files(value)
        if nested:
            return nested
    return []


def _extract_message_id(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    for key in ("message_id", "msg_id", "id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return _extract_message_id(payload.get("data"))


def _extract_file_message_id(payload: Any, file_name: str) -> str:
    for message in reversed(_extract_history_messages(payload)):
        if _contains_text(message, file_name):
            message_id = _extract_message_id(message)
            if message_id:
                return message_id
    return ""


def _extract_history_messages(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return []
    for key in ("messages", "message", "data"):
        nested = _extract_history_messages(payload.get(key))
        if nested:
            return nested
    return []


def _contains_text(value: Any, text: str) -> bool:
    if isinstance(value, str):
        return text in value
    if isinstance(value, list):
        return any(_contains_text(item, text) for item in value)
    if isinstance(value, Mapping):
        return any(_contains_text(item, text) for item in value.values())
    return False


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str:
    value = _first_value(mapping, *keys)
    return "" if value is None else str(value)


def _first_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None
