from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from aiohttp import web
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase
from qqbot_common.api_results import require_api_result

from .models import ArtifactRequestError
from .publisher import ArtifactPublisher, PublisherLimits
from .validation import normalize_allowed_roots, validate_publish_request
from .shapez_mods import ShapezModPublisher


_PUBLISH_PATH = "/admin/api/artifacts/publish-local"
_HEALTH_PATH = "/healthz"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_LOOPBACK_REMOTES = {"127.0.0.1", "::1", "localhost"}


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="启用本地产物发布服务")
    config_version: str = Field(default="0.1.2", description="配置版本")


class ListenerSection(PluginConfigBase):
    __ui_label__ = "监听"
    __ui_order__ = 1

    host: str = Field(default="127.0.0.1", description="必须是回环监听地址")
    port: int = Field(default=8080, ge=1, le=65535, description="本地兼容 API 端口")
    account_self_id: str = Field(default="", description="当前 MaiBot 实例 QQ；留空不启动监听")
    owner_self_id: str = Field(default="1443944862", description="唯一监听实例 QQ")
    request_max_bytes: int = Field(default=1048576, ge=1024, le=16777216, description="HTTP JSON 请求上限")


class PublishingSection(PluginConfigBase):
    __ui_label__ = "发布"
    __ui_order__ = 2

    allowed_roots: list[str] = Field(default_factory=lambda: ["/mnt/d/project"], description="允许发布的 Git 工作区根")
    max_artifact_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1,
        le=2 * 1024 * 1024 * 1024,
        description="单个 zip 或 APK 文件最大字节数",
    )
    max_zip_entries: int = Field(default=10000, ge=1, le=100000, description="zip/APK 最大条目数")
    max_uncompressed_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=1,
        le=8 * 1024 * 1024 * 1024,
        description="zip/APK 解压后总大小上限",
    )
    publish_max_age_seconds: int = Field(default=300, ge=10, le=3600, description="请求时间戳最大偏差")
    lock_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0, description="跨进程发布锁等待秒数")
    api_timeout_seconds: float = Field(
        default=900.0,
        ge=30.0,
        le=1800.0,
        description="NapCat 群文件动作的插件 RPC 超时秒数",
    )


class StorageSection(PluginConfigBase):
    __ui_label__ = "存储"
    __ui_order__ = 3

    runtime_root_override: str = Field(default="", description="留空时读取 qqbot_common 共享业务根")


class ArtifactConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    listener: ListenerSection = Field(default_factory=ListenerSection)
    publishing: PublishingSection = Field(default_factory=PublishingSection)
    storage: StorageSection = Field(default_factory=StorageSection)


class QQBotArtifactPlugin(MaiBotPlugin):
    """Expose the localhost-compatible build artifact publishing endpoint."""

    config_model: ClassVar[type[PluginConfigBase] | None] = ArtifactConfig

    def __init__(self) -> None:
        super().__init__()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._publisher: ArtifactPublisher | None = None
        self._mod_publisher: ShapezModPublisher | None = None

    async def on_load(self) -> None:
        await self._start_listener()

    async def on_unload(self) -> None:
        await self._stop_listener()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        await self._stop_listener()
        await self._start_listener()
        self.ctx.logger.info("QQBot 本地产物发布配置已更新: %s", version)

    async def _start_listener(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("QQBot 本地产物发布插件未启用")
            return
        account_self_id = self.config.listener.account_self_id.strip()
        owner_self_id = self.config.listener.owner_self_id.strip()
        if not account_self_id:
            self.ctx.logger.info("QQBot 本地产物发布未配置当前实例账号，跳过监听")
            return
        if not owner_self_id or account_self_id != owner_self_id:
            self.ctx.logger.info(
                "QQBot 本地产物发布由其他实例负责: current=%s owner=%s",
                account_self_id,
                owner_self_id,
            )
            return
        host = self.config.listener.host.strip().lower()
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("artifact listener.host 必须是回环地址")
        runtime_root = await self._runtime_root()
        allowed_roots = normalize_allowed_roots(self.config.publishing.allowed_roots)
        self._publisher = ArtifactPublisher(
            self._call_api,
            runtime_root=runtime_root,
            self_id=account_self_id,
            limits=PublisherLimits(
                max_zip_entries=self.config.publishing.max_zip_entries,
                max_uncompressed_bytes=self.config.publishing.max_uncompressed_bytes,
                lock_timeout_seconds=self.config.publishing.lock_timeout_seconds,
            ),
        )
        self._mod_publisher = ShapezModPublisher(self._call_mod_api, account_self_id, runtime_root)
        app = web.Application(client_max_size=self.config.listener.request_max_bytes)
        app["allowed_roots"] = allowed_roots
        app.router.add_get(_HEALTH_PATH, self._health)
        app.router.add_post(_PUBLISH_PATH, self._publish_local)
        app.router.add_post("/admin/api/artifacts/shapez1-mods", self._sync_shapez_mod)
        app.router.add_get("/admin/api/artifacts/group-folders", self._group_folders)
        app.router.add_get("/admin/api/artifacts/group-files", self._group_files)
        app.router.add_post("/admin/api/artifacts/group-folders", self._group_folders)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, self.config.listener.port)
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            self._publisher = None
            raise
        self._runner = runner
        self._site = site
        self.ctx.logger.info(
            "QQBot 本地产物发布已监听 http://%s:%s",
            host,
            self.config.listener.port,
        )

    async def _stop_listener(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._site = None
        self._runner = None
        self._publisher = None
        self._mod_publisher = None

    async def _health(self, request: web.Request) -> web.Response:
        if not _is_loopback_request(request):
            return web.json_response({"ok": False, "detail": "Local request required."}, status=403)
        return web.json_response(
            {
                "ok": True,
                "plugin": "mlj.qqbot-artifact",
                "self_id": self.config.listener.account_self_id.strip(),
            }
        )

    async def _sync_shapez_mod(self, request: web.Request) -> web.Response:
        """在固定目标群文件夹中同步一个已确认模组。"""
        if not _is_loopback_request(request):
            return web.json_response({"detail": "Local request required."}, status=403)
        if self._mod_publisher is None:
            return web.json_response({"detail": "Mod publisher is unavailable."}, status=503)
        try:
            result = await self._mod_publisher.sync(await request.json())
        except (ValueError, OSError) as exc:
            return web.json_response({"detail": str(exc)}, status=400)
        except Exception as exc:
            self.ctx.logger.warning("一代模组同步失败：%s", type(exc).__name__)
            return web.json_response({"detail": str(exc)}, status=502)
        return web.json_response({"ok": True, **result})

    async def _call_mod_api(self, action: str, **params: Any) -> Any:
        """通过插件 SDK 解包 NapCat 文件调用，协议错误交由模组发布器处理。"""
        result = await self._call_api("adapter.napcat.file." + action, params=params)
        return require_api_result(result, "同步一代模组群文件")

    async def _group_files(self, request: web.Request) -> web.Response:
        """读取指定目录文件元数据，供投递中断后核对实际状态。"""
        if not _is_loopback_request(request):
            return web.json_response({"detail": "Local request required."}, status=403)
        try:
            group_id = int(request.query.get("group_id", ""))
            folder_id = request.query.get("folder_id", "").strip()
            if group_id <= 0 or len(folder_id) > 256 or any(ord(char) < 32 for char in folder_id):
                raise ValueError
        except ValueError:
            return web.json_response({"detail": "Invalid group or folder ID."}, status=400)
        if folder_id == "/":
            folder_id = ""
        try:
            params = {"group_id": group_id}
            if folder_id:
                params["folder_id"] = folder_id
            action = "get_group_files_by_folder" if folder_id else "get_group_root_files"
            result = await asyncio.wait_for(self._call_api("adapter.napcat.file." + action, params=params), timeout=30)
            payload = require_api_result(result, "读取群文件")
            if isinstance(payload, Mapping) and "data" in payload and ("status" in payload or "retcode" in payload):
                payload = payload["data"]
            files = payload.get("files") if isinstance(payload, Mapping) else None
            if not isinstance(files, list):
                raise ValueError("OneBot 未返回文件列表")
            response = [{key: item.get(key) for key in ("file_id", "file_name", "file_size")} for item in files]
        except Exception as exc:
            self.ctx.logger.warning("读取群文件失败: error_type=%s", type(exc).__name__)
            return web.json_response({"detail": "Group file lookup failed."}, status=502)
        return web.json_response({"group_id": group_id, "folder_id": folder_id, "files": response})

    async def _group_folders(self, request: web.Request) -> web.Response:
        """供本机读取目录，或显式创建/复用一个指定名称的群文件目录。"""
        if not _is_loopback_request(request):
            return web.json_response({"detail": "Local request required."}, status=403)
        folder_name = None
        try:
            if request.method == "POST":
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError
                folder_name = body.get("folder_name")
                if (not isinstance(folder_name, str) or not folder_name.strip()
                        or len(folder_name) > 100 or any(ord(char) < 32 for char in folder_name)):
                    raise ValueError
                folder_name = folder_name.strip()
                raw_group_id = body.get("group_id", "")
            else:
                raw_group_id = request.query.get("group_id", "")
            group_id = int(raw_group_id)
            if group_id <= 0 or isinstance(raw_group_id, bool):
                raise ValueError
        except (ValueError, TypeError):
            return web.json_response({"detail": "Positive group_id and valid folder_name required."}, status=400)
        try:
            folders = await self._read_group_folders(group_id)
            if folder_name is not None:
                matches = [item for item in folders if item["folder_name"] == folder_name]
                if not matches:
                    created = await asyncio.wait_for(self._call_api(
                        "adapter.napcat.file.create_group_file_folder",
                        params={"group_id": group_id, "folder_name": folder_name},
                    ), timeout=30)
                    require_api_result(created, "创建群文件夹")
                    folders = await self._read_group_folders(group_id)
                    matches = [item for item in folders if item["folder_name"] == folder_name]
                if len(matches) != 1:
                    return web.json_response({"detail": "Cannot uniquely resolve requested folder."}, status=409)
                folders = matches
        except Exception as exc:
            self.ctx.logger.warning("群文件夹操作失败: error_type=%s", type(exc).__name__)
            return web.json_response({"detail": "Group folder operation failed; query before retrying."}, status=502)
        return web.json_response({"group_id": group_id, "folders": folders})

    async def _read_group_folders(self, group_id: int) -> list[dict[str, str]]:
        """读取必要的目录元数据，不暴露群文件内容。"""
        result = await asyncio.wait_for(
            self._call_api("adapter.napcat.file.get_group_root_files", params={"group_id": group_id}),
            timeout=30,
        )
        payload = require_api_result(result, "读取群文件夹")
        if isinstance(payload, Mapping) and "data" in payload and ("status" in payload or "retcode" in payload):
            payload = payload["data"]
        folders = payload.get("folders") if isinstance(payload, Mapping) else None
        if not isinstance(folders, list):
            raise ValueError("OneBot 未返回文件夹列表")
        return [{"folder_id": str(item["folder_id"]), "folder_name": str(item["folder_name"])} for item in folders]

    async def _publish_local(self, request: web.Request) -> web.Response:
        if not _is_loopback_request(request):
            return web.json_response({"ok": False, "detail": "Local request required."}, status=403)
        publisher = self._publisher
        if publisher is None:
            return web.json_response({"ok": False, "detail": "Artifact publisher is unavailable."}, status=503)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "detail": "Invalid JSON payload."}, status=400)
        try:
            publish_request = await _to_thread_validate(self, payload, request.app["allowed_roots"])
            result = await publisher.publish(publish_request)
        except ArtifactRequestError as exc:
            return web.json_response({"ok": False, "detail": exc.detail}, status=exc.status)
        except TimeoutError:
            return web.json_response({"ok": False, "detail": "Artifact publisher is busy."}, status=503)
        except Exception as exc:
            self.ctx.logger.warning("本地产物发布失败: error_type=%s", type(exc).__name__)
            return web.json_response({"ok": False, "detail": "Artifact publication failed."}, status=500)
        return web.json_response(
            {
                "ok": True,
                "uploaded": result.uploaded,
                "deleted": result.deleted,
                "skipped": result.skipped,
            }
        )

    async def _call_api(self, api_name: str, **kwargs: Any) -> Any:
        timeout_ms = int(float(self.config.publishing.api_timeout_seconds) * 1000)
        return await self.ctx.call_capability(
            "api.call",
            timeout_ms=timeout_ms,
            api_name=api_name,
            version="",
            args=kwargs,
        )

    async def _runtime_root(self) -> Path:
        override = self.config.storage.runtime_root_override.strip()
        if override:
            return Path(override).expanduser().resolve()
        result = await self.ctx.api.call("qqbot.storage.runtime_root")
        payload = require_api_result(result, "读取 QQBot 业务数据根")
        path = str(payload.get("path") or "").strip() if isinstance(payload, Mapping) else ""
        if not path:
            raise RuntimeError("QQBot 公共插件没有返回业务数据根")
        return Path(path).resolve()


async def _to_thread_validate(
    plugin: QQBotArtifactPlugin,
    payload: object,
    allowed_roots: tuple[Path, ...],
):
    import asyncio

    return await asyncio.to_thread(
        validate_publish_request,
        payload,
        allowed_roots=allowed_roots,
        max_artifact_bytes=plugin.config.publishing.max_artifact_bytes,
        publish_max_age_seconds=plugin.config.publishing.publish_max_age_seconds,
    )


def _is_loopback_request(request: web.Request) -> bool:
    remote = str(request.remote or "").strip().lower()
    return remote in _LOOPBACK_REMOTES


def create_plugin() -> QQBotArtifactPlugin:
    """Create the local artifact API plugin."""

    return QQBotArtifactPlugin()
