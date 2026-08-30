from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import hashlib
import json
import subprocess


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_MAX_RESPONSE_BYTES = 1024 * 1024


class ArtifactPublishError(RuntimeError):
    """A safe, caller-visible publication failure without response details."""


@dataclass(frozen=True, slots=True)
class ArtifactPublishResult:
    """Counts of observable server publication actions."""

    uploaded: int
    skipped: int
    deleted: int


def publish_apk_to_group(
    path: Path,
    *,
    group_id: str,
    version: str,
    endpoint: str,
    timeout_seconds: float,
) -> ArtifactPublishResult:
    """Publish one repository-local APK through the localhost artifact service."""

    artifact = Path(path).resolve()
    if artifact.suffix.lower() != ".apk" or not artifact.is_file():
        raise ArtifactPublishError("invalid_artifact")
    target_group_id = _positive_group_id(group_id)
    publish_endpoint = _validate_endpoint(endpoint)
    repo_root = _find_git_repo_root(artifact.parent)
    if repo_root is None:
        raise ArtifactPublishError("git_repo_missing")

    branch = _git_output(repo_root, "branch", "--show-current")
    commit_hash = _git_output(repo_root, "rev-parse", "HEAD")
    commit_subject = _git_output(repo_root, "log", "-1", "--pretty=%s")
    if not branch or not commit_hash:
        raise ArtifactPublishError("git_context_missing")

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project_id": repo_root.name,
        "branch": branch,
        "commit_hash": commit_hash,
        "commit_subject": commit_subject,
        "commit_detail": f"Arcaea {version} 安装包发布",
        "files": [
            {
                "path": str(artifact),
                "name": artifact.name,
                "targets": [target_group_id],
                "sha256": _calculate_sha256(artifact),
                "message": f"Arcaea {version} 安装包已更新。",
            }
        ],
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        publish_endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_response = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        exc.close()
        raise ArtifactPublishError("http_error") from exc
    except (OSError, TimeoutError, URLError) as exc:
        raise ArtifactPublishError("connection_error") from exc
    if len(raw_response) > _MAX_RESPONSE_BYTES:
        raise ArtifactPublishError("response_too_large")
    try:
        decoded: Any = json.loads(raw_response.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactPublishError("invalid_response") from exc
    if not isinstance(decoded, dict) or decoded.get("ok") is not True:
        raise ArtifactPublishError("publication_rejected")
    uploaded = _result_count(decoded.get("uploaded"))
    skipped = _result_count(decoded.get("skipped"))
    deleted = _result_count(decoded.get("deleted"))
    if uploaded == 0 and skipped == 0:
        raise ArtifactPublishError("empty_result")
    return ArtifactPublishResult(uploaded=uploaded, skipped=skipped, deleted=deleted)


def _validate_endpoint(endpoint: str) -> str:
    text = endpoint.strip()
    parsed = urlsplit(text)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/admin/api/artifacts/publish-local"
    ):
        raise ArtifactPublishError("invalid_endpoint")
    return text


def _positive_group_id(group_id: str) -> int:
    try:
        value = int(str(group_id).strip())
    except (TypeError, ValueError) as exc:
        raise ArtifactPublishError("invalid_group") from exc
    if value <= 0:
        raise ArtifactPublishError("invalid_group")
    return value


def _find_git_repo_root(start: Path) -> Path | None:
    for candidate in (start.resolve(), *start.resolve().parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_output(repo_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArtifactPublishError("git_command_failed") from exc
    if result.returncode != 0:
        raise ArtifactPublishError("git_command_failed")
    return result.stdout.strip()


def _calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ArtifactPublishError("artifact_unreadable") from exc
    return digest.hexdigest()


def _result_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0
