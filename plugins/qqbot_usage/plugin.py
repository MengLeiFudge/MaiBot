from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping

import asyncio
import base64
import re
import secrets
import time

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase
from qqbot_common.api_results import require_api_result

from .codexradar_efficiency import CODEXRADAR_EFFICIENCY_URL
from .codexradar_efficiency import fetch_codexradar_efficiency
from .codexradar_efficiency import render_codexradar_efficiency_image
from .sub2api_usage import Sub2APIAccountSevenDayRanking
from .sub2api_usage import Sub2APIAccountUsage
from .sub2api_usage import Sub2APIClient
from .sub2api_usage import Sub2APIUsageAlert
from .sub2api_usage import Sub2APIUsageCache
from .sub2api_usage import Sub2APIUsageSnapshot
from .sub2api_usage import Sub2APIUserUsage
from .sub2api_usage import format_sub2api_usage_alert_message
from .sub2api_usage import format_sub2api_usage_response
from .sub2api_usage import retain_failed_account_ranking
from .sub2api_usage import update_sub2api_usage_alert_state
from .sub2api_usage_image import render_sub2api_usage_image


USAGE_COMMAND_PATTERN = r"^(?:@\S+\s*)?用量$"
_FIRST_REFRESH_TEXT = "Sub2API 用量后台正在首次刷新，稍后再发“用量”即可返回当前报告。"
_NOT_CONFIGURED_TEXT = "Sub2API 用量查询尚未配置。"
_IMAGE_SUMMARIES = ("用量报告", "额度报告", "统计已更新", "数据面板", "当前用量")


class PluginSection(PluginConfigBase):
    """Usage plugin registration settings."""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否注册 Sub2API 用量固定命令")
    config_version: str = Field(default="0.1.0", description="配置版本")


class Sub2APISection(PluginConfigBase):
    """Sub2API endpoint and refresh settings."""

    __ui_label__ = "Sub2API"
    __ui_order__ = 1

    base_url: str = Field(default="", description="Sub2API 根地址")
    admin_api_key: str = Field(
        default="",
        description="Sub2API Admin API Key",
        json_schema_extra={"x-widget": "password", "x-icon": "key"},
    )
    timeout_seconds: float = Field(default=90.0, ge=1.0, le=300.0, description="单次请求超时秒数")
    refresh_interval_seconds: float = Field(default=300.0, ge=60.0, le=3600.0, description="后台刷新周期秒数")


class CodexRadarSection(PluginConfigBase):
    """Optional public CodexRadar report settings."""

    __ui_label__ = "CodexRadar"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="是否附加 CodexRadar 智力效率报告")
    url: str = Field(default=CODEXRADAR_EFFICIENCY_URL, description="公开效率数据地址")
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0, description="公开数据请求超时秒数")
    refresh_interval_seconds: float = Field(default=300.0, ge=60.0, le=3600.0, description="后台刷新周期秒数")


class AlertSection(PluginConfigBase):
    """Optional proactive quota alerts."""

    __ui_label__ = "用量提醒"
    __ui_order__ = 3

    enabled: bool = Field(default=False, description="是否发送 5h 用量阈值提醒")
    group_ids: list[str] = Field(default_factory=list, description="提醒目标群号")


class StorageSection(PluginConfigBase):
    """Rebuildable image cache settings."""

    __ui_label__ = "缓存"
    __ui_order__ = 4

    cache_root_override: str = Field(default="", description="留空时使用插件 runtime_dir")


class UsageConfig(PluginConfigBase):
    """Complete usage plugin configuration."""

    plugin: PluginSection = Field(default_factory=PluginSection)
    sub2api: Sub2APISection = Field(default_factory=Sub2APISection)
    codexradar: CodexRadarSection = Field(default_factory=CodexRadarSection)
    alerts: AlertSection = Field(default_factory=AlertSection)
    storage: StorageSection = Field(default_factory=StorageSection)


class QQBotUsagePlugin(MaiBotPlugin):
    """Serve cached Sub2API and CodexRadar reports before the chat pipeline."""

    config_model: ClassVar[type[PluginConfigBase] | None] = UsageConfig

    def __init__(self) -> None:
        super().__init__()
        self._usage_cache = Sub2APIUsageCache()
        self._usage_refresh_task: asyncio.Task[None] | None = None
        self._codexradar_refresh_task: asyncio.Task[None] | None = None
        self._codexradar_image_path: Path | None = None
        self._alerted_thresholds_by_account: dict[str, set[int]] = {}

    async def on_load(self) -> None:
        if self.config.plugin.enabled and self._is_configured():
            self._start_background_tasks()
        self.ctx.logger.info(
            "QQBot 用量插件已加载，启用=%s，Sub2API已配置=%s，CodexRadar=%s",
            self.config.plugin.enabled,
            self._is_configured(),
            self.config.codexradar.enabled,
        )

    async def on_unload(self) -> None:
        await self._stop_background_tasks()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        await self._stop_background_tasks()
        self._usage_cache = Sub2APIUsageCache()
        self._codexradar_image_path = None
        if self.config.plugin.enabled and self._is_configured():
            self._start_background_tasks()
        self.ctx.logger.info("QQBot 用量配置已更新: %s", version)

    @Command(
        "qqbot_usage",
        description="查询 Sub2API 账号额度、消费榜和 CodexRadar 智力效率",
        pattern=USAGE_COMMAND_PATTERN,
        timeout_ms=30000,
    )
    async def handle_usage_command(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        message: object = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Claim and answer one exact usage command without entering Planner."""

        del stream_id, kwargs
        started_at = time.monotonic()
        command_text = _text_segments(message) or _strip_leading_mention(text)
        if not self.config.plugin.enabled:
            return True, "", True

        facts = _message_facts(message, group_id, user_id)
        if not facts["self_id"]:
            raise ValueError("用量命令缺少当前机器人 self_id")
        if not await self._claim(command_text, facts):
            return True, "", True

        if not self._is_configured():
            await self._send(group_id, user_id, text=_NOT_CONFIGURED_TEXT)
            self._log_command_result("not_configured", group_id, started_at)
            return True, _NOT_CONFIGURED_TEXT, True

        snapshot = self._usage_cache.get_latest()
        if not _snapshot_ready(snapshot):
            await self._send(group_id, user_id, text=_FIRST_REFRESH_TEXT)
            self._log_command_result("refresh_pending", group_id, started_at)
            return True, _FIRST_REFRESH_TEXT, True

        assert snapshot is not None
        try:
            image_paths = await self._render_report_images(snapshot)
            await self._send(group_id, user_id, image_paths=image_paths)
            self._log_command_result(f"images_{len(image_paths)}", group_id, started_at)
            return True, "Sub2API 用量报告", True
        except Exception:
            self.ctx.logger.exception("用量报告图片渲染或发送失败，回退纯文本")
            response = format_sub2api_usage_response(snapshot)
            await self._send(group_id, user_id, text=response)
            self._log_command_result("text_fallback", group_id, started_at)
            return True, response, True

    def _log_command_result(self, status: str, group_id: str, started_at: float) -> None:
        self.ctx.logger.info(
            "用量命令已消费: chat_type=%s, status=%s, duration_ms=%.1f",
            "group" if group_id else "private",
            status,
            (time.monotonic() - started_at) * 1000,
        )

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="sub2api_usage",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "用量命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("用量命令仲裁返回格式无效")
        return bool(payload.get("claimed"))

    def _is_configured(self) -> bool:
        return bool(self.config.sub2api.base_url.strip() and self.config.sub2api.admin_api_key.strip())

    def _start_background_tasks(self) -> None:
        if self._usage_refresh_task is None or self._usage_refresh_task.done():
            self._usage_refresh_task = asyncio.create_task(
                self._usage_refresh_loop(),
                name="qqbot-usage-sub2api-refresh",
            )
        if self.config.codexradar.enabled and (
            self._codexradar_refresh_task is None or self._codexradar_refresh_task.done()
        ):
            self._codexradar_refresh_task = asyncio.create_task(
                self._codexradar_refresh_loop(),
                name="qqbot-usage-codexradar-refresh",
            )

    async def _stop_background_tasks(self) -> None:
        tasks = [task for task in (self._usage_refresh_task, self._codexradar_refresh_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._usage_refresh_task = None
        self._codexradar_refresh_task = None

    def _new_sub2api_client(self) -> Sub2APIClient:
        return Sub2APIClient(
            base_url=self.config.sub2api.base_url,
            admin_api_key=self.config.sub2api.admin_api_key,
            timeout_seconds=self.config.sub2api.timeout_seconds,
        )

    async def _refresh_account_ranking(
        self,
        account: Sub2APIAccountUsage,
        users: tuple[Sub2APIUserUsage, ...],
        previous: Sub2APIAccountSevenDayRanking | None,
        *,
        now: datetime,
    ) -> Sub2APIAccountSevenDayRanking:
        last_error = ""
        for _ in range(2):
            try:
                ranking_users = await self._new_sub2api_client().get_account_seven_day_ranking(
                    account,
                    users,
                    now=now,
                )
                return Sub2APIAccountSevenDayRanking(
                    account_id=account.account_id,
                    users=ranking_users,
                    refreshed_at=datetime.now(timezone.utc),
                )
            except Exception as exc:
                last_error = str(exc)
        return retain_failed_account_ranking(account.account_id, previous, last_error)

    async def _refresh_users_once(self) -> Sub2APIUsageSnapshot:
        previous = self._usage_cache.get_latest()
        accounts = previous.accounts if previous is not None else ()
        rankings = previous.account_seven_day_rankings if previous is not None else ()
        users = previous.users if previous is not None else ()
        accounts_refreshed_at = previous.accounts_refreshed_at if previous is not None else None
        users_refreshed_at = previous.users_refreshed_at if previous is not None else None
        accounts_error = previous.accounts_error if previous is not None else ""
        users_error = ""
        for attempt in range(2):
            try:
                users = tuple(await self._new_sub2api_client().get_user_usage_ranking())
                users_refreshed_at = datetime.now(timezone.utc)
                break
            except Exception as exc:
                users_error = str(exc)
                if attempt == 1:
                    break
        snapshot = Sub2APIUsageSnapshot(
            accounts=accounts,
            account_seven_day_rankings=rankings,
            users=users,
            accounts_refreshed_at=accounts_refreshed_at,
            users_refreshed_at=users_refreshed_at,
            accounts_error=accounts_error,
            users_error=users_error,
        )
        self._usage_cache.set(snapshot)
        return snapshot

    async def _refresh_accounts_once(self) -> Sub2APIUsageSnapshot:
        previous = self._usage_cache.get_latest()
        accounts = previous.accounts if previous is not None else ()
        rankings = previous.account_seven_day_rankings if previous is not None else ()
        users = previous.users if previous is not None else ()
        accounts_refreshed_at = previous.accounts_refreshed_at if previous is not None else None
        users_refreshed_at = previous.users_refreshed_at if previous is not None else None
        users_error = previous.users_error if previous is not None else ""
        accounts_error = ""
        for attempt in range(2):
            try:
                accounts = tuple(await self._new_sub2api_client().get_account_usage(force_refresh=True))
                accounts_refreshed_at = datetime.now(timezone.utc)
                break
            except Exception as exc:
                accounts_error = str(exc)
                if attempt == 1:
                    break
        if not accounts_error:
            previous_by_account = {ranking.account_id: ranking for ranking in rankings}
            ranking_now = datetime.now(timezone.utc)
            refreshed_rankings: list[Sub2APIAccountSevenDayRanking] = []
            for account in accounts:
                refreshed_rankings.append(
                    await self._refresh_account_ranking(
                        account,
                        users,
                        previous_by_account.get(account.account_id),
                        now=ranking_now,
                    )
                )
            rankings = tuple(refreshed_rankings)
        snapshot = Sub2APIUsageSnapshot(
            accounts=accounts,
            account_seven_day_rankings=rankings,
            users=users,
            accounts_refreshed_at=accounts_refreshed_at,
            users_refreshed_at=users_refreshed_at,
            accounts_error=accounts_error,
            users_error=users_error,
        )
        self._usage_cache.set(snapshot)
        return snapshot

    async def _usage_refresh_loop(self) -> None:
        interval = max(60.0, self.config.sub2api.refresh_interval_seconds)
        phase_interval = max(30.0, interval / 2)
        try:
            users_snapshot = await self._refresh_users_once()
            self.ctx.logger.info(
                "Sub2API 用户用量缓存首次刷新完成，用户=%s，失败=%s",
                len(users_snapshot.users),
                bool(users_snapshot.users_error),
            )
            accounts_snapshot = await self._refresh_accounts_once()
            await self._handle_alerts(accounts_snapshot)
            self.ctx.logger.info(
                "Sub2API 账号用量缓存首次刷新完成，账号=%s，失败=%s",
                len(accounts_snapshot.accounts),
                bool(accounts_snapshot.accounts_error),
            )
            while True:
                await asyncio.sleep(phase_interval)
                users_snapshot = await self._refresh_users_once()
                self.ctx.logger.info(
                    "Sub2API 用户用量缓存已刷新，用户=%s，失败=%s",
                    len(users_snapshot.users),
                    bool(users_snapshot.users_error),
                )
                await asyncio.sleep(phase_interval)
                accounts_snapshot = await self._refresh_accounts_once()
                await self._handle_alerts(accounts_snapshot)
                ranking_errors = sum(1 for ranking in accounts_snapshot.account_seven_day_rankings if ranking.error)
                self.ctx.logger.info(
                    "Sub2API 账号用量缓存已刷新，账号=%s，失败=%s，7d榜失败=%s",
                    len(accounts_snapshot.accounts),
                    bool(accounts_snapshot.accounts_error),
                    ranking_errors,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.ctx.logger.exception("Sub2API 用量后台刷新循环异常")

    async def _codexradar_refresh_loop(self) -> None:
        interval = max(60.0, self.config.codexradar.refresh_interval_seconds)
        while True:
            try:
                snapshot = await asyncio.to_thread(
                    fetch_codexradar_efficiency,
                    url=self.config.codexradar.url,
                    timeout_seconds=self.config.codexradar.timeout_seconds,
                )
                self._codexradar_image_path = await asyncio.to_thread(
                    render_codexradar_efficiency_image,
                    snapshot=snapshot,
                    output_dir=self._cache_root() / "codexradar",
                )
                self.ctx.logger.info(
                    "CodexRadar 效率缓存已刷新，数据点=%s，24h作答=%s",
                    len(snapshot.points),
                    snapshot.runs_24h_total,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.warning("CodexRadar 效率缓存刷新失败: %s", exc)
            await asyncio.sleep(interval)

    async def _handle_alerts(self, snapshot: Sub2APIUsageSnapshot) -> None:
        if not self.config.alerts.enabled or not self.config.alerts.group_ids or snapshot.accounts_error:
            return
        alerts = update_sub2api_usage_alert_state(
            list(snapshot.accounts),
            self._alerted_thresholds_by_account,
        )
        for alert in alerts:
            await self._send_alert(alert)

    async def _send_alert(self, alert: Sub2APIUsageAlert) -> None:
        text = format_sub2api_usage_alert_message(alert)
        for group_id in self.config.alerts.group_ids:
            normalized_group_id = str(group_id).strip()
            if not normalized_group_id.isdigit():
                continue
            try:
                result = await self.ctx.api.call(
                    "adapter.napcat.group.send_group_msg",
                    params={
                        "group_id": normalized_group_id,
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                )
                require_api_result(result, "发送 Sub2API 用量提醒")
            except Exception as exc:
                self.ctx.logger.warning(
                    "发送 Sub2API 用量提醒失败: group_id=%s threshold=%s error=%s",
                    normalized_group_id,
                    alert.threshold,
                    exc,
                )

    async def _render_report_images(self, snapshot: Sub2APIUsageSnapshot) -> list[Path]:
        usage_path = await asyncio.to_thread(
            render_sub2api_usage_image,
            snapshot=snapshot,
            output_dir=self._cache_root() / "sub2api",
        )
        paths = [usage_path]
        radar_path = self._codexradar_image_path
        if radar_path is not None and radar_path.is_file():
            paths.append(radar_path)
        return paths

    def _cache_root(self) -> Path:
        override = self.config.storage.cache_root_override.strip()
        if override:
            return Path(override).expanduser().resolve()
        return Path(self.ctx.paths.runtime_dir).resolve()

    async def _send(
        self,
        group_id: str,
        user_id: str,
        *,
        text: str = "",
        image_paths: list[Path] | None = None,
    ) -> None:
        segments: list[dict[str, object]] = []
        for image_path in image_paths or []:
            image_data = await asyncio.to_thread(image_path.read_bytes)
            segments.append(
                {
                    "type": "image",
                    "data": {
                        "file": "base64://" + base64.b64encode(image_data).decode("ascii"),
                        "summary": secrets.choice(_IMAGE_SUMMARIES),
                    },
                }
            )
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        if group_id:
            api_name = "adapter.napcat.group.send_group_msg"
            params = {"group_id": group_id, "message": segments}
        else:
            api_name = "adapter.napcat.message.send_private_msg"
            params = {"user_id": user_id, "message": segments}
        result = await self.ctx.api.call(api_name, params=params)
        require_api_result(result, "发送用量报告")


def _snapshot_ready(snapshot: Sub2APIUsageSnapshot | None) -> bool:
    return bool(
        snapshot is not None
        and snapshot.accounts_refreshed_at is not None
        and snapshot.users_refreshed_at is not None
    )


def _message_facts(message: object, group_id: str, user_id: str) -> dict[str, Any]:
    message_dict = message if isinstance(message, Mapping) else {}
    message_info = message_dict.get("message_info")
    message_info_dict = message_info if isinstance(message_info, Mapping) else {}
    additional = message_info_dict.get("additional_config")
    additional_dict = additional if isinstance(additional, Mapping) else {}
    raw_message = message_dict.get("raw_message")
    at_target_ids: list[str] = []
    if isinstance(raw_message, list):
        for segment in raw_message:
            if not isinstance(segment, Mapping) or segment.get("type") != "at":
                continue
            data = segment.get("data")
            target_id = str(data.get("target_user_id") or "").strip() if isinstance(data, Mapping) else ""
            if target_id and target_id not in at_target_ids:
                at_target_ids.append(target_id)
    try:
        timestamp = float(message_dict.get("timestamp") or time.time())
    except (TypeError, ValueError):
        timestamp = time.time()
    return {
        "user_id": user_id,
        "group_id": group_id,
        "self_id": str(additional_dict.get("self_id") or "").strip(),
        "timestamp": timestamp,
        "at_target_ids": at_target_ids,
    }


def _text_segments(message: object) -> str:
    if not isinstance(message, Mapping):
        return ""
    raw_message = message.get("raw_message")
    if not isinstance(raw_message, list):
        return ""
    parts: list[str] = []
    for segment in raw_message:
        if not isinstance(segment, Mapping) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        if isinstance(data, Mapping):
            parts.append(str(data.get("text") or data.get("content") or ""))
        else:
            parts.append(str(data or ""))
    return "".join(parts).strip()


def _strip_leading_mention(text: str) -> str:
    return re.sub(r"^@\S+\s*", "", text.strip(), count=1)


def create_plugin() -> QQBotUsagePlugin:
    """Create the QQBot usage plugin."""

    return QQBotUsagePlugin()
