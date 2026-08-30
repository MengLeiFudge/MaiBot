from __future__ import annotations

import importlib
from pathlib import Path
import re
from types import ModuleType

from .models import ComicChapter, ComicPdfArtifact, ComicPdfError, DownloadedComic


class PdfRenderer:
    """Render ordered image pages through a replaceable img2pdf backend."""

    def __init__(
        self,
        *,
        max_pages: int,
        max_bytes: int,
        module: ModuleType | None = None,
    ) -> None:
        self.max_pages = max(1, int(max_pages))
        self.max_bytes = max(1024, int(max_bytes))
        self._module = module

    def render(self, comic: DownloadedComic, output_dir: Path) -> tuple[ComicPdfArtifact, ...]:
        """Render one whole-book PDF or bounded chapter/page parts."""
        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if comic.page_count <= self.max_pages:
            whole = self._render_pages(
                pages=tuple(page for chapter in comic.chapters for page in chapter.pages),
                output=destination / f"JM{comic.album_id}-{_safe_name(comic.title)}.pdf",
                chapter_indexes=tuple(chapter.index for chapter in comic.chapters),
            )
            if whole.path.stat().st_size <= self.max_bytes:
                return (whole,)
            whole.path.unlink(missing_ok=True)
        artifacts: list[ComicPdfArtifact] = []
        for chapter in comic.chapters:
            artifacts.extend(self._render_chapter(comic.album_id, chapter, destination))
        return tuple(artifacts)

    def _render_chapter(
        self,
        album_id: str,
        chapter: ComicChapter,
        output_dir: Path,
    ) -> list[ComicPdfArtifact]:
        pages = chapter.pages
        chunks = [pages[index : index + self.max_pages] for index in range(0, len(pages), self.max_pages)]
        artifacts: list[ComicPdfArtifact] = []
        for chunk_index, chunk in enumerate(chunks, start=1):
            suffix = f"-part{chunk_index}" if len(chunks) > 1 else ""
            output = output_dir / (
                f"JM{album_id}-chapter{chapter.index:03d}-{_safe_name(chapter.title)}{suffix}.pdf"
            )
            artifacts.extend(
                self._render_bounded_chunk(
                    pages=chunk,
                    output=output,
                    chapter_index=chapter.index,
                )
            )
        return artifacts

    def _render_bounded_chunk(
        self,
        *,
        pages: tuple[Path, ...],
        output: Path,
        chapter_index: int,
    ) -> list[ComicPdfArtifact]:
        artifact = self._render_pages(
            pages=pages,
            output=output,
            chapter_indexes=(chapter_index,),
        )
        if artifact.path.stat().st_size <= self.max_bytes:
            return [artifact]
        artifact.path.unlink(missing_ok=True)
        if len(pages) <= 1:
            raise ComicPdfError("单页 PDF 已超过文件大小限制，无法发送。")
        midpoint = len(pages) // 2
        results: list[ComicPdfArtifact] = []
        for part, subset in enumerate((pages[:midpoint], pages[midpoint:]), start=1):
            child = output.with_name(f"{output.stem}-split{part}{output.suffix}")
            results.extend(
                self._render_bounded_chunk(
                    pages=subset,
                    output=child,
                    chapter_index=chapter_index,
                )
            )
        return results

    def _render_pages(
        self,
        *,
        pages: tuple[Path, ...],
        output: Path,
        chapter_indexes: tuple[int, ...],
    ) -> ComicPdfArtifact:
        if not pages:
            raise ComicPdfError("没有可用于生成 PDF 的图片。")
        backend = self._load_backend()
        temporary = output.with_suffix(output.suffix + ".part")
        temporary.unlink(missing_ok=True)
        try:
            with temporary.open("wb") as stream:
                backend.convert([str(page) for page in pages], outputstream=stream)
            if temporary.stat().st_size <= 0:
                raise ComicPdfError("PDF 生成结果为空。")
            temporary.replace(output)
        except ComicPdfError:
            raise
        except Exception as exc:
            raise ComicPdfError("图片转换 PDF 失败。") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return ComicPdfArtifact(
            path=output,
            page_count=len(pages),
            chapter_indexes=chapter_indexes,
        )

    def _load_backend(self) -> ModuleType:
        if self._module is not None:
            return self._module
        try:
            return importlib.import_module("img2pdf")
        except ImportError as exc:
            raise ComicPdfError("PDF 组件未安装，请在 MaiBot 运行环境安装锁定依赖。") from exc


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", str(value or "").strip())
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    return (normalized or "comic")[:80]
