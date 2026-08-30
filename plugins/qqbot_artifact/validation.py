from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .models import ArtifactFile, ArtifactRequestError, PublishContext, PublishRequest


_WINDOWS_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARCHIVE_SUFFIXES = {".zip", ".apk"}


def normalize_local_path(raw_path: str | Path) -> Path:
    """Translate Windows drive paths when the plugin runs inside WSL."""

    text = str(raw_path).strip()
    if not text or "\x00" in text:
        raise ArtifactRequestError("Artifact path is required.")
    match = _WINDOWS_PATH_RE.fullmatch(text)
    if match and os.name != "nt":
        drive, tail = match.groups()
        normalized_tail = tail.replace("\\", "/")
        text = f"/mnt/{drive.lower()}/{normalized_tail}"
    return Path(text).expanduser().resolve()


def validate_publish_request(
    payload: object,
    *,
    allowed_roots: tuple[Path, ...],
    max_artifact_bytes: int,
    publish_max_age_seconds: int,
    now: datetime | None = None,
) -> PublishRequest:
    """Validate freshness, Git identity, paths and artifact metadata."""

    if not isinstance(payload, dict):
        raise ArtifactRequestError("Invalid JSON payload.")
    files_payload = payload.get("files")
    if not isinstance(files_payload, list) or not files_payload:
        raise ArtifactRequestError("No artifact files to publish.")
    _validate_timestamp(str(payload.get("timestamp") or ""), publish_max_age_seconds, now=now)
    branch = _required_text(payload, "branch", 255)
    commit_hash = _required_text(payload, "commit_hash", 64)
    commit_detail = str(payload.get("commit_detail") or "").strip()
    if len(commit_detail) > 10000:
        raise ArtifactRequestError("Publish commit detail is too long.")

    first = files_payload[0]
    if not isinstance(first, dict):
        raise ArtifactRequestError("Invalid artifact file payload.")
    first_path = normalize_local_path(str(first.get("path") or ""))
    repo_root = _find_git_repo_root(first_path.parent)
    if repo_root is None:
        raise ArtifactRequestError("Cannot infer project Git repository from artifact path.")
    _require_inside_allowed_root(repo_root, allowed_roots)
    _validate_git_context(repo_root, branch, commit_hash)

    files = tuple(
        _validate_artifact_file(item, repo_root, max_artifact_bytes=max_artifact_bytes)
        for item in files_payload
    )
    if not commit_detail and not any(item.message for item in files):
        raise ArtifactRequestError("Publish commit detail or file message is required.")
    return PublishRequest(
        files=files,
        context=PublishContext(
            project_id=str(payload.get("project_id") or "").strip()[:255],
            branch=branch,
            commit_hash=commit_hash,
            commit_subject=str(payload.get("commit_subject") or "").strip()[:1000],
            commit_detail=commit_detail,
        ),
    )


def normalize_allowed_roots(values: list[str]) -> tuple[Path, ...]:
    roots = tuple(normalize_local_path(value) for value in values if str(value).strip())
    if not roots:
        raise ValueError("publishing.allowed_roots 至少需要一个路径")
    return roots


def _validate_artifact_file(payload: Any, repo_root: Path, *, max_artifact_bytes: int) -> ArtifactFile:
    if not isinstance(payload, dict):
        raise ArtifactRequestError("Invalid artifact file payload.")
    path = normalize_local_path(str(payload.get("path") or ""))
    suffix = path.suffix.lower()
    if suffix not in _ARCHIVE_SUFFIXES:
        raise ArtifactRequestError("Only zip and APK artifacts can be uploaded.")
    if not path.is_file():
        raise ArtifactRequestError("Artifact file does not exist.", status=404)
    _require_relative_to(path, repo_root, "Artifact must be inside project repository.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ArtifactRequestError("Artifact file cannot be read.", status=404) from exc
    if size <= 0 or size > max_artifact_bytes:
        raise ArtifactRequestError("Artifact file size is outside the configured limit.")

    raw_name = str(payload.get("name") or "").strip() or path.name
    name = Path(raw_name.replace("\\", "/")).name
    name_suffix = Path(name).suffix.lower()
    if name != raw_name or name_suffix not in _ARCHIVE_SUFFIXES or name_suffix != suffix or len(name) > 255:
        raise ArtifactRequestError("Invalid artifact upload name.")
    targets = _positive_group_ids(payload.get("targets"))
    expected_sha256 = _optional_sha256(payload.get("sha256"), "sha256")
    content_sha256 = _optional_sha256(payload.get("content_sha256"), "content_sha256")
    message = str(payload.get("message") or "").strip()
    if len(message) > 4000:
        raise ArtifactRequestError("Artifact message is too long.")
    return ArtifactFile(
        path=path,
        name=name,
        targets=targets,
        sha256=expected_sha256,
        content_sha256=content_sha256,
        message=message,
    )


def _validate_timestamp(text: str, max_age_seconds: int, *, now: datetime | None) -> None:
    if not text.strip():
        raise ArtifactRequestError("Publish timestamp is required.")
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        published_at = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ArtifactRequestError("Invalid publish timestamp.") from exc
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = abs((current - published_at.astimezone(timezone.utc)).total_seconds())
    if age > max_age_seconds:
        raise ArtifactRequestError("Publish request timestamp is stale.")


def _validate_git_context(repo_root: Path, branch: str, commit_hash: str) -> None:
    current_branch = _read_git_output(repo_root, "branch", "--show-current")
    if not current_branch or current_branch != branch:
        raise ArtifactRequestError("Publish branch does not match project checkout.")
    current_commit = _read_git_output(repo_root, "rev-parse", "HEAD").lower()
    if not current_commit or not current_commit.startswith(commit_hash.lower()):
        raise ArtifactRequestError("Publish commit does not match project checkout.")


def _read_git_output(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _find_git_repo_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _require_inside_allowed_root(path: Path, roots: tuple[Path, ...]) -> None:
    if not any(_is_relative_to(path, root) for root in roots):
        raise ArtifactRequestError("Project repository is outside configured roots.")


def _require_relative_to(path: Path, parent: Path, detail: str) -> None:
    if not _is_relative_to(path, parent):
        raise ArtifactRequestError(detail)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _positive_group_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ArtifactRequestError("Artifact targets must be an array of group ids.")
    targets: list[int] = []
    for raw_group_id in value:
        if isinstance(raw_group_id, bool):
            raise ArtifactRequestError("Artifact targets must be positive integer group ids.")
        try:
            group_id = int(raw_group_id)
        except (TypeError, ValueError) as exc:
            raise ArtifactRequestError("Artifact targets must be positive integer group ids.") from exc
        if group_id <= 0:
            raise ArtifactRequestError("Artifact targets must be positive integer group ids.")
        if group_id not in targets:
            targets.append(group_id)
    if not targets:
        raise ArtifactRequestError("Artifact targets must include at least one group id.")
    return tuple(targets)


def _optional_sha256(value: Any, field_name: str) -> str:
    text = str(value or "").strip().lower()
    if text and not _SHA256_RE.fullmatch(text):
        raise ArtifactRequestError(f"Artifact {field_name} must be a SHA-256 hex digest.")
    return text


def _required_text(payload: dict[str, Any], key: str, max_length: int) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ArtifactRequestError(f"Publish {key.replace('_', ' ')} is required.")
    if len(value) > max_length:
        raise ArtifactRequestError(f"Publish {key.replace('_', ' ')} is too long.")
    return value
