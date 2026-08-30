from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import hashlib
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt below.
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX uses fcntl above.
    msvcrt = None  # type: ignore[assignment]

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_CATEGORIES = {
    "angry": "抱怨、批评或激烈反对",
    "happy": "成功确认、积极反馈或庆祝",
    "sad": "伤心、歉意、遗憾或安慰",
    "surprised": "超出预期的信息",
    "confused": "请求澄清或表达理解障碍",
    "color": "轻松社交中的暧昧表达",
    "cpu": "技术讨论中表示思维卡顿",
    "fool": "自嘲或缓和气氛",
    "givemoney": "涉及报酬或奖励",
    "like": "表达喜爱",
    "see": "偷瞄或持续关注",
    "shy": "隐私话题或收到赞美",
    "work": "工作流程相关场景",
    "reply": "等待反馈",
    "meow": "萌系互动",
    "baka": "友善的轻微责备或吐槽",
    "morning": "早安问候",
    "sleep": "作息、疲劳或休息",
    "sigh": "无奈、无语或感慨",
}

_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_TRANSACTION_STATE = threading.local()


class MemeStore:
    """Filesystem and locked JSON index for the shared meme_manager runtime."""

    def __init__(self, root: Path, defaults_root: Path | None = None) -> None:
        self.root = Path(root)
        self.memes_dir = self.root / "memes"
        self.index_path = self.root / "meme_index.json"
        self.lock_path = self.root / ".meme_index.lock"
        self.legacy_descriptions_path = self.root / "memes_data.json"
        self.defaults_root = Path(defaults_root) if defaults_root else None
        self.memes_dir.mkdir(parents=True, exist_ok=True)
        with self._transaction():
            if not self.index_path.is_file():
                self._save_unlocked(self._scan_index())

    def load_index(self) -> dict[str, Any]:
        with self._transaction():
            return self._load_unlocked()

    def save_index(self, index: dict[str, Any]) -> None:
        with self._transaction():
            self._save_unlocked(index)

    def categories(self) -> dict[str, dict[str, Any]]:
        return self.load_index()["categories"]

    def category_counts(self) -> dict[str, int]:
        with self._transaction():
            counts = {name: 0 for name in self._load_unlocked()["categories"]}
            for path in self.iter_images():
                counts[path.parent.name] = counts.get(path.parent.name, 0) + 1
            return dict(sorted(counts.items()))

    def iter_images(self) -> list[Path]:
        return sorted(
            path
            for path in self.memes_dir.glob("*/*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )

    def grouped_files(self) -> dict[str, list[str]]:
        with self._transaction():
            grouped = {name: [] for name in self._load_unlocked()["categories"]}
            for path in self.iter_images():
                grouped.setdefault(path.parent.name, []).append(path.name)
            return grouped

    def select_image(self, category: str, text: str, recent: set[str]) -> Path | None:
        """Select one enabled local image using index metadata without external model calls."""
        category = self._safe_category(category)
        normalized_text = text.casefold()
        with self._transaction():
            index = self._load_unlocked()
            category_metadata = index["categories"].get(category)
            if not isinstance(category_metadata, dict) or category_metadata.get("auto_send_enabled", True) is False:
                return None
            if self._matches_any(normalized_text, category_metadata, "avoid_when", "disabled_scenes"):
                return None
            weighted: list[tuple[Path, float]] = []
            for item in index["images"]:
                if item.get("category") != category or item.get("auto_send_enabled", True) is False:
                    continue
                filename = Path(str(item.get("filename") or "")).name
                relative_path = f"{category}/{filename}"
                path = self.memes_dir / relative_path
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if not filename or relative_path in recent or not path.is_file() or not (0 < size <= MAX_IMAGE_BYTES):
                    continue
                if self._matches_any(normalized_text, item, "avoid_when", "disabled_scenes"):
                    continue
                terms = self._metadata_terms(
                    category_metadata,
                    item,
                    keys=("keywords", "use_cases", "applicable_scenes", "emotion_tags"),
                )
                matches = sum(1 for term in terms if term.casefold() in normalized_text)
                try:
                    base_weight = float(item.get("weight", 1.0))
                except (TypeError, ValueError):
                    base_weight = 1.0
                if base_weight > 0:
                    weighted.append((path, base_weight * (1 + matches)))
            if not weighted:
                return None
            return random.choices(
                [item[0] for item in weighted],
                weights=[item[1] for item in weighted],
                k=1,
            )[0]

    @classmethod
    def _matches_any(cls, text: str, metadata: Mapping[str, Any], *keys: str) -> bool:
        return any(term.casefold() in text for term in cls._metadata_terms(metadata, keys=keys))

    @staticmethod
    def _metadata_terms(*metadata_items: Mapping[str, Any], keys: tuple[str, ...]) -> set[str]:
        terms: set[str] = set()
        for metadata in metadata_items:
            for key in keys:
                value = metadata.get(key, [])
                raw_terms = value if isinstance(value, list) else [value]
                terms.update(str(item).strip() for item in raw_terms if str(item).strip())
        return terms

    def add_bytes(self, category: str, filename: str, content: bytes) -> Path:
        category = self._safe_category(category)
        if not content:
            raise ValueError("图片内容为空")
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("图片超过 20 MiB 限制")
        suffix = self._image_suffix(filename, content)
        with self._transaction():
            index = self._load_unlocked()
            if category not in index["categories"]:
                raise ValueError(f"不存在的表情类别：{category}")
            category_dir = self.memes_dir / category
            category_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(content).hexdigest()
            for existing in category_dir.iterdir():
                if existing.is_file() and hashlib.sha256(existing.read_bytes()).hexdigest() == digest:
                    raise ValueError(f"同一分类中已存在相同图片：{existing.name}")
            stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(filename).stem).strip("._") or str(int(time.time()))
            target = self._available_path(category_dir / f"{stem}{suffix}")
            self._atomic_bytes(target, content)
            index["images"].append(self._image_entry(category, target))
            self._save_unlocked(index)
            return target

    def publish_download(self, category: str, filename: str, temporary: Path) -> Path:
        """Validate a bounded remote artifact, atomically publish it, then index it."""
        category = self._safe_category(category)
        filename = Path(filename).name
        temporary = Path(temporary)
        with self._transaction():
            index = self._load_unlocked()
            if category not in index["categories"]:
                raise ValueError(f"不存在的表情类别：{category}")
            self._validate_image_file(temporary, filename)
            category_dir = self.memes_dir / category
            category_dir.mkdir(parents=True, exist_ok=True)
            target = category_dir / filename
            if target.exists():
                raise FileExistsError(f"表情图片已存在：{category}/{filename}")
            os.replace(temporary, target)
            index["images"] = [
                item
                for item in index["images"]
                if not (item.get("category") == category and item.get("filename") == filename)
            ]
            index["images"].append(self._image_entry(category, target))
            self._save_unlocked(index)
            return target

    def clear_category(self, category: str) -> int:
        category = self._safe_category(category)
        with self._transaction():
            index = self._load_unlocked()
            if category not in index["categories"]:
                raise ValueError(f"不存在的表情类别：{category}")
            paths = [path for path in self.iter_images() if path.parent.name == category]
            for path in paths:
                path.unlink()
            index["images"] = [item for item in index["images"] if item.get("category") != category]
            self._save_unlocked(index)
            return len(paths)

    def clear_all(self) -> int:
        with self._transaction():
            index = self._load_unlocked()
            paths = self.iter_images()
            for path in paths:
                path.unlink()
            index["images"] = []
            self._save_unlocked(index)
            return len(paths)

    def delete_category(self, category: str) -> int:
        category = self._safe_category(category)
        with self._transaction():
            index = self._load_unlocked()
            if category not in index["categories"] and not (self.memes_dir / category).is_dir():
                raise ValueError(f"不存在的表情类别：{category}")
            paths = [path for path in self.iter_images() if path.parent.name == category]
            shutil.rmtree(self.memes_dir / category, ignore_errors=True)
            index["categories"].pop(category, None)
            index["images"] = [item for item in index["images"] if item.get("category") != category]
            self._save_unlocked(index)
            return len(paths)

    def remove_category_config(self, category: str) -> None:
        category = self._safe_category(category)
        with self._transaction():
            index = self._load_unlocked()
            if category not in index["categories"]:
                raise ValueError(f"不存在的表情类别：{category}")
            index["categories"].pop(category)
            self._save_unlocked(index)

    def ensure_category(self, category: str, description: str = "请添加描述") -> None:
        category = self._safe_category(category)
        with self._transaction():
            index = self._load_unlocked()
            index["categories"].setdefault(category, self._category_payload(category, description))
            (self.memes_dir / category).mkdir(parents=True, exist_ok=True)
            self._save_unlocked(index)

    def rename_category(self, old_name: str, new_name: str) -> None:
        old_name, new_name = self._safe_category(old_name), self._safe_category(new_name)
        with self._transaction():
            index = self._load_unlocked()
            if old_name not in index["categories"] or new_name in index["categories"]:
                raise ValueError("源类别不存在或目标类别已存在")
            old_path, new_path = self.memes_dir / old_name, self.memes_dir / new_name
            if old_path.exists():
                old_path.rename(new_path)
            index["categories"][new_name] = index["categories"].pop(old_name)
            index["categories"][new_name]["label"] = new_name
            for item in index["images"]:
                if item.get("category") == old_name:
                    item["category"] = new_name
                    item["relative_path"] = f"memes/{new_name}/{item['filename']}"
            self._save_unlocked(index)

    def update_category(self, category: str, metadata: dict[str, Any]) -> None:
        category = self._safe_category(category)
        with self._transaction():
            index = self._load_unlocked()
            payload = index["categories"].setdefault(category, self._category_payload(category, ""))
            for key in ("label", "description", "use_cases", "avoid_when", "auto_send_enabled"):
                if key in metadata:
                    payload[key] = metadata[key]
            (self.memes_dir / category).mkdir(parents=True, exist_ok=True)
            self._save_unlocked(index)

    def update_image(self, category: str, filename: str, metadata: dict[str, Any]) -> dict[str, Any]:
        category, filename = self._safe_category(category), Path(filename).name
        with self._transaction():
            index = self._load_unlocked()
            entry = next(
                (
                    item
                    for item in index["images"]
                    if item.get("category") == category and item.get("filename") == filename
                ),
                None,
            )
            if entry is None or not (self.memes_dir / category / filename).is_file():
                raise FileNotFoundError("表情图片不存在")
            for key in (
                "title", "content_caption", "use_cases", "emotion_tags", "intensity",
                "avoid_when", "auto_send_enabled", "weight",
            ):
                if key in metadata:
                    entry[key] = metadata[key]
            self._save_unlocked(index)
            return dict(entry)

    def delete_image(self, category: str, filename: str) -> bool:
        category, filename = self._safe_category(category), Path(filename).name
        with self._transaction():
            path = self.memes_dir / category / filename
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                return False
            path.unlink()
            index = self._load_unlocked()
            index["images"] = [
                item
                for item in index["images"]
                if not (item.get("category") == category and item.get("filename") == filename)
            ]
            self._save_unlocked(index)
            return True

    def transfer_images(self, source: str, target: str, filenames: Iterable[str], *, copy: bool) -> dict[str, list[str]]:
        source, target = self._safe_category(source), self._safe_category(target)
        with self._transaction():
            index = self._load_unlocked()
            index["categories"].setdefault(target, self._category_payload(target, "请添加描述"))
            target_dir = self.memes_dir / target
            target_dir.mkdir(parents=True, exist_ok=True)
            result: dict[str, list[str]] = {"processed": [], "missing": [], "conflicting": []}
            for raw_name in dict.fromkeys(filenames):
                name = Path(str(raw_name)).name
                source_path, target_path = self.memes_dir / source / name, target_dir / name
                if not source_path.is_file():
                    result["missing"].append(name)
                    continue
                if target_path.exists():
                    result["conflicting"].append(name)
                    continue
                shutil.copy2(source_path, target_path) if copy else shutil.move(source_path, target_path)
                if not copy:
                    index["images"] = [
                        item
                        for item in index["images"]
                        if not (item.get("category") == source and item.get("filename") == name)
                    ]
                index["images"].append(self._image_entry(target, target_path))
                result["processed"].append(name)
            self._save_unlocked(index)
            return result

    def delete_image_metadata_only(self, category: str, filename: str) -> None:
        with self._transaction():
            index = self._load_unlocked()
            index["images"] = [
                item
                for item in index["images"]
                if not (item.get("category") == category and item.get("filename") == filename)
            ]
            self._save_unlocked(index)

    def restore_defaults(self, category: str = "") -> dict[str, Any]:
        if self.defaults_root is None or not self.defaults_root.is_dir():
            return {"source_exists": False, "copied": 0, "duplicates": 0, "categories": []}
        available = sorted(path.name for path in self.defaults_root.iterdir() if path.is_dir())
        if category and category not in available:
            raise ValueError(f"默认表情包中不存在类别：{category}")
        copied = duplicates = 0
        directories = [self.defaults_root / category] if category else [self.defaults_root / item for item in available]
        for source_dir in directories:
            self.ensure_category(source_dir.name, DEFAULT_CATEGORIES.get(source_dir.name, "请添加描述"))
            for source in source_dir.iterdir():
                if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                try:
                    self.add_bytes(source_dir.name, source.name, source.read_bytes())
                    copied += 1
                except ValueError as exc:
                    if "已存在相同图片" in str(exc):
                        duplicates += 1
                    else:
                        raise
        return {"source_exists": True, "copied": copied, "duplicates": duplicates, "categories": available}

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = self._scan_index()
        return self._normalize(raw)

    def _save_unlocked(self, index: dict[str, Any]) -> None:
        normalized = self._normalize(index)
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".meme_index.", suffix=".tmp", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        key = str(self.index_path.resolve())
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(key, threading.RLock())
        with process_lock:
            depths = getattr(_TRANSACTION_STATE, "depths", None)
            if depths is None:
                depths = {}
                _TRANSACTION_STATE.depths = depths
            if depths.get(key, 0):
                depths[key] += 1
                try:
                    yield
                finally:
                    depths[key] -= 1
                return
            self.root.mkdir(parents=True, exist_ok=True)
            lock_handle = self.lock_path.open("a+b")
            try:
                self._lock_file(lock_handle)
                depths[key] = 1
                yield
            finally:
                depths.pop(key, None)
                self._unlock_file(lock_handle)
                lock_handle.close()

    @staticmethod
    def _lock_file(handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return
        if msvcrt is None:
            raise RuntimeError("当前平台不支持跨进程文件锁")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    @staticmethod
    def _unlock_file(handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _scan_index(self) -> dict[str, Any]:
        descriptions = dict(DEFAULT_CATEGORIES)
        if self.legacy_descriptions_path.is_file():
            try:
                legacy = json.loads(self.legacy_descriptions_path.read_text(encoding="utf-8"))
                if isinstance(legacy, dict):
                    descriptions.update({str(key): str(value) for key, value in legacy.items()})
            except (OSError, json.JSONDecodeError):
                pass
        categories = {
            name: self._category_payload(name, description) for name, description in descriptions.items()
        }
        for directory in self.memes_dir.iterdir():
            if directory.is_dir():
                categories.setdefault(directory.name, self._category_payload(directory.name, "请添加描述"))
        images = [self._image_entry(path.parent.name, path) for path in self.iter_images()]
        return {"schema_version": 1, "source": "maibot_qqbot_meme", "categories": categories, "images": images}

    def _normalize(self, raw: object) -> dict[str, Any]:
        data = dict(raw) if isinstance(raw, dict) else {}
        categories = data.get("categories") if isinstance(data.get("categories"), dict) else {}
        normalized_categories: dict[str, dict[str, Any]] = {}
        for name, metadata in categories.items():
            safe_name = self._safe_category(str(name))
            payload = dict(metadata) if isinstance(metadata, dict) else {}
            normalized_categories[safe_name] = self._category_payload(
                safe_name, str(payload.get("description") or "")
            ) | payload
        images = []
        raw_images = data.get("images") if isinstance(data.get("images"), list) else []
        for item in raw_images:
            if not isinstance(item, dict):
                continue
            try:
                category = self._safe_category(str(item.get("category") or ""))
                filename = Path(str(item.get("filename") or "")).name
            except ValueError:
                continue
            if category and filename:
                normalized = dict(item)
                normalized.update({
                    "category": category,
                    "filename": filename,
                    "relative_path": f"memes/{category}/{filename}",
                })
                images.append(normalized)
                normalized_categories.setdefault(category, self._category_payload(category, ""))
        return {
            **data,
            "schema_version": 1,
            "source": str(data.get("source") or "maibot_qqbot_meme"),
            "updated_at": int(time.time()),
            "categories": dict(sorted(normalized_categories.items())),
            "images": sorted(images, key=lambda item: (item["category"], item["filename"])),
        }

    @staticmethod
    def _safe_category(category: str) -> str:
        value = str(category).strip()
        if not value or value in {".", ".."} or Path(value).name != value or any(char in value for char in "/\\\0"):
            raise ValueError("类别名称无效")
        return value

    @staticmethod
    def _category_payload(name: str, description: str) -> dict[str, Any]:
        return {
            "label": name,
            "description": description,
            "use_cases": [],
            "avoid_when": [],
            "auto_send_enabled": True,
        }

    def _image_entry(self, category: str, path: Path) -> dict[str, Any]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        return {
            "id": f"{category}-{path.stem}-{digest[:8]}",
            "category": category,
            "filename": path.name,
            "relative_path": f"memes/{category}/{path.name}",
            "title": path.stem,
            "content_caption": "",
            "use_cases": [],
            "emotion_tags": [],
            "intensity": 2,
            "avoid_when": [],
            "auto_send_enabled": True,
            "weight": 1.0,
            "sha256": digest,
        }

    def _upsert_path(self, category: str, path: Path) -> None:
        with self._transaction():
            index = self._load_unlocked()
            index["images"] = [
                item
                for item in index["images"]
                if not (item.get("category") == category and item.get("filename") == path.name)
            ]
            index["images"].append(self._image_entry(category, path))
            self._save_unlocked(index)

    @staticmethod
    def _atomic_bytes(target: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _available_path(path: Path) -> Path:
        if not path.exists():
            return path
        serial = 1
        while True:
            candidate = path.with_name(f"{path.stem}_{serial}{path.suffix}")
            if not candidate.exists():
                return candidate
            serial += 1

    @classmethod
    def _validate_image_file(cls, path: Path, filename: str) -> None:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError("远端图片下载失败") from exc
        if size <= 0:
            raise ValueError("远端图片内容为空")
        if size > MAX_IMAGE_BYTES:
            raise ValueError("远端图片超过 20 MiB 限制")
        extension = Path(filename).suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            raise ValueError("远端文件扩展名不受支持")
        with path.open("rb") as handle:
            content = handle.read(16)
        detected = cls._detected_suffix(content)
        compatible = {".jpg", ".jpeg"} if detected == ".jpg" else {detected}
        if extension not in compatible:
            raise ValueError("远端图片扩展名与文件签名不匹配")

    @classmethod
    def _image_suffix(cls, filename: str, content: bytes) -> str:
        detected = cls._detected_suffix(content[:16])
        extension = Path(filename).suffix.lower()
        if extension in IMAGE_EXTENSIONS:
            compatible = {".jpg", ".jpeg"} if detected == ".jpg" else {detected}
            if extension not in compatible:
                raise ValueError("图片扩展名与文件签名不匹配")
            return extension
        return detected

    @staticmethod
    def _detected_suffix(content: bytes) -> str:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if content.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return ".webp"
        raise ValueError("不支持的图片文件签名")
