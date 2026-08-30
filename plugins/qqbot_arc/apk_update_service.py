from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable
from urllib.request import Request, urlopen

import asyncio
import json

from .arcaea_record_apk_downloader import ApkDownloadCancelled
from .arcaea_record_apk_downloader import ArcaeaRecordApkDownloader


ARC_VERSION_URL = "https://webapi.lowiro.com/webapi/serve/static/bin/arcaea/apk"


@dataclass(slots=True)
class ArcApkUpdateStatus:
    state: str = "idle"
    version: str = ""
    progress: str = ""
    path: Path | None = None
    error: str = ""


class ArcApkUpdateManager:
    """Own one process-local APK query/download lifecycle."""

    def __init__(
        self,
        version_fetcher: Callable[[], str],
        downloader: ArcaeaRecordApkDownloader,
        *,
        downloaded_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.version_fetcher = version_fetcher
        self.downloader = downloader
        self.downloaded_callback = downloaded_callback
        self.status = ArcApkUpdateStatus()
        self._task: asyncio.Task[None] | None = None
        self._cancel_event = Event()

    async def query_and_update(self) -> str:
        if self._task is not None and not self._task.done():
            return self.render_status()
        try:
            latest_version = await asyncio.to_thread(self.version_fetcher)
        except Exception as exc:
            self.status = ArcApkUpdateStatus(state="failed", error=type(exc).__name__)
            raise RuntimeError("Arcaea 官网版本查询失败") from exc
        existing = self._find_existing_apk(latest_version)
        if existing is not None:
            self.status = ArcApkUpdateStatus(
                state="completed",
                version=latest_version,
                progress="100%",
                path=existing,
            )
            return f"当前官网版本：{latest_version}\n安装包已经下载过：{existing.name}"
        self._cancel_event = Event()
        self.status = ArcApkUpdateStatus(
            state="downloading",
            version=latest_version,
            progress="准备下载",
        )
        self._task = asyncio.create_task(self._download(latest_version), name="qqbot-arc-apk-download")
        return f"当前官网版本：{latest_version}\n已开始下载，发送 xz 或 arcxz 可查看进度。"

    def completed_artifact(self) -> tuple[str, Path] | None:
        """Return the completed APK only while its local file still exists."""

        path = self.status.path
        if self.status.state != "completed" or path is None or not path.is_file():
            return None
        return self.status.version, path

    def render_status(self) -> str:
        if self.status.state == "downloading":
            return f"Arcaea {self.status.version} 安装包正在下载。\n当前进度：{self.status.progress}"
        if self.status.state == "completed":
            suffix = f"：{self.status.path.name}" if self.status.path is not None else "。"
            return f"Arcaea {self.status.version} 安装包已下载完毕{suffix}"
        if self.status.state == "failed":
            version = f" {self.status.version}" if self.status.version else ""
            return f"Arcaea{version} 安装包下载失败：{self.status.error}"
        if self.status.state == "cancelled":
            return f"Arcaea {self.status.version} 安装包下载已取消。"
        return "当前没有进行中的 Arcaea 安装包下载。"

    async def shutdown(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        self._cancel_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _download(self, version: str) -> None:
        def update_progress(progress: str) -> None:
            self.status.progress = progress

        try:
            result = await asyncio.to_thread(
                self.downloader.download_latest_apk,
                version,
                update_progress,
                self._cancel_event,
            )
        except ApkDownloadCancelled:
            self.status = ArcApkUpdateStatus(state="cancelled", version=version)
        except asyncio.CancelledError:
            self._cancel_event.set()
            self.status = ArcApkUpdateStatus(state="cancelled", version=version)
            raise
        except Exception as exc:
            self.status = ArcApkUpdateStatus(
                state="failed",
                version=version,
                progress=self.status.progress,
                error=type(exc).__name__,
            )
        else:
            self.status = ArcApkUpdateStatus(
                state="completed",
                version=version,
                progress="100%",
                path=result.path,
            )
            if self.downloaded_callback is not None:
                self.downloaded_callback(version)

    def _find_existing_apk(self, version: str) -> Path | None:
        if not self.downloader.target_dir.is_dir():
            return None
        normalized = version.casefold()
        matches = [
            path
            for path in self.downloader.target_dir.glob("*.apk")
            if normalized in path.name.casefold() and path.is_file() and path.stat().st_size > 0
        ]
        return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def fetch_latest_arc_version(timeout_seconds: float = 20.0) -> str:
    request = Request(ARC_VERSION_URL, headers={"User-Agent": "qqbot-arc/0.2"})
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    try:
        version = str(payload["value"]["version"]).strip()
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Arcaea 官网版本响应缺少 value.version") from exc
    if not version:
        raise RuntimeError("Arcaea 官网返回了空版本号")
    return version
