"""Stable bot-owned contracts for cached JMComic PDF delivery."""

from .adapter import JmcomicAdapter
from .cache import ComicPdfCache
from .config import load_comic_pdf_config
from .encryption import PdfEncryptor
from .friend_route import (
    ComicFriendRouteCoordinator,
    ComicFriendRouteDecision,
    is_onebot_friend,
)
from .models import (
    ComicCacheEntry,
    ComicChapter,
    ComicDownloadError,
    ComicPdfArtifact,
    ComicPdfConfig,
    ComicPdfDelivery,
    ComicPdfError,
    ComicPdfSubmission,
    ComicQueueFullError,
)
from .renderer import PdfRenderer
from .sender import send_private_pdfs_with_password
from .service import ComicPdfService

__all__ = [
    "ComicCacheEntry",
    "ComicChapter",
    "ComicDownloadError",
    "ComicFriendRouteCoordinator",
    "ComicFriendRouteDecision",
    "ComicPdfArtifact",
    "ComicPdfCache",
    "ComicPdfConfig",
    "ComicPdfDelivery",
    "ComicPdfError",
    "ComicPdfService",
    "ComicPdfSubmission",
    "ComicQueueFullError",
    "JmcomicAdapter",
    "PdfEncryptor",
    "PdfRenderer",
    "load_comic_pdf_config",
    "is_onebot_friend",
    "send_private_pdfs_with_password",
]
