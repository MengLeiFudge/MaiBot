from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping

import asyncio
import base64
from datetime import timedelta
import re
import time

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from qqbot_common.api_results import require_api_result

from .apk_update_service import ArcApkUpdateManager
from .apk_update_service import fetch_latest_arc_version
from .arcaea_record_apk_downloader import ArcaeaRecordApkDownloader
from .artifact_client import publish_apk_to_group
from .background_service import ArcBackgroundService
from .background_service import ArcBackgroundStore
from .background_service import ArcKnowledgeSyncService
from .arc_logic import ArcCatalog
from .arc_logic import ArcEventService
from .arc_logic import ArcGuessGame
from .arc_logic import ArcMessage
from .arc_logic import parse_art_grid
from .arc_logic import parse_guess_count
from .arc_logic import parse_recommend_ptt
from .storage import ArcSessionStore


ARC_COMMAND_PATTERN = (
    r"(?i)^(?:@\S+\s*)?(?:"
    r"arctj\s*[0-9]+(?:\.[0-9]+)?|arc(?:hd|tz)|"
    r"(?:arczm|zm)(?:\s*[1-9][0-9]*)?|"
    r"(?:arcqh|qh)(?:\s*(?:[1-9][0-9]*|max|bt|补图))?|"
    r"(?:arcjx|jx)|(?:xz|arcxz))$"
)
APK_PATTERN = re.compile(r"^(?:xz|arcxz)$", re.I)
ACTIVITY_PATTERN = re.compile(r"^arc(?:hd|tz)$", re.I)
REVEAL_PATTERN = re.compile(r"^(?:arcjx|jx)$", re.I)
ART_OPEN_PATTERN = re.compile(r"^(?:arcqh|qh)\s*(?:bt|补图)$", re.I)


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否注册 ARC 固定命令与会话 Hook")
    config_version: str = Field(default="0.4.0", description="配置版本")


class CutoverSection(PluginConfigBase):
    __ui_label__ = "迁移接管"
    __ui_order__ = 1

    write_enabled: bool = Field(default=True, description="ARC 固定命令和会话状态已由本插件接管")


class StorageSection(PluginConfigBase):
    __ui_label__ = "存储"
    __ui_order__ = 2

    runtime_root_override: str = Field(default="", description="留空时使用 QQBot 公共插件的数据根")
    database_path: str = Field(default="", description="共享 SQLite 路径；留空写入运行根 db/qqbot_features.sqlite3")
    cache_root: str = Field(default="", description="猜歌面板缓存根；留空使用插件 runtime_dir/qqbot_arc")


class AssetsSection(PluginConfigBase):
    __ui_label__ = "Arcaea 资产"
    __ui_order__ = 3

    assets_root: str = Field(default="", description="含官谱/songlist 和曲绘目录的静态资产根")
    aliases_path: str = Field(default="", description="可选的只读静态曲名别名 JSON")
    timezone: str = Field(default="Asia/Shanghai", description="活动时间显示时区")
    session_timeout_seconds: int = Field(default=300, ge=30, le=3600, description="猜歌会话无操作超时")


class ApkSection(PluginConfigBase):
    __ui_label__ = "Arcaea APK"
    __ui_order__ = 4

    author_qq: str = Field(default="605738729", description="唯一可执行 xz/arcxz 的 QQ")
    arcaea_record_root: str = Field(default="", description="arcaeaRecord Maven 项目目录")
    artifact_root: str = Field(default="", description="APK 产物目录；留空使用插件 runtime_dir/qqbot_arc/apk")
    maven_command: str = Field(default="", description="可选 Maven 可执行文件路径")
    java_home: str = Field(default="", description="可选 JAVA_HOME")
    version_query_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0, description="官网版本查询超时")
    compile_timeout_seconds: float = Field(default=180.0, ge=10.0, le=900.0, description="arcaeaRecord 编译超时")
    download_timeout_seconds: float = Field(default=900.0, ge=30.0, le=3600.0, description="APK 下载超时")


class ArtifactPublishSection(PluginConfigBase):
    __ui_label__ = "安装包发布"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="下载完成后通过本地产物 API 发布到当前群文件")
    endpoint: str = Field(
        default="http://127.0.0.1:8080/admin/api/artifacts/publish-local",
        description="仅允许回环地址的本地产物发布接口",
    )
    timeout_seconds: float = Field(default=300.0, ge=5.0, le=600.0, description="群文件发布请求超时")


class BackgroundSection(PluginConfigBase):
    __ui_label__ = "ARC 后台"
    __ui_order__ = 6

    enabled: bool = Field(default=True, description="启用 ARC 后台同步、过期处理和活动提醒")
    loop_interval_seconds: float = Field(default=60.0, ge=10.0, le=3600.0, description="后台循环周期")
    aliases_enabled: bool = Field(default=True, description="每日同步曲名别名")
    constants_enabled: bool = Field(default=True, description="每日补齐谱面定数")
    version_check_enabled: bool = Field(default=True, description="定期查询官网版本")
    guess_expiration_enabled: bool = Field(default=True, description="清理并揭晓过期猜歌")
    activity_reminders_enabled: bool = Field(default=True, description="每日发送活动提醒")
    alias_sync_interval_hours: float = Field(default=24.0, ge=1.0, le=168.0, description="别名同步间隔")
    constants_sync_interval_hours: float = Field(default=24.0, ge=1.0, le=168.0, description="定数同步间隔")
    version_check_interval_hours: float = Field(default=12.0, ge=1.0, le=168.0, description="版本查询间隔")
    activity_check_interval_hours: float = Field(default=1.0, ge=0.25, le=24.0, description="活动提醒查询间隔")
    reminder_group_ids: list[str] = Field(default_factory=list, description="活动提醒群；留空使用当前 bot 群列表")


class RoutingSection(PluginConfigBase):
    __ui_label__ = "路由"
    __ui_order__ = 7

    bot_account_ids: list[str] = Field(default_factory=list, description="不得触发 ARC 会话的机器人 QQ")


class ArcConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    cutover: CutoverSection = Field(default_factory=CutoverSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    assets: AssetsSection = Field(default_factory=AssetsSection)
    apk: ApkSection = Field(default_factory=ApkSection)
    artifact_publish: ArtifactPublishSection = Field(default_factory=ArtifactPublishSection)
    background: BackgroundSection = Field(default_factory=BackgroundSection)
    routing: RoutingSection = Field(default_factory=RoutingSection)


class QQBotArcPlugin(MaiBotPlugin):
    """MaiBot native Arc recommendation, event and guessing plugin."""

    config_model: ClassVar[type[PluginConfigBase] | None] = ArcConfig

    async def on_load(self) -> None:
        self._arc_apk_update_manager: ArcApkUpdateManager | None = None
        self._arc_background_service: ArcBackgroundService | None = None
        self._arc_background_task: asyncio.Task[None] | None = None
        if self.config.background.enabled and self.config.cutover.write_enabled:
            self._arc_background_task = asyncio.create_task(
                self._background_loop(),
                name="qqbot-arc-background",
            )
        self.ctx.logger.info(
            "QQBot ARC 插件已加载，启用=%s，业务写入=%s",
            self.config.plugin.enabled,
            self.config.cutover.write_enabled,
        )

    async def on_unload(self) -> None:
        await self._stop_background()
        manager = getattr(self, "_arc_apk_update_manager", None)
        if manager is not None:
            await manager.shutdown()
        self._arc_apk_update_manager = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self._stop_background()
            manager = getattr(self, "_arc_apk_update_manager", None)
            if manager is not None:
                await manager.shutdown()
            self._arc_apk_update_manager = None
            self._arc_background_service = None
            if self.config.background.enabled and self.config.cutover.write_enabled:
                self._arc_background_task = asyncio.create_task(
                    self._background_loop(),
                    name="qqbot-arc-background",
                )
            self.ctx.logger.info("QQBot ARC 配置已更新: %s", version)

    @Command(
        "qqbot_arc",
        description="Arcaea 推荐、活动和猜歌入口",
        pattern=ARC_COMMAND_PATTERN,
        timeout_ms=600000,
    )
    async def handle_arc_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        del kwargs
        command_text = _text_segments(message) or _strip_mention(text)
        facts = _message_facts(message, group_id, user_id)
        if facts["user_id"] in self._bot_ids():
            return True, "", True
        if not facts["self_id"]:
            response = "ARC 命令暂时无法确认当前机器人身份。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        try:
            claimed = await self._claim(command_text, facts, "arc_command")
        except Exception:
            response = "ARC 命令仲裁失败，请稍后重试。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not claimed:
            return True, "", True
        if not self.config.plugin.enabled:
            response = "ARC 功能当前未启用。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if not self.config.cutover.write_enabled:
            response = "ARC 功能接管当前已关闭。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        if APK_PATTERN.fullmatch(command_text):
            if facts["user_id"] != self.config.apk.author_qq.strip():
                response = "只有作者可以使用这个指令。"
                await self.ctx.send.text(response, stream_id)
                return True, response, True
            try:
                runtime_root = await self._runtime_root()
                manager = self._apk_manager(runtime_root)
                response = await manager.query_and_update()
                response = await self._append_apk_publish_result(response, manager, facts)
            except Exception:
                response = "Arc 安装包下载查询失败，请检查本机 APK 下载配置。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True

        if ACTIVITY_PATTERN.fullmatch(command_text):
            try:
                messages = await asyncio.to_thread(ArcEventService(self.config.assets.timezone).messages)
            except Exception:
                response = "Arc 活动梯子查询失败，请稍后重试。"
                await self.ctx.send.text(response, stream_id)
                return True, response, True
            response = "\n\n".join(messages or ["当前没有活动梯子。"])
            await self.ctx.send.text(response, stream_id)
            return True, response, True

        try:
            runtime_root = await self._runtime_root()
            game = self._game(runtime_root)
            ptt = parse_recommend_ptt(command_text)
            if ptt is not None:
                try:
                    catalog = self._catalog(runtime_root)
                    chart = await asyncio.to_thread(catalog.recommend, ptt)
                    result = ArcMessage(catalog.recommendation_text(ptt, chart), chart.jacket_path)
                    await self._send(result, stream_id, facts)
                except Exception:
                    response = "Arc 推荐失败，本地曲库或曲绘当前不可用。"
                    await self.ctx.send.text(response, stream_id)
                    return True, response, True
                return True, result.text, True
            count = parse_guess_count(command_text)
            if count is not None:
                if not facts["group_id"]:
                    response = "Arc 猜歌只能在群聊中使用。"
                    await self.ctx.send.text(response, stream_id)
                    return True, response, True
                result = await asyncio.to_thread(game.start_letters, facts["group_id"], count)
                await self._send(result, stream_id, facts)
                return True, result.text, True
            if REVEAL_PATTERN.fullmatch(command_text):
                if not facts["group_id"]:
                    response = "Arc 猜歌只能在群聊中使用。"
                    await self.ctx.send.text(response, stream_id)
                    return True, response, True
                result = await asyncio.to_thread(game.reveal, facts["group_id"])
                await self._send(result, stream_id, facts)
                return True, result.text, True
            if ART_OPEN_PATTERN.fullmatch(command_text):
                if not facts["group_id"]:
                    response = "Arc 猜歌只能在群聊中使用。"
                    await self.ctx.send.text(response, stream_id)
                    return True, response, True
                result = await asyncio.to_thread(game.open_art, facts["group_id"])
                await self._send(result, stream_id, facts)
                return True, result.text, True
            grid_size = parse_art_grid(command_text)
            if grid_size is not None:
                if not facts["group_id"]:
                    response = "Arc 猜歌只能在群聊中使用。"
                    await self.ctx.send.text(response, stream_id)
                    return True, response, True
                result = await asyncio.to_thread(game.start_or_open_art, facts["group_id"], grid_size)
                await self._send(result, stream_id, facts)
                return True, result.text, True
        except Exception:
            response = "Arc 猜歌处理失败，请稍后重试。"
            await self.ctx.send.text(response, stream_id)
            return True, response, True
        response = "无法识别这个 ARC 固定指令。"
        await self.ctx.send.text(response, stream_id)
        return True, response, True

    @HookHandler(
        "chat.receive.before_process",
        name="qqbot_arc_guess_session_gate",
        description="在 Planner、普通聊天和 A-Memorix 前消费 ARC 猜歌答案",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=30000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_guess_answer(self, message: object = None, **kwargs: Any) -> dict[str, str]:
        del kwargs
        if not self.config.plugin.enabled:
            return {"action": "continue"}
        text = _text_segments(message)
        facts = _message_facts(message, "", "")
        if not text or not facts["group_id"] or facts["user_id"] in self._bot_ids():
            return {"action": "continue"}
        runtime_root = await self._runtime_root()
        game = self._game(runtime_root, initialize_store=self.config.cutover.write_enabled)
        session = await asyncio.to_thread(game.store.load, facts["group_id"])
        if session is None or not game.recognizes_answer(session, text):
            return {"action": "continue"}
        if not self.config.cutover.write_enabled:
            return {"action": "abort"}
        if not facts["self_id"] or not await self._claim(text, facts, "arc_session"):
            return {"action": "abort"}
        player_name = await self._display_name(facts["group_id"], facts["user_id"])
        result = await asyncio.to_thread(game.handle_answer, facts["group_id"], text, player_name)
        if result is not None:
            await self._send(result, _stream_id(message), facts)
        return {"action": "abort"}

    async def _claim(self, text: str, facts: dict[str, Any], feature: str) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature=feature,
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "ARC 命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("ARC 命令仲裁返回格式无效")
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

    async def _display_name(self, group_id: str, user_id: str) -> str:
        result = await self.ctx.api.call("qqbot.identity.display_name", group_id=group_id, user_id=user_id)
        try:
            payload = require_api_result(result, "解析 ARC 答题者显示名")
        except RuntimeError:
            return user_id
        return str(payload.get("display_name") or user_id) if isinstance(payload, Mapping) else user_id

    def _catalog(self, runtime_root: Path) -> ArcCatalog:
        assets_root = (
            Path(self.config.assets.assets_root).expanduser().resolve()
            if self.config.assets.assets_root.strip()
            else runtime_root / "data" / "arc"
        )
        aliases = Path(self.config.assets.aliases_path).expanduser().resolve() if self.config.assets.aliases_path.strip() else None
        return ArcCatalog(assets_root, aliases, self._database_path(runtime_root))

    def _game(self, runtime_root: Path, *, initialize_store: bool = True) -> ArcGuessGame:
        database = self._database_path(runtime_root)
        cache = (
            Path(self.config.storage.cache_root).expanduser().resolve()
            if self.config.storage.cache_root.strip()
            else Path(self.ctx.paths.runtime_dir).resolve() / "qqbot_arc"
        )
        return ArcGuessGame(
            self._catalog(runtime_root),
            ArcSessionStore(database, initialize=initialize_store),
            cache,
            timeout_seconds=float(self.config.assets.session_timeout_seconds),
        )

    def _database_path(self, runtime_root: Path) -> Path:
        return (
            Path(self.config.storage.database_path).expanduser().resolve()
            if self.config.storage.database_path.strip()
            else runtime_root / "db" / "qqbot_features.sqlite3"
        )

    async def _append_apk_publish_result(
        self,
        response: str,
        manager: ArcApkUpdateManager,
        facts: dict[str, Any],
    ) -> str:
        completed = manager.completed_artifact()
        if completed is None:
            return response
        version, artifact_path = completed
        if not facts["group_id"]:
            return response + "\n安装包已下载；群文件发布只能在群聊中执行。"
        if not self.config.artifact_publish.enabled:
            return response + "\n安装包已下载，但本地产物发布当前未启用。"
        try:
            result = await asyncio.to_thread(
                publish_apk_to_group,
                artifact_path,
                group_id=facts["group_id"],
                version=version,
                endpoint=self.config.artifact_publish.endpoint,
                timeout_seconds=float(self.config.artifact_publish.timeout_seconds),
            )
        except Exception as exc:
            self.ctx.logger.warning("ARC 安装包群文件发布失败: type=%s", type(exc).__name__)
            return response + "\n安装包已下载，但发布到当前群失败，请稍后重试。"
        if result.uploaded:
            return response + "\n已发布到当前群文件。"
        return response + "\n当前群已有相同安装包，无需重复上传。"

    def _apk_manager(self, runtime_root: Path) -> ArcApkUpdateManager:
        if getattr(self, "_arc_apk_update_manager", None) is None:
            project_root = self.config.apk.arcaea_record_root.strip()
            if not project_root:
                raise RuntimeError("未配置 apk.arcaea_record_root")
            artifact_root = (
                Path(self.config.apk.artifact_root).expanduser().resolve()
                if self.config.apk.artifact_root.strip()
                else Path(self.ctx.paths.runtime_dir).resolve() / "qqbot_arc" / "apk"
            )
            downloader = ArcaeaRecordApkDownloader(
                Path(project_root).expanduser().resolve(),
                artifact_root,
                maven_command=self.config.apk.maven_command.strip(),
                java_home=self.config.apk.java_home.strip(),
                compile_timeout_seconds=float(self.config.apk.compile_timeout_seconds),
                download_timeout_seconds=float(self.config.apk.download_timeout_seconds),
            )
            store = ArcBackgroundStore(self._database_path(runtime_root))
            self._arc_apk_update_manager = ArcApkUpdateManager(
                lambda: fetch_latest_arc_version(float(self.config.apk.version_query_timeout_seconds)),
                downloader,
                downloaded_callback=lambda version: store.update(version_last_downloaded=version),
            )
        return self._arc_apk_update_manager

    async def _background_loop(self) -> None:
        interval = float(self.config.background.loop_interval_seconds)
        while True:
            try:
                service = await self._get_background_service()
                await service.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.warning("QQBot ARC 后台循环失败: type=%s", type(exc).__name__)
            await asyncio.sleep(interval)

    async def _stop_background(self) -> None:
        task = getattr(self, "_arc_background_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            self.ctx.logger.warning("QQBot ARC 后台任务取消超过 10 秒")
        self._arc_background_task = None

    async def _get_background_service(self) -> ArcBackgroundService:
        existing = getattr(self, "_arc_background_service", None)
        if existing is not None:
            return existing
        runtime_root = await self._runtime_root()
        store = ArcBackgroundStore(self._database_path(runtime_root))
        catalog = self._catalog(runtime_root)
        knowledge = ArcKnowledgeSyncService(catalog.assets_root, store)
        game = self._game(runtime_root)
        config = self.config.background
        service = ArcBackgroundService(
            store,
            version_fetcher=lambda: fetch_latest_arc_version(float(self.config.apk.version_query_timeout_seconds)),
            alias_sync=knowledge.sync_aliases,
            constants_sync=knowledge.sync_missing_constants,
            expire_sessions=lambda timestamp: game.expire_sessions(now=timestamp),
            event_messages=lambda now: ArcEventService(self.config.assets.timezone).messages(now=now),
            list_group_ids=self._background_group_ids,
            send_group=self._background_send_group,
            timezone_name=self.config.assets.timezone,
            alias_interval=timedelta(hours=float(config.alias_sync_interval_hours)),
            constants_interval=timedelta(hours=float(config.constants_sync_interval_hours)),
            version_interval=timedelta(hours=float(config.version_check_interval_hours)),
            activity_check_interval=timedelta(hours=float(config.activity_check_interval_hours)),
            reminder_group_ids=tuple(str(item).strip() for item in config.reminder_group_ids if str(item).strip()),
            aliases_enabled=config.aliases_enabled,
            constants_enabled=config.constants_enabled,
            version_check_enabled=config.version_check_enabled,
            guess_expiration_enabled=config.guess_expiration_enabled,
            activity_reminders_enabled=config.activity_reminders_enabled,
            logger=self.ctx.logger,
        )
        self._arc_background_service = service
        return service

    async def _background_group_ids(self) -> list[str]:
        response = await self.ctx.api.call("adapter.napcat.group.get_group_list", no_cache=False)
        payload = require_api_result(response, "ARC 后台读取群列表")
        if isinstance(payload, Mapping):
            groups = payload.get("groups")
            if groups is None:
                groups = payload.get("data", [])
        else:
            groups = payload
        if not isinstance(groups, list):
            raise RuntimeError("ARC 后台群列表返回格式无效")
        result: list[str] = []
        for group in groups:
            group_id = str(group.get("group_id") or "").strip() if isinstance(group, Mapping) else ""
            if group_id:
                result.append(group_id)
        return result

    async def _background_send_group(self, group_id: str, message: object) -> None:
        result = message if isinstance(message, ArcMessage) else ArcMessage(str(message))
        segments: list[dict[str, Any]] = []
        if result.image_path is not None:
            image_bytes = await asyncio.to_thread(result.image_path.read_bytes)
            segments.append(
                {
                    "type": "image",
                    "data": {
                        "file": "base64://" + base64.b64encode(image_bytes).decode("ascii"),
                        "summary": "Arc 后台消息",
                    },
                }
            )
        if result.text:
            segments.append({"type": "text", "data": {"text": result.text}})
        response = await self.ctx.api.call(
            "adapter.napcat.group.send_group_msg",
            params={"group_id": str(group_id), "message": segments},
        )
        require_api_result(response, "ARC 后台群消息发送")

    async def _send(self, result: ArcMessage, stream_id: str, facts: dict[str, Any]) -> None:
        if result.image_path is None:
            await self.ctx.send.text(result.text, stream_id)
            return
        image_bytes = await asyncio.to_thread(result.image_path.read_bytes)
        image_source = "base64://" + base64.b64encode(image_bytes).decode("ascii")
        segments = [
            {"type": "image", "data": {"file": image_source, "summary": "Arc 猜歌面板"}},
            {"type": "text", "data": {"text": f"\n{result.text}"}},
        ]
        if facts["group_id"]:
            api_name = "adapter.napcat.group.send_group_msg"
            params = {"group_id": facts["group_id"], "message": segments}
        else:
            api_name = "adapter.napcat.message.send_private_msg"
            params = {"user_id": facts["user_id"], "message": segments}
        response = await self.ctx.api.call(api_name, params=params)
        require_api_result(response, "ARC 图片发送")

    def _bot_ids(self) -> set[str]:
        return {str(item).strip() for item in self.config.routing.bot_account_ids if str(item).strip()}


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
    if not isinstance(message, Mapping):
        return ""
    raw = message.get("raw_message")
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for segment in raw:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        parts.append(str(data.get("text") or data.get("content") or "") if isinstance(data, Mapping) else str(data or ""))
    return "".join(parts).strip()


def _strip_mention(text: str) -> str:
    return re.sub(r"^@\S+\s*", "", text.strip(), count=1)


def _stream_id(message: object) -> str:
    return str(message.get("session_id") or "").strip() if isinstance(message, Mapping) else ""


def create_plugin() -> QQBotArcPlugin:
    return QQBotArcPlugin()
