from __future__ import annotations

import importlib
from pathlib import Path
import re
from types import ModuleType

from .models import ComicChapter, ComicDownloadError, DownloadedComic


SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
PINNED_JMCOMIC_VERSION = "2.7.2"


class JmcomicAdapter:
    """Isolate the bot from JMComic's changing option and entity contracts."""

    def __init__(self, *, proxy: str = "", module: ModuleType | None = None) -> None:
        self.proxy = str(proxy or "").strip()
        self._module = module

    async def download_album(self, album_id: str, task_root: Path) -> DownloadedComic:
        """Download and normalize one album under the caller-owned task root."""
        normalized_id = str(album_id or "").strip()
        if not normalized_id.isdigit():
            raise ComicDownloadError("JM 作品 ID 必须是纯数字。")
        root = Path(task_root).resolve()
        root.mkdir(parents=True, exist_ok=False)
        module = self._load_module()
        option = self._build_option(module, root)
        try:
            result = await module.download_album_async(normalized_id, option=option)
            album = getattr(result, "detail", None)
            if album is None and isinstance(result, tuple) and result:
                album = result[0]
            if album is None:
                raise ComicDownloadError("JM 下载结果不兼容当前适配器。")
            return self._normalize_album(normalized_id, album, option, root)
        except ComicDownloadError:
            raise
        except Exception as exc:
            raise ComicDownloadError("JM 下载失败，请稍后重试或检查代理配置。") from exc

    def _load_module(self) -> ModuleType:
        module = self._module
        if module is None:
            try:
                module = importlib.import_module("jmcomic")
            except ImportError as exc:
                raise ComicDownloadError("JM 下载组件未安装，请在 MaiBot 运行环境安装锁定依赖。") from exc
        if str(getattr(module, "__version__", "")) != PINNED_JMCOMIC_VERSION:
            raise ComicDownloadError("JM 下载组件版本不兼容，请安装锁定版本。")
        if not callable(getattr(module, "download_album_async", None)):
            raise ComicDownloadError("JM 下载组件缺少必需接口，请安装锁定版本。")
        option_type = getattr(module, "JmOption", None)
        if option_type is None or not callable(getattr(option_type, "construct", None)):
            raise ComicDownloadError("JM 配置接口不兼容，请安装锁定版本。")
        return module

    def _build_option(self, module: ModuleType, root: Path):
        proxies: object = {}
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}
        return module.JmOption.construct(
            {
                "log": False,
                "dir_rule": {
                    "rule": "Bd_Aid_Pindex",
                    "base_dir": str(root),
                    "normalize_zh": None,
                },
                "download": {
                    "cache": True,
                    "image": {"decode": True, "suffix": None},
                    "threading": {"image": 8, "photo": 2},
                },
                "client": {
                    "cache": None,
                    "domain": [],
                    "postman": {
                        "type": "curl_cffi",
                        "meta_data": {
                            "impersonate": "chrome",
                            "headers": None,
                            "proxies": proxies,
                        },
                    },
                    "impl": "api",
                    "async_impl": "async_api",
                    "retry_times": 3,
                },
                "plugins": {"valid": "raise"},
            }
        )

    def _normalize_album(self, album_id: str, album, option, root: Path) -> DownloadedComic:
        chapters: list[ComicChapter] = []
        for fallback_index, photo in enumerate(album, start=1):
            index = _positive_int(getattr(photo, "album_index", None), fallback_index)
            chapter_dir = Path(option.decide_image_save_dir(photo, ensure_exists=False)).resolve()
            _require_under_root(chapter_dir, root)
            pages = tuple(
                sorted(
                    (
                        path.resolve()
                        for path in chapter_dir.iterdir()
                        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
                    ),
                    key=_natural_path_key,
                )
            ) if chapter_dir.is_dir() else ()
            for page in pages:
                _require_under_root(page, root)
            if not pages:
                raise ComicDownloadError("JM 下载结果缺少可用图片。")
            chapters.append(
                ComicChapter(
                    index=index,
                    title=str(getattr(photo, "title", "") or f"第{index}章").strip(),
                    pages=pages,
                )
            )
        if not chapters:
            raise ComicDownloadError("JM 下载结果没有章节。")
        chapters.sort(key=lambda chapter: chapter.index)
        author = str(getattr(album, "author", "") or "未知作者").strip()
        title = str(getattr(album, "oname", "") or getattr(album, "title", "") or f"JM{album_id}").strip()
        tags = _normalize_tags(getattr(album, "tags", ()))
        return DownloadedComic(
            album_id=album_id,
            author=author,
            title=title,
            chapters=tuple(chapters),
            tags=tags,
        )


def _normalize_tags(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = re.split(r"[,，/|]", value)
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = ()
    return tuple(
        dict.fromkeys(
            text
            for item in candidates
            if (text := str(item or "").strip())
        )
    )


def _positive_int(value: object, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _natural_path_key(path: Path) -> tuple[tuple[int, object], ...]:
    parts = re.split(r"(\d+)", path.stem.casefold())
    tokens = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in parts
    )
    return tokens + ((1, path.suffix.casefold()),)


def _require_under_root(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ComicDownloadError("JM 下载产物路径越界，任务已终止。") from exc
