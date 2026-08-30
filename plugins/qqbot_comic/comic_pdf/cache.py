from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any
import uuid

from .models import ComicCacheEntry, ComicPdfArtifact, ComicPdfError, DownloadedComic
from .renderer import PdfRenderer


CACHE_SCHEMA_VERSION = 1
CACHE_RENDERER_VERSION = 1
MANIFEST_FILE_NAME = "metadata.json"
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class ComicPdfCache:
    """Store and validate persistent plaintext comic PDFs with bounded LRU."""

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        self.root = Path(root).resolve()
        self.max_bytes = max(1, int(max_bytes))

    def lookup(self, album_id: str) -> ComicCacheEntry | None:
        """Return one hash-validated cache entry and update its access time."""
        normalized_id = _normalize_album_id(album_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_work_dirs()
        for candidate in sorted(self.root.glob(f"JM{normalized_id}-*")):
            if not candidate.is_dir() or candidate.name.startswith("."):
                continue
            try:
                entry, manifest = self._validate(candidate, normalized_id)
            except (ComicPdfError, OSError, ValueError, json.JSONDecodeError):
                self._remove_cache_dir(candidate)
                continue
            manifest["last_accessed_at"] = _utc_now()
            _write_json_atomic(candidate / MANIFEST_FILE_NAME, manifest)
            return entry
        return None

    def store(
        self,
        comic: DownloadedComic,
        renderer: PdfRenderer,
    ) -> ComicCacheEntry:
        """Render and atomically publish one complete plaintext cache entry."""
        album_id = _normalize_album_id(comic.album_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_work_dirs()
        folder_name = _cache_base_name(album_id, comic.author, comic.title)
        target = _require_under_root(self.root / folder_name, self.root)
        staging = _require_under_root(
            self.root / f".{folder_name}.staging-{uuid.uuid4().hex}",
            self.root,
        )
        staging.mkdir(parents=False, exist_ok=False)
        manifest: dict[str, Any] = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "renderer_version": CACHE_RENDERER_VERSION,
            "jmcomic_version": "2.7.2",
            "album_id": album_id,
            "author": comic.author,
            "title": comic.title,
            "tags": list(comic.tags),
            "folder_name": folder_name,
            "download_complete": False,
            "created_at": _utc_now(),
            "last_accessed_at": _utc_now(),
            "page_count": comic.page_count,
            "total_size_bytes": 0,
            "artifacts": [],
        }
        _write_json_atomic(staging / MANIFEST_FILE_NAME, manifest)
        try:
            rendered = renderer.render(comic, staging)
            renamed = self._rename_artifacts(rendered, staging, folder_name)
            artifact_records = []
            total_size = 0
            for artifact in renamed:
                size = artifact.path.stat().st_size
                digest = _sha256_file(artifact.path)
                total_size += size
                artifact_records.append(
                    {
                        "file_name": artifact.path.name,
                        "page_count": artifact.page_count,
                        "chapter_indexes": list(artifact.chapter_indexes),
                        "size_bytes": size,
                        "sha256": digest,
                    }
                )
            manifest.update(
                {
                    "download_complete": True,
                    "total_size_bytes": total_size,
                    "artifacts": artifact_records,
                }
            )
            _write_json_atomic(staging / MANIFEST_FILE_NAME, manifest)
            self._publish(staging, target, album_id)
            entry, _ = self._validate(target, album_id)
            self.enforce_limit(exclude=target)
            return entry
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def enforce_limit(self, *, exclude: Path | None = None) -> None:
        """Evict least-recently-used complete entries until under the limit."""
        if not self.root.is_dir():
            return
        excluded = Path(exclude).resolve() if exclude is not None else None
        entries: list[tuple[str, int, Path]] = []
        total = 0
        for candidate in self.root.iterdir():
            if not candidate.is_dir() or candidate.name.startswith("."):
                continue
            manifest_path = candidate / MANIFEST_FILE_NAME
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("download_complete") is not True:
                    continue
                size = int(manifest.get("total_size_bytes") or 0)
                accessed = str(manifest.get("last_accessed_at") or "")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            total += max(0, size)
            entries.append((accessed, max(0, size), candidate.resolve()))
        for _, size, candidate in sorted(entries, key=lambda item: item[0]):
            if total <= self.max_bytes:
                break
            if excluded is not None and candidate == excluded:
                continue
            self._remove_cache_dir(candidate)
            total -= size

    def _validate(
        self,
        cache_dir: Path,
        album_id: str,
    ) -> tuple[ComicCacheEntry, dict[str, Any]]:
        directory = _require_under_root(cache_dir.resolve(), self.root)
        manifest = json.loads((directory / MANIFEST_FILE_NAME).read_text(encoding="utf-8"))
        if manifest.get("schema_version") != CACHE_SCHEMA_VERSION:
            raise ComicPdfError("JM 缓存版本不兼容。")
        if manifest.get("download_complete") is not True:
            raise ComicPdfError("JM 缓存尚未完成。")
        if str(manifest.get("album_id") or "") != album_id:
            raise ComicPdfError("JM 缓存作品 ID 不匹配。")
        records = manifest.get("artifacts")
        if not isinstance(records, list) or not records:
            raise ComicPdfError("JM 缓存没有 PDF 文件。")
        artifacts: list[ComicPdfArtifact] = []
        total_size = 0
        total_pages = 0
        for record in records:
            if not isinstance(record, dict):
                raise ComicPdfError("JM 缓存文件记录无效。")
            file_name = str(record.get("file_name") or "")
            if Path(file_name).name != file_name or not file_name.lower().endswith(".pdf"):
                raise ComicPdfError("JM 缓存文件名无效。")
            path = _require_under_root((directory / file_name).resolve(), directory)
            size = int(record.get("size_bytes") or 0)
            page_count = int(record.get("page_count") or 0)
            digest = str(record.get("sha256") or "").lower()
            if not path.is_file() or size <= 0 or path.stat().st_size != size:
                raise ComicPdfError("JM 缓存文件大小不匹配。")
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or _sha256_file(path) != digest:
                raise ComicPdfError("JM 缓存文件校验失败。")
            chapter_indexes = tuple(int(value) for value in record.get("chapter_indexes") or [])
            artifacts.append(
                ComicPdfArtifact(path, page_count, chapter_indexes, size, digest)
            )
            total_size += size
            total_pages += page_count
        if total_size != int(manifest.get("total_size_bytes") or -1):
            raise ComicPdfError("JM 缓存总大小不匹配。")
        if total_pages != int(manifest.get("page_count") or -1):
            raise ComicPdfError("JM 缓存总页数不匹配。")
        return (
            ComicCacheEntry(
                album_id=album_id,
                author=str(manifest.get("author") or "未知作者"),
                title=str(manifest.get("title") or f"JM{album_id}"),
                cache_dir=directory,
                artifacts=tuple(artifacts),
                tags=_manifest_tags(manifest.get("tags")),
            ),
            manifest,
        )

    def _rename_artifacts(
        self,
        artifacts: tuple[ComicPdfArtifact, ...],
        staging: Path,
        base_name: str,
    ) -> tuple[ComicPdfArtifact, ...]:
        renamed: list[ComicPdfArtifact] = []
        multiple = len(artifacts) > 1
        for index, artifact in enumerate(artifacts, start=1):
            suffix = f"({index})" if multiple else ""
            target = _require_under_root(staging / f"{base_name}{suffix}.pdf", staging)
            artifact.path.replace(target)
            renamed.append(
                ComicPdfArtifact(target, artifact.page_count, artifact.chapter_indexes)
            )
        return tuple(renamed)

    def _publish(self, staging: Path, target: Path, album_id: str) -> None:
        old_dirs = [
            path
            for path in self.root.glob(f"JM{album_id}-*")
            if path.is_dir() and not path.name.startswith(".")
        ]
        backups: list[Path] = []
        try:
            for old in old_dirs:
                backup = self.root / f".{old.name}.old-{uuid.uuid4().hex}"
                old.replace(backup)
                backups.append(backup)
            staging.replace(target)
        except BaseException:
            for backup in backups:
                original_name = backup.name.split(".old-", 1)[0].lstrip(".")
                original = self.root / original_name
                if not original.exists():
                    backup.replace(original)
            raise
        finally:
            for backup in backups:
                shutil.rmtree(backup, ignore_errors=True)

    def _cleanup_stale_work_dirs(self) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - 24 * 60 * 60
        for candidate in self.root.glob(".*"):
            try:
                if candidate.is_dir() and candidate.stat().st_mtime < cutoff:
                    self._remove_cache_dir(candidate)
            except OSError:
                continue

    def _remove_cache_dir(self, path: Path) -> None:
        directory = _require_under_root(Path(path).resolve(), self.root)
        if directory != self.root:
            shutil.rmtree(directory, ignore_errors=True)


def _manifest_tags(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        dict.fromkeys(
            text
            for item in value
            if (text := str(item or "").strip())
        )
    )


def _cache_base_name(album_id: str, author: str, title: str) -> str:
    safe_author = _safe_component(author, fallback="未知作者", max_length=36)
    safe_title = _safe_component(title, fallback=f"JM{album_id}", max_length=72)
    return f"JM{album_id}-【{safe_author}】{safe_title}"


def _safe_component(value: str, *, fallback: str, max_length: int) -> str:
    normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value or "").strip())
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = normalized[:max_length].rstrip(" .") or fallback
    if normalized.upper() in _WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    return normalized


def _normalize_album_id(value: str) -> str:
    album_id = str(value or "").strip()
    if not album_id.isdigit():
        raise ComicPdfError("JM 作品 ID 必须是纯数字。")
    return album_id


def _require_under_root(path: Path, root: Path) -> Path:
    resolved_root = Path(root).resolve()
    resolved_path = Path(path).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ComicPdfError("JM 缓存路径越界。") from exc
    return resolved_path


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.part-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
