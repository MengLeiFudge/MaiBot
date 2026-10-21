from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import uuid

from .models import PublicationState


class ArtifactStateStore:
    """Persist server-owned content hashes with atomic replacement."""

    def __init__(self, runtime_root: Path) -> None:
        self._root = runtime_root.resolve() / "local_artifacts"

    @property
    def lock_path(self) -> Path:
        return self._root / ".publish.lock"

    def load(self, group_id: int, file_name: str, *, folder_id: str = "") -> PublicationState | None:
        path = self._state_path(group_id, file_name, folder_id=folder_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or str(payload.get("name") or "") != file_name:
            return None
        if payload.get("folder_id", "") != folder_id:
            return None
        content_sha256 = str(
            payload.get("server_content_sha256")
            or payload.get("content_sha256")
            or payload.get("sha256")
            or ""
        ).strip().lower()
        if len(content_sha256) != 64:
            return None
        return PublicationState(
            name=file_name,
            content_sha256=content_sha256,
            reply_message_id=str(payload.get("reply_message_id") or "").strip(),
            notice_sent=bool(payload.get("notice_sent", True)),
            folder_id=folder_id,
        )

    def save(self, group_id: int, state: PublicationState) -> None:
        path = self._state_path(group_id, state.name, folder_id=state.folder_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "name": state.name,
            "folder_id": state.folder_id,
            "server_content_sha256": state.content_sha256,
            "content_sha256": state.content_sha256,
            "reply_message_id": state.reply_message_id,
            "notice_sent": state.notice_sent,
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def mark_notice_sent(self, group_id: int, file_name: str, content_sha256: str, *, folder_id: str = "") -> None:
        current = self.load(group_id, file_name, folder_id=folder_id)
        if current is None or current.content_sha256 != content_sha256:
            return
        self.save(
            group_id,
            PublicationState(
                name=current.name,
                content_sha256=current.content_sha256,
                reply_message_id=current.reply_message_id,
                notice_sent=True,
                folder_id=folder_id,
            ),
        )

    def _state_path(self, group_id: int, file_name: str, *, folder_id: str = "") -> Path:
        """保留旧根目录缓存，并按远端文件夹 ID 的哈希隔离其他目录。"""
        name_key = hashlib.sha256(file_name.encode("utf-8")).hexdigest()
        directory = self._root / str(group_id)
        if folder_id:
            directory = directory / "folders" / hashlib.sha256(folder_id.encode("utf-8")).hexdigest()
        return directory / f"{name_key}.json"
