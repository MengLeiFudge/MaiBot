from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
import secrets
from types import ModuleType
import uuid

from .models import ComicCacheEntry, ComicPdfArtifact, ComicPdfDelivery, ComicPdfError


PINNED_PIKEPDF_VERSION = "10.11.0"


class PdfEncryptor:
    """Create request-owned AES-encrypted copies of cached plaintext PDFs."""

    def __init__(self, temp_root: Path, *, module: ModuleType | None = None) -> None:
        self.temp_root = Path(temp_root).resolve()
        self._module = module

    def create_delivery(self, entry: ComicCacheEntry) -> ComicPdfDelivery:
        """Encrypt every cached PDF with the complete numeric JMID password."""
        password = str(entry.album_id)
        if not password.isdigit():
            raise ComicPdfError("JM PDF 密码无法由作品 ID 生成。")
        module = self._load_module()
        self.temp_root.mkdir(parents=True, exist_ok=True)
        task_root = (self.temp_root / f"jm{entry.album_id}-delivery-{uuid.uuid4().hex}").resolve()
        _require_under_root(task_root, self.temp_root)
        task_root.mkdir(parents=False, exist_ok=False)
        artifacts: list[ComicPdfArtifact] = []
        try:
            for source in entry.artifacts:
                destination = (task_root / source.path.name).resolve()
                _require_under_root(destination, task_root)
                temporary = destination.with_suffix(destination.suffix + ".part")
                with module.open(source.path) as pdf:
                    pdf.save(
                        temporary,
                        encryption=module.Encryption(
                            owner=secrets.token_urlsafe(32),
                            user=password,
                            R=6,
                            aes=True,
                            metadata=True,
                        ),
                    )
                if not temporary.is_file() or temporary.stat().st_size <= 0:
                    raise ComicPdfError("JM PDF 加密结果为空。")
                temporary.replace(destination)
                size = destination.stat().st_size
                artifacts.append(
                    ComicPdfArtifact(
                        path=destination,
                        page_count=source.page_count,
                        chapter_indexes=source.chapter_indexes,
                        size_bytes=size,
                        sha256=_sha256_file(destination),
                    )
                )
            return ComicPdfDelivery(
                album_id=entry.album_id,
                password=password,
                task_root=task_root,
                artifacts=tuple(artifacts),
            )
        except BaseException:
            ComicPdfDelivery(entry.album_id, password, task_root, ()).cleanup()
            raise

    def _load_module(self) -> ModuleType:
        module = self._module
        if module is None:
            try:
                module = importlib.import_module("pikepdf")
            except ImportError as exc:
                raise ComicPdfError("PDF 加密组件未安装，请在 MaiBot 运行环境安装锁定依赖。") from exc
        if str(getattr(module, "__version__", "")) != PINNED_PIKEPDF_VERSION:
            raise ComicPdfError("PDF 加密组件版本不兼容，请安装锁定版本。")
        if not callable(getattr(module, "open", None)) or not callable(getattr(module, "Encryption", None)):
            raise ComicPdfError("PDF 加密组件接口不兼容，请安装锁定版本。")
        return module


def _require_under_root(path: Path, root: Path) -> None:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError as exc:
        raise ComicPdfError("JM 加密产物路径越界。") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
