from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    """One validated zip and its target groups."""

    path: Path
    name: str
    targets: tuple[int, ...]
    sha256: str = ""
    content_sha256: str = ""
    message: str = ""
    # 空字符串表示群文件根目录，其余值为 QQ 返回的文件夹 ID。
    folder_id: str = ""


@dataclass(frozen=True, slots=True)
class PublishContext:
    """Git metadata attached to one publication request."""

    project_id: str
    branch: str
    commit_hash: str
    commit_subject: str = ""
    commit_detail: str = ""


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """Validated publication request."""

    files: tuple[ArtifactFile, ...]
    context: PublishContext


@dataclass(slots=True)
class PublishResult:
    """Observable publication actions returned by the HTTP endpoint."""

    uploaded: list[dict[str, object]] = field(default_factory=list)
    deleted: list[dict[str, object]] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PublicationState:
    """Last server-verified content state for one group and upload name."""

    name: str
    content_sha256: str
    reply_message_id: str = ""
    notice_sent: bool = True
    # 与群号和文件名共同确定发布状态的归属目录。
    folder_id: str = ""


class ArtifactRequestError(ValueError):
    """Client-visible request validation error."""

    def __init__(self, detail: str, *, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
