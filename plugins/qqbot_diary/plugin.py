from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast

import asyncio

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase

from .exporter import DiaryExporter


class PluginSection(PluginConfigBase):
    """日记工作区导出的启用状态。"""

    enabled: bool = Field(default=False, description="将 better-diary 成品保存到蓝铃工作区")
    config_version: str = Field(default="0.1.0", description="配置版本")


class ExportSection(PluginConfigBase):
    """日记副本的位置和后台同步间隔。"""

    directory: str = Field(default="D:/project/lanling/日记/better-diary", description="日记 Markdown 导出目录")
    interval_seconds: int = Field(default=60, ge=30, le=3600, description="后台检查成品的间隔秒数")


class DiaryConfig(PluginConfigBase):
    """只导出本机已生成的正文，不负责选材、生成或空间发布。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    export: ExportSection = Field(default_factory=ExportSection)


class QQBotDiaryPlugin(MaiBotPlugin):
    """连接 better-diary 存档与个人工作区，不进入聊天请求链路。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = DiaryConfig

    def __init__(self) -> None:
        super().__init__()
        self._task: asyncio.Task[None] | None = None

    async def on_load(self) -> None:
        config = cast(DiaryConfig, self.config)
        if not config.plugin.enabled:
            return
        destination = Path(config.export.directory)
        if not destination.is_absolute():
            raise ValueError("日记导出目录必须是绝对路径")
        data_dir = Path(self.ctx.paths.data_dir)
        exporter = DiaryExporter(
            data_dir.parent / "org.orge-8.better-diary" / "diaries.json",
            destination, data_dir / "exports.json",
        )
        self._task = asyncio.create_task(self._run(exporter, config.export.interval_seconds), name="qqbot-diary-export")
        self.ctx.logger.info("日记工作区导出已启用，目录=%s，间隔秒数=%s", destination, config.export.interval_seconds)

    async def _run(self, exporter: DiaryExporter, interval: int) -> None:
        """磁盘操作在线程中执行；卸载等待当前写入完成，不遗留后台写者。"""
        last_error = ""
        last_conflicts: list[str] = []
        while True:
            pending = asyncio.create_task(asyncio.to_thread(exporter.sync))
            try:
                count, conflicts = await asyncio.shield(pending)
                last_error = ""
                if count:
                    self.ctx.logger.info("日记已导出到工作区，篇数=%s", count)
                if conflicts and conflicts != last_conflicts:
                    self.ctx.logger.warning("保留已手工修改或同名的日记文件，日期=%s", ",".join(conflicts))
                last_conflicts = conflicts
            except asyncio.CancelledError:
                try:
                    await pending
                except Exception as exc:
                    self.ctx.logger.error("日记导出停止时写入失败，error_type=%s", type(exc).__name__)
                raise
            except Exception as exc:
                error = type(exc).__name__
                if error != last_error:
                    self.ctx.logger.error("日记导出失败，保留现有文件；请检查存档格式和目录权限，error_type=%s", error)
                last_error = error
            await asyncio.sleep(interval)

    async def on_unload(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self.on_unload()
            await self.on_load()


def create_plugin() -> MaiBotPlugin:
    """由 Runner 管理导出任务的生命周期。"""
    return QQBotDiaryPlugin()
