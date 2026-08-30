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

    def load(self, group_id: int, file_name: str) -> PublicationState | None:
        path = self._state_path(group_id, file_name)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or str(payload.get("name") or "") != file_name:
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
        )

    def save(self, group_id: int, state: PublicationState) -> None:
        path = self._state_path(group_id, state.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "name": state.name,
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

    def mark_notice_sent(self, group_id: int, file_name: str, content_sha256: str) -> None:
        current = self.load(group_id, file_name)
        if current is None or current.content_sha256 != content_sha256:
            return
        self.save(
            group_id,
            PublicationState(
                name=current.name,
                content_sha256=current.content_sha256,
                reply_message_id=current.reply_message_id,
                notice_sent=True,
            ),
        )

    def _state_path(self, group_id: int, file_name: str) -> Path:
        name_key = hashlib.sha256(file_name.encode("utf-8")).hexdigest()
        return self._root / str(group_id) / f"{name_key}.json"
