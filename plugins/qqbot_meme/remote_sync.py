from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import hashlib
import mimetypes
import os
import random
import string
import tempfile
import time

from .storage import IMAGE_EXTENSIONS, MAX_IMAGE_BYTES, MemeStore


class RemoteProvider(Protocol):
    label: str

    def list(self) -> list[dict[str, Any]]: ...
    def upload(self, path: Path, category: str) -> None: ...
    def download(self, item: dict[str, Any], target: Path) -> None: ...
    def delete(self, remote_id: str) -> None: ...


class RemoteSync:
    def __init__(self, store: MemeStore, provider: RemoteProvider) -> None:
        self.store = store
        self.provider = provider

    def status(self) -> dict[str, Any]:
        local = {f"{path.parent.name}/{path.name}": path for path in self.store.iter_images()}
        remote_items = self.provider.list()
        remote = {self._key(item): item for item in remote_items}
        to_upload = [self._local_item(key, path) for key, path in local.items() if key not in remote]
        to_download = [item for key, item in remote.items() if key not in local]
        return {
            "provider_label": self.provider.label,
            "to_upload": to_upload,
            "to_download": to_download,
            "to_delete_local": [self._local_item(key, path) for key, path in local.items() if key not in remote],
            "to_delete_remote": to_download,
            "upload_count": len(to_upload),
            "download_count": len(to_download),
            "remote_image_count": len(remote_items),
            "remote_total_bytes": sum(int(item.get("size") or 0) for item in remote_items),
            "is_synced": not to_upload and not to_download,
        }

    def run(self, task: str) -> dict[str, int]:
        status = self.status()
        uploaded = downloaded = deleted = 0
        if task in {"upload", "overwrite_to_remote"}:
            for item in status["to_upload"]:
                self.provider.upload(Path(item["path"]), str(item["category"]))
                uploaded += 1
        if task in {"download", "overwrite_from_remote"}:
            for item in status["to_download"]:
                category = str(item.get("category") or "default")
                filename = Path(str(item["filename"])).name
                declared_size = int(item.get("size") or 0)
                if declared_size > MAX_IMAGE_BYTES:
                    raise ValueError(f"远端图片超过 20 MiB 限制：{category}/{filename}")
                self.store.ensure_category(category)
                category_dir = self.store.memes_dir / category
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".remote.", suffix=".part", dir=category_dir
                )
                os.close(descriptor)
                temporary = Path(temporary_name)
                try:
                    self.provider.download(item, temporary)
                    self.store.publish_download(category, filename, temporary)
                    downloaded += 1
                finally:
                    temporary.unlink(missing_ok=True)
        if task == "overwrite_to_remote":
            for item in status["to_delete_remote"]:
                self.provider.delete(str(item["id"]))
                deleted += 1
        elif task == "overwrite_from_remote":
            for item in status["to_delete_local"]:
                if self.store.delete_image(str(item["category"]), str(item["filename"])):
                    deleted += 1
        if task not in {"upload", "download", "overwrite_to_remote", "overwrite_from_remote"}:
            raise ValueError(f"未知同步任务：{task}")
        return {"uploaded": uploaded, "downloaded": downloaded, "deleted": deleted}

    @staticmethod
    def _key(item: dict[str, Any]) -> str:
        return f"{str(item.get('category') or 'default').strip('/')}/{Path(str(item.get('filename') or '')).name}"

    @staticmethod
    def _local_item(key: str, path: Path) -> dict[str, Any]:
        return {"id": key, "category": path.parent.name, "filename": path.name, "path": str(path), "size": path.stat().st_size}


class R2Provider:
    label = "Cloudflare R2"

    def __init__(self, config: dict[str, str]) -> None:
        required = ("account_id", "access_key_id", "secret_access_key", "bucket_name")
        missing = [name for name in required if not str(config.get(name) or "").strip()]
        if missing:
            raise ValueError("Cloudflare R2 配置缺少：" + ", ".join(missing))
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("Cloudflare R2 同步需要 boto3 和 botocore") from exc
        self.bucket = config["bucket_name"]
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{config['account_id']}.r2.cloudflarestorage.com",
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def list(self) -> list[dict[str, Any]]:
        result = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="memes/"):
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                parts = key.removeprefix("memes/").split("/")
                if len(parts) < 2 or Path(parts[-1]).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                result.append({"id": key, "category": "/".join(parts[:-1]), "filename": parts[-1], "size": int(item.get("Size") or 0)})
        return result

    def upload(self, path: Path, category: str) -> None:
        self.client.upload_file(str(path), self.bucket, f"memes/{category}/{path.name}", ExtraArgs={"ContentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream"})

    def download(self, item: dict[str, Any], target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            self.client.download_fileobj(
                self.bucket,
                str(item["id"]),
                _LimitedWriter(handle, MAX_IMAGE_BYTES),
            )

    def delete(self, remote_id: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=remote_id)


class StarDotsProvider:
    label = "StarDots"
    base_url = "https://api.stardots.io/openapi"

    def __init__(self, config: dict[str, str]) -> None:
        required = ("key", "secret", "space")
        missing = [name for name in required if not str(config.get(name) or "").strip()]
        if missing:
            raise ValueError("StarDots 配置缺少：" + ", ".join(missing))
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("StarDots 同步需要 requests") from exc
        self.requests = requests
        self.key, self.secret, self.space = config["key"], config["secret"], config["space"]
        self.session = requests.Session()

    def _headers(self, *, json_content: bool = True) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        signature = hashlib.md5(f"{timestamp}|{self.secret}|{nonce}".encode()).hexdigest().upper()
        headers = {"x-stardots-timestamp": timestamp, "x-stardots-nonce": nonce, "x-stardots-key": self.key, "x-stardots-sign": signature}
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    def list(self) -> list[dict[str, Any]]:
        result, page = [], 1
        while True:
            response = self.session.get(f"{self.base_url}/file/list", headers=self._headers(), params={"space": self.space, "page": page, "pageSize": 100}, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success"):
                raise RuntimeError("StarDots 获取文件列表失败")
            items = payload.get("data", {}).get("list", [])
            for item in items:
                remote_name = str(item.get("name") or "")
                if "@@CAT@@" in remote_name:
                    encoded, filename = remote_name.split("@@CAT@@", 1)
                    category = encoded.replace("@@DIR@@", "/")
                else:
                    category, filename = "default", remote_name
                result.append({"id": remote_name, "remote_name": remote_name, "category": category, "filename": filename, "url": item.get("url", ""), "size": int(item.get("size") or item.get("fileSize") or 0)})
            if len(items) < 100:
                return result
            page += 1

    def upload(self, path: Path, category: str) -> None:
        remote_name = f"{category.replace('/', '@@DIR@@')}@@CAT@@{path.name}"
        with path.open("rb") as handle:
            response = self.session.put(f"{self.base_url}/file/upload", headers=self._headers(json_content=False), files={"file": (remote_name, handle, mimetypes.guess_type(path.name)[0]), "space": (None, self.space)}, timeout=60)
        response.raise_for_status()
        if not response.json().get("success"):
            raise RuntimeError("StarDots 上传失败")

    def download(self, item: dict[str, Any], target: Path) -> None:
        remote_name = str(item.get("remote_name") or item.get("id") or "")
        ticket_response = self.session.post(
            f"{self.base_url}/file/ticket",
            headers=self._headers(),
            json={"space": self.space, "filename": remote_name},
            timeout=30,
        )
        ticket_response.raise_for_status()
        ticket_payload = ticket_response.json()
        if not ticket_payload.get("success"):
            raise RuntimeError("StarDots 获取下载票据失败")
        ticket = str(ticket_payload.get("data", {}).get("ticket") or "")
        url = f"https://i.stardots.io/{self.space}/{remote_name}?ticket={ticket}"
        response = self.session.get(url, timeout=60, stream=True)
        response.raise_for_status()
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_IMAGE_BYTES:
                    raise ValueError("远端图片超过 20 MiB 限制")
                handle.write(chunk)

    def delete(self, remote_id: str) -> None:
        response = self.session.delete(f"{self.base_url}/file/delete", headers=self._headers(), json={"space": self.space, "filenameList": [remote_id]}, timeout=30)
        response.raise_for_status()
        if not response.json().get("success"):
            raise RuntimeError("StarDots 删除失败")


class _LimitedWriter:
    """File-like sink that aborts provider streaming beyond the artifact limit."""

    def __init__(self, handle: Any, limit: int) -> None:
        self.handle = handle
        self.limit = limit
        self.written = 0

    def write(self, content: bytes) -> int:
        self.written += len(content)
        if self.written > self.limit:
            raise ValueError("远端图片超过 20 MiB 限制")
        return self.handle.write(content)


def create_remote_sync(store: MemeStore, provider_name: str, config: dict[str, str]) -> RemoteSync:
    normalized = provider_name.strip().lower()
    if normalized in {"cloudflare_r2", "r2"}:
        return RemoteSync(store, R2Provider(config))
    if normalized == "stardots":
        return RemoteSync(store, StarDotsProvider(config))
    raise ValueError("图床服务未配置；支持 stardots 或 cloudflare_r2")
