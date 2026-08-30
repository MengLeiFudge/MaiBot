from __future__ import annotations

from .models import ComicPdfConfig


def load_comic_pdf_config(config=None) -> ComicPdfConfig:
    """Load bounded JMComic settings from the shared plugin configuration."""
    return ComicPdfConfig(
        enabled=_read_bool(_get(config, "jmcomic_enabled", True), True),
        proxy=str(_get(config, "jmcomic_proxy", "") or "").strip(),
        timeout_seconds=_clamp_int(
            _get(config, "jmcomic_timeout_seconds", 1800),
            default=1800,
            minimum=60,
            maximum=7200,
        ),
        max_pages_per_pdf=_clamp_int(
            _get(config, "jmcomic_max_pages_per_pdf", 500),
            default=500,
            minimum=10,
            maximum=1000,
        ),
        max_pdf_bytes=_clamp_int(
            _get(config, "jmcomic_max_pdf_size_mb", 100),
            default=100,
            minimum=10,
            maximum=500,
        )
        * 1024
        * 1024,
        max_concurrent_jobs=_clamp_int(
            _get(config, "jmcomic_max_concurrent_jobs", 2),
            default=2,
            minimum=1,
            maximum=2,
        ),
        max_queued_jobs=_clamp_int(
            _get(config, "jmcomic_max_queued_jobs", 50),
            default=50,
            minimum=1,
            maximum=100,
        ),
        cache_max_bytes=_clamp_int(
            _get(config, "jmcomic_cache_max_gb", 10),
            default=10,
            minimum=1,
            maximum=100,
        )
        * 1024
        * 1024
        * 1024,
    )


def _get(config, key: str, default=None):
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            value = getter(key)
            return default if value is None else value
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _read_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "开启", "启用"}:
            return True
        if normalized in {"0", "false", "no", "off", "关闭", "禁用"}:
            return False
    return default


def _clamp_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))
