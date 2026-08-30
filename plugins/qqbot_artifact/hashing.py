from __future__ import annotations

from pathlib import Path
import hashlib
import zipfile

from .models import ArtifactRequestError


_ARCHIVE_SUFFIXES = {".zip", ".apk"}


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ArtifactRequestError("Artifact file cannot be read.", status=404) from exc
    return digest.hexdigest()


def validate_zip_structure(
    path: Path,
    *,
    max_entries: int,
    max_uncompressed_bytes: int,
) -> None:
    """Validate zip metadata without reading every expanded entry body."""

    try:
        with zipfile.ZipFile(path, "r") as archive:
            entries = archive.infolist()
            _validate_entries(
                entries,
                max_entries=max_entries,
                max_uncompressed_bytes=max_uncompressed_bytes,
            )
    except (zipfile.BadZipFile, RuntimeError, UnicodeError) as exc:
        raise ArtifactRequestError("Artifact is not a readable zip archive.") from exc


def calculate_artifact_content_sha256(
    path: Path,
    file_sha256: str,
    *,
    max_entries: int,
    max_uncompressed_bytes: int,
) -> str:
    """Use stable expanded-content hashes for zip builds and file hashes for APKs."""

    if path.suffix.lower() not in _ARCHIVE_SUFFIXES:
        raise ArtifactRequestError("Artifact must be a zip or APK archive.")
    if path.suffix.lower() == ".apk":
        validate_zip_structure(
            path,
            max_entries=max_entries,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
        return file_sha256
    return calculate_zip_content_sha256(
        path,
        max_entries=max_entries,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )


def calculate_zip_content_sha256(
    path: Path,
    *,
    max_entries: int,
    max_uncompressed_bytes: int,
) -> str:
    """Hash stable entry names, sizes and bytes while ignoring zip timestamps."""

    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(path, "r") as archive:
            entries = archive.infolist()
            _validate_entries(
                entries,
                max_entries=max_entries,
                max_uncompressed_bytes=max_uncompressed_bytes,
            )
            for info in sorted(entries, key=lambda item: (item.filename.casefold(), item.filename)):
                digest.update(info.filename.encode("utf-8"))
                digest.update(b"\0")
                digest.update(str(info.file_size).encode("ascii"))
                digest.update(b"\0")
                with archive.open(info, "r") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                digest.update(b"\0")
    except (zipfile.BadZipFile, RuntimeError, UnicodeError) as exc:
        raise ArtifactRequestError("Artifact is not a readable zip archive.") from exc
    return digest.hexdigest()


def _validate_entries(
    entries: list[zipfile.ZipInfo],
    *,
    max_entries: int,
    max_uncompressed_bytes: int,
) -> None:
    if len(entries) > max_entries:
        raise ArtifactRequestError("Artifact archive has too many entries.")
    total_size = sum(item.file_size for item in entries)
    if total_size > max_uncompressed_bytes:
        raise ArtifactRequestError("Artifact archive expands beyond the configured limit.")
    if any(item.flag_bits & 0x1 for item in entries):
        raise ArtifactRequestError("Encrypted artifact archives are not supported.")
