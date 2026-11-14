from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, cast

import asyncio
import uuid

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder


REPLY_RULE = (
    "[原生回复规则]决定公开回应时调用原生 reply，由回复器结合完整人设、聊天记录和已核验资料组织正文。"
    "复杂任务已有可靠且有用的阶段结论时，可以先调用 reply，在 reply_reference 中说明已确认事实和本次仅需回应的部分，"
    "随后继续查询或研究，有新增证据后再补充。依赖工具结果的结论必须等待结果，不把未核实的推测写成事实。"
    "简单聊天正常回答，不机械报进度，不重复已经发过的内容，不发送内部分析；决定保持安静时不调用 reply。"
    "联网时先发现并实际调用 web_search；tool_search 只发现本地工具。历史经历只依据实际上下文或检索结果。"
    "查询失败时说明已有结论和缺失证据；只有新线索、不同核验方法或可恢复的临时错误才重试。"
    "需求完成或本轮已明确无法继续核实时如实收尾。[/原生回复规则]"
)


class PluginSection(PluginConfigBase):
    """原生回复规则的启用状态。"""

    enabled: bool = Field(default=False, description="为原生回复提供公开阶段结论与研究续轮规则")
    config_version: str = Field(default="0.1.0", description="配置版本")


class GuardSection(PluginConfigBase):
    """限制插件运行的机器人实例。"""

    bot_qq: str = Field(default="2629227874", pattern=r"^[1-9][0-9]{4,11}$", description="目标机器人 QQ")


class GuardConfig(PluginConfigBase):
    """本机启用配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    guard: GuardSection = Field(default_factory=GuardSection)


class QQBotReplyGuardPlugin(MaiBotPlugin):
    """向已有原生回复资格的规划请求追加阶段答复规则。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = GuardConfig

    def __init__(self) -> None:
        """初始化启用状态，允许加载中途失败后安全卸载。"""
        super().__init__()
        self._active = False

    async def on_load(self) -> None:
        """加载期间只报告状态。"""
        self._active = True
        self.ctx.logger.info("原生回复规则已加载，启用=%s", cast(GuardConfig, self.config).plugin.enabled)

    async def on_unload(self) -> None:
        """停止追加新请求规则。"""
        self._active = False

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """请求直接读取当前配置，不持有跨版本的会话状态。"""
        del scope, config_data, version

    @HookHandler(
        "maisaka.planner.before_request", name="qqbot_reply_guard_prepare",
        description="为已有原生回复资格的会话追加阶段答复与研究续轮规则",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=5000, error_policy=ErrorPolicy.SKIP,
    )
    async def prepare_reply(
        self, session_id: str = "", items: object = None, tool_definitions: object = None,
        item_schema_version: int = 0, **kwargs: Any,
    ) -> dict[str, object]:
        """保留原有上下文和全部工具定义，普通及阶段答复统一交给原生 reply。"""
        config = cast(GuardConfig, self.config)
        if (not self._active or not config.plugin.enabled or not session_id or item_schema_version != 1
                or not isinstance(items, list) or not isinstance(tool_definitions, list)):
            return {"action": "continue"}
        for tool in tool_definitions:
            function = tool.get("function") if isinstance(tool, Mapping) else None
            if isinstance(function, Mapping) and function.get("name") == "reply":
                break
        else:
            return {"action": "continue"}
        async with asyncio.timeout(3):
            bot_qq = str(await self.ctx.config.get("bot.qq_account", ""))
        if bot_qq != config.guard.bot_qq or config is not self.config or not self._active:
            return {"action": "continue"}
        rule = {"item_type": "SystemMessageItem", "meta": {
            "item_id": uuid.uuid4().hex, "logical_turn_id": None, "timestamp": datetime.now().isoformat(),
        }, "parts": [{"type": "text", "text": REPLY_RULE}]}
        return {"action": "continue", "modified_kwargs": {
            **kwargs, "session_id": session_id, "items": [*items, rule],
            "tool_definitions": tool_definitions, "item_schema_version": item_schema_version,
        }}


def create_plugin() -> QQBotReplyGuardPlugin:
    """返回由公开 Runner 管理的插件。"""
    return QQBotReplyGuardPlugin()
