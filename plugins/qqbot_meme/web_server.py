from __future__ import annotations

from collections.abc import Callable
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import json
import mimetypes
import secrets

from .storage import MemeStore


class MemeWebServer:
    """Authenticated local management server owned by the plugin lifecycle."""

    def __init__(
        self,
        store: MemeStore,
        assets_root: Path,
        host: str,
        port: int,
        remote_status: Callable[[], dict[str, Any]] | None = None,
        remote_task: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.assets_root = Path(assets_root)
        self.host = host
        self.port = port
        self.key = secrets.token_urlsafe(8)
        self._sessions: set[str] = set()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._remote_status = remote_status
        self._remote_task = remote_task

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._httpd)

    def start(self) -> None:
        if self.running:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner._handle(self)

            def do_POST(self) -> None:  # noqa: N802
                owner._handle(self)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = Thread(target=self._httpd.serve_forever, name="qqbot-meme-webui", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(request.path)
        path = parsed.path
        if path == "/health":
            self._json(request, {"status": "running", "version": "1.0"})
            return
        if path == "/login":
            self._login(request)
            return
        if not self._authenticated(request):
            request.send_response(HTTPStatus.FOUND)
            request.send_header("Location", "/login")
            request.end_headers()
            return
        try:
            if path == "/":
                self._file(request, self.assets_root / "templates" / "index.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                relative = Path(unquote(path.removeprefix("/static/")))
                self._safe_asset(request, self.assets_root / "static", relative)
            elif path.startswith("/memes/"):
                self._safe_asset(request, self.store.memes_dir, Path(unquote(path.removeprefix("/memes/"))))
            elif path.startswith("/api/"):
                self._api(request, path, parse_qs(parsed.query))
            else:
                self._json(request, {"message": "Not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, FileNotFoundError) as exc:
            self._json(request, {"message": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json(request, {"message": f"操作失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _login(self, request: BaseHTTPRequestHandler) -> None:
        error = ""
        if request.command == "POST":
            body = self._body(request).decode("utf-8", errors="replace")
            key = parse_qs(body).get("key", [""])[0]
            if secrets.compare_digest(key, self.key):
                token = secrets.token_urlsafe(24)
                self._sessions.add(token)
                request.send_response(HTTPStatus.FOUND)
                request.send_header("Location", "/")
                request.send_header("Set-Cookie", f"meme_session={token}; HttpOnly; SameSite=Strict; Path=/")
                request.end_headers()
                return
            error = "密钥错误，请重试。"
        template = (self.assets_root / "templates" / "login.html").read_text(encoding="utf-8")
        payload = template.replace("{{ERROR}}", error)
        self._bytes(request, payload.encode("utf-8"), "text/html; charset=utf-8")

    def _api(self, request: BaseHTTPRequestHandler, path: str, query: dict[str, list[str]]) -> None:
        del query
        if request.command == "GET" and path == "/api/emoji":
            self._json(request, self.store.grouped_files())
        elif request.command == "GET" and path == "/api/emotions":
            self._json(request, {name: meta.get("description", "") for name, meta in self.store.categories().items()})
        elif request.command == "GET" and path == "/api/index":
            self._json(request, self.store.load_index())
        elif request.command == "GET" and path.startswith("/api/emoji/"):
            self._json(request, self.store.grouped_files().get(unquote(path.rsplit("/", 1)[-1]), []))
        elif request.command == "GET" and path == "/api/sync/status":
            local = set(self.store.categories())
            directories = {item.name for item in self.store.memes_dir.iterdir() if item.is_dir()}
            self._json(request, {"status": "ok", "missing_in_config": sorted(directories - local), "deleted_categories": sorted(local - directories), "differences": {"missing_in_config": sorted(directories - local), "deleted_categories": sorted(local - directories)}})
        elif request.command == "GET" and path == "/api/img_host/sync/status":
            self._json(request, self._remote_status() if self._remote_status else {"error": "图床服务未配置"}, HTTPStatus.OK if self._remote_status else HTTPStatus.BAD_REQUEST)
        elif request.command == "POST" and path == "/api/emoji/add":
            fields, files = self._multipart(request)
            image = files.get("image_file")
            if image is None:
                raise ValueError("没有找到上传的图片文件")
            target = self.store.add_bytes(fields.get("category", ""), image[0], image[1])
            self._json(request, {"message": "表情包添加成功", "path": str(target), "category": target.parent.name, "filename": target.name}, HTTPStatus.CREATED)
        elif request.command == "POST" and path == "/api/emoji/metadata":
            data = self._json_body(request)
            entry = self.store.update_image(str(data.get("category") or ""), str(data.get("filename") or ""), data.get("metadata") if isinstance(data.get("metadata"), dict) else {})
            self._json(request, {"message": "元数据已保存", "entry": entry})
        elif request.command == "POST" and path == "/api/emoji/delete":
            data = self._json_body(request)
            deleted = self.store.delete_image(str(data.get("category") or ""), str(data.get("image_file") or ""))
            self._json(request, {"message": "Emoji deleted successfully"} if deleted else {"message": "Emoji not found"}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif request.command == "POST" and path == "/api/emoji/batch_delete":
            data = self._json_body(request)
            category = str(data.get("category") or "")
            names = data.get("image_files") if isinstance(data.get("image_files"), list) else []
            deleted, missing = [], []
            for name in names:
                (deleted if self.store.delete_image(category, str(name)) else missing).append(Path(str(name)).name)
            self._json(request, {"message": "Batch delete completed", "deleted_files": deleted, "missing_files": missing, "deleted_count": len(deleted), "missing_count": len(missing)})
        elif request.command == "POST" and path in {"/api/emoji/batch_move", "/api/emoji/batch_copy"}:
            data = self._json_body(request)
            result = self.store.transfer_images(str(data.get("source_category") or ""), str(data.get("target_category") or ""), data.get("image_files") if isinstance(data.get("image_files"), list) else [], copy=path.endswith("batch_copy"))
            verb = "copied" if path.endswith("batch_copy") else "moved"
            self._json(request, {f"{verb}_files": result["processed"], "missing_files": result["missing"], "conflicting_files": result["conflicting"], f"{verb}_count": len(result["processed"])})
        elif request.command == "POST" and path == "/api/emoji/move":
            data = self._json_body(request)
            result = self.store.transfer_images(str(data.get("source_category") or ""), str(data.get("target_category") or ""), [str(data.get("image_file") or "")], copy=False)
            self._json(request, {"message": "Emoji moved successfully", "filename": result["processed"][0]} if result["processed"] else {"message": "Emoji not found"}, HTTPStatus.OK if result["processed"] else HTTPStatus.NOT_FOUND)
        elif request.command == "POST" and path == "/api/category/clear":
            data = self._json_body(request)
            count = self.store.clear_category(str(data.get("category") or ""))
            self._json(request, {"message": "Category cleared successfully", "deleted_count": count})
        elif request.command == "POST" and path == "/api/emoji/clear_all":
            count = self.store.clear_all()
            self._json(request, {"message": "All emojis cleared successfully", "deleted_count": count})
        elif request.command == "POST" and path == "/api/category/delete":
            data = self._json_body(request)
            self.store.delete_category(str(data.get("category") or ""))
            self._json(request, {"message": "Category deleted successfully"})
        elif request.command == "POST" and path == "/api/category/remove_from_config":
            data = self._json_body(request)
            self.store.remove_category_config(str(data.get("category") or ""))
            self._json(request, {"message": "Category removed from config"})
        elif request.command == "POST" and path == "/api/category/restore":
            data = self._json_body(request)
            self.store.ensure_category(str(data.get("category") or ""), str(data.get("description") or "请添加描述"))
            self._json(request, {"message": "Category created successfully"})
        elif request.command == "POST" and path == "/api/category/rename":
            data = self._json_body(request)
            self.store.rename_category(str(data.get("old_name") or ""), str(data.get("new_name") or ""))
            self._json(request, {"message": "Category renamed successfully"})
        elif request.command == "POST" and path == "/api/category/update_description":
            data = self._json_body(request)
            category, description = str(data.get("tag") or ""), str(data.get("description") or "")
            self.store.update_category(category, {"description": description})
            self._json(request, {"category": category, "description": description})
        elif request.command == "POST" and path == "/api/sync/config":
            for directory in self.store.memes_dir.iterdir():
                if directory.is_dir() and directory.name not in self.store.categories():
                    self.store.ensure_category(directory.name)
            self._json(request, {"message": "配置同步成功"})
        elif request.command == "POST" and path in {"/api/img_host/sync/upload", "/api/img_host/sync/download"}:
            if self._remote_task is None:
                self._json(request, {"message": "图床服务未配置"}, HTTPStatus.BAD_REQUEST)
            else:
                self._remote_task("upload" if path.endswith("upload") else "download")
                self._json(request, {"success": True})
        elif request.command == "GET" and path == "/api/img_host/sync/check_process":
            self._json(request, {"completed": True, "success": True})
        else:
            self._json(request, {"message": "Not found"}, HTTPStatus.NOT_FOUND)

    def _authenticated(self, request: BaseHTTPRequestHandler) -> bool:
        cookies = SimpleCookie(request.headers.get("Cookie", ""))
        token = cookies.get("meme_session")
        return token is not None and token.value in self._sessions

    def _safe_asset(self, request: BaseHTTPRequestHandler, root: Path, relative: Path) -> None:
        target = (root / relative).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            self._json(request, {"message": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        self._file(request, target, mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    def _file(self, request: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._json(request, {"message": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        self._bytes(request, path.read_bytes(), content_type)

    @staticmethod
    def _body(request: BaseHTTPRequestHandler) -> bytes:
        length = int(request.headers.get("Content-Length", "0") or 0)
        if length > 20 * 1024 * 1024:
            raise ValueError("请求体超过 20 MiB 限制")
        return request.rfile.read(length)

    def _json_body(self, request: BaseHTTPRequestHandler) -> dict[str, Any]:
        data = json.loads(self._body(request).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON 请求体必须是对象")
        return data

    def _multipart(self, request: BaseHTTPRequestHandler) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        content_type = request.headers.get("Content-Type", "")
        message = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + self._body(request)
        )
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if name and filename:
                files[str(name)] = (Path(filename).name, payload)
            elif name:
                fields[str(name)] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return fields, files

    @staticmethod
    def _json(request: BaseHTTPRequestHandler, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        MemeWebServer._bytes(request, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    @staticmethod
    def _bytes(request: BaseHTTPRequestHandler, payload: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(payload)))
        request.send_header("Cache-Control", "no-store")
        request.end_headers()
        request.wfile.write(payload)
