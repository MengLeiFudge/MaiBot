from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import shutil
import uuid

from .adapter import JmcomicAdapter
from .cache import ComicPdfCache
from .encryption import PdfEncryptor
from .models import (
    ComicCacheEntry,
    ComicDownloadError,
    ComicPdfConfig,
    ComicPdfDelivery,
    ComicPdfError,
    ComicPdfSubmission,
    ComicQueueFullError,
)
from .renderer import PdfRenderer


@dataclass(frozen=True, slots=True)
class _PendingDownload:
    album_id: str
    future: asyncio.Future[ComicCacheEntry]


class ComicPdfService:
    """Coordinate cached, single-flight, FIFO-bounded comic PDF downloads."""

    def __init__(
        self,
        temp_root: Path,
        config: ComicPdfConfig | None = None,
        *,
        cache_root: Path | None = None,
        adapter_factory: Callable[[], JmcomicAdapter] | None = None,
        renderer_factory: Callable[[], PdfRenderer] | None = None,
        cache: ComicPdfCache | None = None,
        encryptor: PdfEncryptor | None = None,
    ) -> None:
        self.temp_root = Path(temp_root).resolve()
        self.cache_root = Path(cache_root or (self.temp_root / "cache")).resolve()
        self.config = config or ComicPdfConfig()
        self._adapter_factory = adapter_factory or (
            lambda: JmcomicAdapter(proxy=self.config.proxy)
        )
        self._renderer_factory = renderer_factory or (
            lambda: PdfRenderer(
                max_pages=self.config.max_pages_per_pdf,
                max_bytes=self.config.max_pdf_bytes,
            )
        )
        self._cache = cache or ComicPdfCache(
            self.cache_root,
            max_bytes=self.config.cache_max_bytes,
        )
        self._encryptor = encryptor or PdfEncryptor(self.temp_root)
        self._lock = asyncio.Lock()
        self._active_count = 0
        self._queue: deque[_PendingDownload] = deque()
        self._inflight: dict[str, asyncio.Future[ComicCacheEntry]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def submit(self, album_id: str, *, force_refresh: bool = False) -> ComicPdfSubmission:
        """Submit one album and report cache, shared-task, start, or queue state."""
        normalized_id = _normalize_album_id(album_id)
        if self._closed:
            raise ComicPdfError("JM 下载服务正在停止，请稍后重试。")
        async with self._lock:
            shared = self._inflight.get(normalized_id)
            if shared is not None:
                return ComicPdfSubmission(
                    normalized_id,
                    "shared",
                    self._queue_position(normalized_id),
                    shared,
                )

        if not force_refresh:
            cached = await asyncio.to_thread(self._cache.lookup, normalized_id)
            if cached is not None:
                future = asyncio.get_running_loop().create_future()
                future.set_result(cached)
                return ComicPdfSubmission(normalized_id, "cache_hit", 0, future)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ComicCacheEntry] = loop.create_future()
        future.add_done_callback(self._consume_future_exception)
        pending = _PendingDownload(normalized_id, future)
        async with self._lock:
            shared = self._inflight.get(normalized_id)
            if shared is not None:
                return ComicPdfSubmission(
                    normalized_id,
                    "shared",
                    self._queue_position(normalized_id),
                    shared,
                )
            self._inflight[normalized_id] = future
            if self._active_count < max(1, self.config.max_concurrent_jobs):
                self._active_count += 1
                self._start_download(pending)
                return ComicPdfSubmission(normalized_id, "started", 0, future)
            if len(self._queue) >= max(0, self.config.max_queued_jobs):
                self._inflight.pop(normalized_id, None)
                raise ComicQueueFullError("JM 下载队列已满，请稍后再试。")
            self._queue.append(pending)
            return ComicPdfSubmission(normalized_id, "queued", len(self._queue), future)

    async def create_delivery(self, entry: ComicCacheEntry) -> ComicPdfDelivery:
        """Create temporary encrypted copies for one private-file delivery."""
        return await asyncio.to_thread(self._encryptor.create_delivery, entry)

    async def shutdown(self) -> None:
        """Cancel owned producers and fail queued requests during plugin unload."""
        async with self._lock:
            self._closed = True
            queued = tuple(self._queue)
            self._queue.clear()
            tasks = tuple(self._tasks)
            for pending in queued:
                self._inflight.pop(pending.album_id, None)
                if not pending.future.done():
                    pending.future.set_exception(
                        ComicPdfError("JM 下载服务已停止，排队任务已取消。")
                    )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _queue_position(self, album_id: str) -> int:
        """Return the current one-based FIFO position, or zero for an active task."""
        return next(
            (
                position
                for position, pending in enumerate(self._queue, start=1)
                if pending.album_id == album_id
            ),
            0,
        )

    def _start_download(self, pending: _PendingDownload) -> None:
        task = asyncio.create_task(
            self._run_download(pending),
            name=f"jmcomic-download-{pending.album_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._consume_task_exception)

    async def _run_download(self, pending: _PendingDownload) -> None:
        task_root = self.temp_root / f"jm{pending.album_id}-build-{uuid.uuid4().hex}"
        try:
            task_root.mkdir(parents=True, exist_ok=False)
            comic = await asyncio.wait_for(
                self._adapter_factory().download_album(
                    pending.album_id,
                    task_root / "download",
                ),
                timeout=max(1, self.config.timeout_seconds),
            )
            entry = await asyncio.to_thread(
                self._cache.store,
                comic,
                self._renderer_factory(),
            )
            if not pending.future.done():
                pending.future.set_result(entry)
        except asyncio.CancelledError:
            if not pending.future.done():
                pending.future.set_exception(
                    ComicPdfError("JM 下载服务已停止，任务已取消。")
                )
            raise
        except TimeoutError:
            if not pending.future.done():
                pending.future.set_exception(
                    ComicDownloadError("JM 下载超时，未写入缓存。")
                )
        except ComicPdfError as exc:
            if not pending.future.done():
                pending.future.set_exception(exc)
        except BaseException as exc:
            if not pending.future.done():
                pending.future.set_exception(
                    ComicPdfError("JM 下载或缓存生成失败。")
                )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            shutil.rmtree(task_root, ignore_errors=True)
            await self._finish_download(pending.album_id)

    async def _finish_download(self, album_id: str) -> None:
        next_pending: _PendingDownload | None = None
        async with self._lock:
            self._inflight.pop(album_id, None)
            self._active_count = max(0, self._active_count - 1)
            if self._queue and not self._closed:
                next_pending = self._queue.popleft()
                self._active_count += 1
        if next_pending is not None:
            self._start_download(next_pending)

    @staticmethod
    def _consume_future_exception(future: asyncio.Future[ComicCacheEntry]) -> None:
        if future.cancelled():
            return
        try:
            future.exception()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            return

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            return
        except BaseException:
            return


def _normalize_album_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.isdigit():
        raise ComicDownloadError("JM 作品 ID 必须是纯数字。")
    return normalized
