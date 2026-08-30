from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping

import asyncio
import re
import time

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase
from qqbot_common.api_results import require_api_result

from .comic_pdf import ComicPdfConfig, ComicPdfError, ComicPdfService, ComicQueueFullError
from .comic_pdf.friend_route import ComicFriendRouteCoordinator, comic_event_key, is_onebot_friend
from .comic_pdf.sender import send_private_pdfs_with_password


JM_COMMAND_PATTERN = r"(?i)^jm\s*([0-9]+)$"


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 JM 漫画 PDF 固定命令")
    config_version: str = Field(default="0.1.0", description="配置版本")


class StorageSection(PluginConfigBase):
    __ui_label__ = "存储"
    __ui_order__ = 1

    runtime_root_override: str = Field(default="", description="留空时使用 QQBot 公共业务数据根")


class DownloadSection(PluginConfigBase):
    __ui_label__ = "下载与缓存"
    __ui_order__ = 2

    proxy: str = Field(default="", description="可选 HTTP/HTTPS 代理；留空不继承系统代理")
    timeout_seconds: int = Field(default=1800, ge=60, le=7200, description="单作品下载超时秒数")
    max_pages_per_pdf: int = Field(default=500, ge=10, le=1000, description="单 PDF 最大页数")
    max_pdf_size_mb: int = Field(default=100, ge=10, le=500, description="单 PDF 最大 MiB")
    max_concurrent_jobs: int = Field(default=2, ge=1, le=2, description="不同作品最大并发数")
    max_queued_jobs: int = Field(default=50, ge=1, le=100, description="FIFO 下载队列上限")
    cache_max_gb: int = Field(default=10, ge=1, le=100, description="明文 PDF 缓存 LRU 上限 GiB")


class RoutingSection(PluginConfigBase):
    __ui_label__ = "好友路由"
    __ui_order__ = 3

    expected_workers: int = Field(default=1, ge=1, le=8, description="同一 QQ 消息预计到达的 MaiBot 实例数")
    rendezvous_wait_seconds: float = Field(default=1.5, ge=0.1, le=5.0, description="群聊好友能力汇合等待秒数")
    preferred_self_id: str = Field(default="", description="多只均为好友时优先执行的机器人 QQ")
    bot_account_ids: list[str] = Field(default_factory=list, description="不得触发 JM 命令的机器人 QQ")


class ComicConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    download: DownloadSection = Field(default_factory=DownloadSection)
    routing: RoutingSection = Field(default_factory=RoutingSection)


class QQBotComicPlugin(MaiBotPlugin):
    """MaiBot native JMComic download, cache, encryption, and delivery plugin."""

    config_model: ClassVar[type[PluginConfigBase] | None] = ComicConfig

    def __init__(self) -> None:
        super().__init__()
        self._service: ComicPdfService | None = None
        self._service_lock = asyncio.Lock()
        self._active_lock = asyncio.Lock()
        self._active_requests = 0

    async def on_load(self) -> None:
        self.ctx.logger.info("QQBot JM 漫画 PDF 插件已加载，启用=%s", self.config.plugin.enabled)

    async def on_unload(self) -> None:
        if self._service is not None:
            await self._service.shutdown()
            self._service = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            old = self._service
            self._service = None
            if old is not None:
                await old.shutdown()
            self.ctx.logger.info("QQBot JM 配置已更新: %s", version)

    @Command(
        "qqbot_comic",
        description="下载 JM 作品、缓存明文 PDF，并私聊发送密码加密副本",
        pattern=JM_COMMAND_PATTERN,
        timeout_ms=7_300_000,
    )
    async def handle_comic_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        del kwargs
        command_text = _text_segments(message) or text.strip()
        match = re.fullmatch(JM_COMMAND_PATTERN, command_text)
        if match is None:
            return False, "", False
        if not self.config.plugin.enabled:
            response = "JM 下载功能当前未启用。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        facts = _message_facts(message, group_id, user_id)
        if not facts["user_id"].isdigit() or facts["user_id"] in self._bot_ids():
            return True, "", True
        if not facts["self_id"]:
            raise ValueError("JM 命令缺少当前机器人 self_id")

        runtime_root = await self._runtime_root()
        try:
            current_is_friend = await is_onebot_friend(self.ctx.api.call, int(facts["user_id"]))
        except Exception as exc:
            self.ctx.logger.warning("JM friend lookup failed: error_type=%s", type(exc).__name__)
            current_is_friend = False
        if facts["group_id"]:
            router = ComicFriendRouteCoordinator(
                runtime_root / ".maibot_locks" / "comic_friend_routes.sqlite3",
                expected_workers=self.config.routing.expected_workers,
                wait_seconds=self.config.routing.rendezvous_wait_seconds,
            )
            decision = await router.choose(
                comic_event_key(command_text, facts["user_id"], facts["group_id"], facts["timestamp"]),
                self_id=facts["self_id"],
                is_friend=current_is_friend,
                preferred_worker=self.config.routing.preferred_self_id.strip(),
            )
            if decision.selected_worker != facts["self_id"]:
                return True, "", True
            has_friend = decision.has_friend
        else:
            has_friend = current_is_friend
        if not await self._claim(command_text, facts):
            return True, "", True
        if not has_friend:
            response = "需要先添加任意一只当前可用机器人为好友，再重新发送。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True

        capacity = self.config.download.max_concurrent_jobs + self.config.download.max_queued_jobs
        async with self._active_lock:
            if self._active_requests >= capacity:
                response = "JM 下载队列已满，请稍后再试。"
                await self.ctx.send.text(response, stream_id)
                return True, response, True
            self._active_requests += 1

        delivery = None
        try:
            service = await self._get_service(runtime_root)
            submission = await service.submit(match.group(1))
            status = _status_text(submission.status, match.group(1), submission.queue_position, self.config.download.max_concurrent_jobs)
            await self.ctx.send.text(status, stream_id)
            entry = await submission.wait()
            if not await is_onebot_friend(self.ctx.api.call, int(facts["user_id"])):
                raise ComicPdfError("发送前检测到好友关系已失效，请重新添加好友后再试。")
            delivery = await service.create_delivery(entry)
            await send_private_pdfs_with_password(
                self.ctx.api.call,
                int(facts["user_id"]),
                match.group(1),
                delivery.artifacts,
                title=entry.title,
                author=entry.author,
                tags=entry.tags,
            )
            return True, status, True
        except ComicQueueFullError as exc:
            response = str(exc)
        except ComicPdfError as exc:
            response = str(exc)
        except Exception as exc:
            self.ctx.logger.error("JM PDF task failed: album_id=%s error_type=%s", match.group(1), type(exc).__name__)
            response = "JM 下载、加密或私聊文件发送失败，请稍后重试。"
        finally:
            if delivery is not None:
                await asyncio.to_thread(delivery.cleanup)
            async with self._active_lock:
                self._active_requests = max(0, self._active_requests - 1)
        await self.ctx.send.text(response, stream_id)
        return True, response, True

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="comic_pdf",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "JM 命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("JM 命令仲裁返回格式无效")
        return bool(payload.get("claimed"))

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

    async def _get_service(self, runtime_root: Path) -> ComicPdfService:
        async with self._service_lock:
            if self._service is None:
                cfg = self.config.download
                service_config = ComicPdfConfig(
                    enabled=True,
                    proxy=cfg.proxy.strip(),
                    timeout_seconds=cfg.timeout_seconds,
                    max_pages_per_pdf=cfg.max_pages_per_pdf,
                    max_pdf_bytes=cfg.max_pdf_size_mb * 1024 * 1024,
                    max_concurrent_jobs=cfg.max_concurrent_jobs,
                    max_queued_jobs=cfg.max_queued_jobs,
                    cache_max_bytes=cfg.cache_max_gb * 1024 * 1024 * 1024,
                )
                self._service = ComicPdfService(
                    Path(self.ctx.paths.runtime_dir).resolve() / "jmcomic",
                    service_config,
                    cache_root=runtime_root / "comic_pdf_cache",
                )
            return self._service

    def _bot_ids(self) -> set[str]:
        return {str(item).strip() for item in self.config.routing.bot_account_ids if str(item).strip()}


def _status_text(status: str, album_id: str, queue_position: int, concurrency: int) -> str:
    low, high = _estimate_minutes(queue_position, concurrency)
    if status == "cache_hit":
        return f"JM{album_id} 缓存命中，开始处理。"
    if status == "started":
        return f"JM{album_id} 开始下载，预计约 {low}-{high} 分钟完成。"
    if status == "shared":
        queued = f"，当前排队第 {queue_position} 个" if queue_position > 0 else ""
        return f"JM{album_id} 正在下载，已加入等待{queued}，预计约 {low}-{high} 分钟完成。"
    if status == "queued":
        return f"JM{album_id} 开始下载，当前排队第 {queue_position} 个，预计约 {low}-{high} 分钟完成。"
    return f"JM{album_id} 开始处理。"


def _estimate_minutes(queue_position: int, concurrency: int) -> tuple[int, int]:
    workers = max(1, int(concurrency))
    position = max(0, int(queue_position))
    batches = 1 + ((position + workers - 1) // workers if position else 0)
    return 5 * batches, 15 * batches


def _message_facts(message: object, group_id: str, user_id: str) -> dict[str, Any]:
    payload = message if isinstance(message, Mapping) else {}
    info = payload.get("message_info")
    info = info if isinstance(info, Mapping) else {}
    group = info.get("group_info")
    user = info.get("user_info")
    additional = info.get("additional_config")
    group = group if isinstance(group, Mapping) else {}
    user = user if isinstance(user, Mapping) else {}
    additional = additional if isinstance(additional, Mapping) else {}
    targets: list[str] = []
    raw = payload.get("raw_message")
    if isinstance(raw, list):
        for segment in raw:
            if not isinstance(segment, Mapping) or segment.get("type") != "at":
                continue
            data = segment.get("data")
            target = str(data.get("target_user_id") or "").strip() if isinstance(data, Mapping) else ""
            if target and target not in targets:
                targets.append(target)
    try:
        timestamp = float(payload.get("timestamp") or time.time())
    except (TypeError, ValueError):
        timestamp = time.time()
    return {
        "group_id": str(group.get("group_id") or group_id or "").strip(),
        "user_id": str(user.get("user_id") or user_id or "").strip(),
        "self_id": str(additional.get("self_id") or "").strip(),
        "timestamp": timestamp,
        "at_target_ids": targets,
    }


def _text_segments(message: object) -> str:
    if not isinstance(message, Mapping) or not isinstance(message.get("raw_message"), list):
        return ""
    parts: list[str] = []
    for segment in message["raw_message"]:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        parts.append(str(data.get("text") or data.get("content") or "") if isinstance(data, Mapping) else str(data or ""))
    return "".join(parts).strip()


def create_plugin() -> QQBotComicPlugin:
    return QQBotComicPlugin()
