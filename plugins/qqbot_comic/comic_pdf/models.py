from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import shutil


class ComicPdfError(RuntimeError):
    """Base error safe to report without upstream response details."""


class ComicDownloadError(ComicPdfError):
    """Raised when the pinned JMComic adapter cannot complete a download."""


class ComicQueueFullError(ComicPdfError):
    """Raised when the bounded comic download queue has no free slot."""


@dataclass(frozen=True, slots=True)
class ComicPdfConfig:
    """Resource, cache, and compatibility limits for comic PDF jobs."""

    enabled: bool = True
    proxy: str = ""
    timeout_seconds: int = 1800
    max_pages_per_pdf: int = 500
    max_pdf_bytes: int = 100 * 1024 * 1024
    max_concurrent_jobs: int = 2
    max_queued_jobs: int = 50
    cache_max_bytes: int = 10 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ComicChapter:
    """One ordered chapter and its downloaded page files."""

    index: int
    title: str
    pages: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class DownloadedComic:
    """Bot-owned normalized result returned by an upstream adapter."""

    album_id: str
    author: str
    title: str
    chapters: tuple[ComicChapter, ...]
    tags: tuple[str, ...] = ()

    @property
    def page_count(self) -> int:
        """Return the total number of downloaded pages."""
        return sum(len(chapter.pages) for chapter in self.chapters)


@dataclass(frozen=True, slots=True)
class ComicPdfArtifact:
    """One standard or encrypted PDF artifact."""

    path: Path
    page_count: int
    chapter_indexes: tuple[int, ...]
    size_bytes: int = 0
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class ComicCacheEntry:
    """One validated persistent plaintext comic PDF cache entry."""

    album_id: str
    author: str
    title: str
    cache_dir: Path
    artifacts: tuple[ComicPdfArtifact, ...]
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class ComicPdfDelivery:
    """Temporary encrypted artifacts owned by one private delivery."""

    album_id: str
    password: str
    task_root: Path
    artifacts: tuple[ComicPdfArtifact, ...]

    def cleanup(self) -> None:
        """Remove encrypted delivery copies without touching the cache."""
        shutil.rmtree(self.task_root, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class ComicPdfSubmission:
    """Observable state and shared result for one submitted album request."""

    album_id: str
    status: str
    queue_position: int
    _future: asyncio.Future[ComicCacheEntry]

    async def wait(self) -> ComicCacheEntry:
        """Wait for the shared cache result without cancelling its producer."""
        return await asyncio.shield(self._future)
