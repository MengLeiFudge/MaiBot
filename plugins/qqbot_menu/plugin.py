from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import asyncio
import base64
import re
import secrets
import time

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from qqbot_common.api_results import require_api_result

from .menu_image import render_feature_menu_image, render_overview_menu_image


MENU_COMMAND_PATTERN = r"^(?:@\S+\s*)?(?:菜单|帮助|指令)(?!(?:\s*\d+\s*)$)(?:\s*.+)?$"
_RUNTIME_TEXT = "当前运行：MaiBot 原生插件；AstrBot 已停止。"
_IMAGE_SUMMARIES = ("指令菜单", "功能一览", "菜单送到", "可用指令", "棉花糖菜单")


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 QQBot 聚合菜单")
    config_version: str = Field(default="0.3.1", description="配置版本")


class MenuConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)


@dataclass(frozen=True, slots=True)
class MenuSection:
    name: str
    aliases: tuple[str, ...]
    status: str
    lines: tuple[str, ...]


class QQBotMenuPlugin(MaiBotPlugin):
    """Render the complete fixed-command catalog before the chat pipeline."""

    config_model: ClassVar[type[PluginConfigBase] | None] = MenuConfig

    async def on_load(self) -> None:
        self.ctx.logger.info("QQBot 菜单插件已加载")

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data
        self.ctx.logger.info("QQBot 菜单配置已更新: %s", version)

    @Command(
        "qqbot_menu",
        description="QQBot 完整固定指令菜单",
        pattern=MENU_COMMAND_PATTERN,
        timeout_ms=30_000,
    )
    async def handle_menu_command(
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
        facts = _message_facts(message, group_id, user_id)
        if not self.config.plugin.enabled:
            response = "菜单功能当前未启用。"
            await self._send_text(group_id, user_id, stream_id, response)
            return True, response, True
        if not facts["self_id"]:
            raise ValueError("菜单命令缺少当前机器人 self_id")
        if not await self._claim(command_text, facts):
            return True, "", True

        sections = await self._sections()
        key = _menu_key(command_text)
        section = _find_section(sections, key) if key else None
        if key and section is None:
            response = _unknown_section_text(key, sections)
            await self._send_text(group_id, user_id, stream_id, response)
            return True, response, True

        response = _section_text(section) if section is not None else _overview_text(sections)
        try:
            output_dir = Path(self.ctx.paths.runtime_dir).resolve() / "menu"
            if section is None:
                image_path = await asyncio.to_thread(
                    render_overview_menu_image,
                    features=sections,
                    feature_mode=_RUNTIME_TEXT,
                    output_dir=output_dir,
                )
            else:
                image_path = await asyncio.to_thread(
                    render_feature_menu_image,
                    feature=section,
                    feature_mode=_RUNTIME_TEXT,
                    output_dir=output_dir,
                )
            await self._send_image(group_id, user_id, image_path)
        except Exception as exc:
            self.ctx.logger.warning("菜单图片发送失败，改发文本: error_type=%s", type(exc).__name__)
            await self._send_text(group_id, user_id, stream_id, response)
        return True, response, True

    async def _sections(self) -> tuple[MenuSection, ...]:
        specs = (
            ("group", "mlj.qqbot-group", "cleanup.write_enabled"),
            ("draw", "mlj.qqbot-draw", "cutover.write_enabled"),
            ("usage", "mlj.qqbot-usage", "plugin.enabled"),
            ("lolicon", "mlj.qqbot-lolicon", "plugin.enabled"),
            ("reread", "mlj.qqbot-reread", "plugin.enabled"),
            ("kun", "mlj.qqbot-kun", "cutover.write_enabled"),
            ("sakura", "mlj.qqbot-sakura", "cutover.write_enabled"),
            ("arc", "mlj.qqbot-arc", "cutover.write_enabled"),
            ("comic", "mlj.qqbot-comic", "plugin.enabled"),
            ("factorio", "mlj.qqbot-factorio", "plugin.enabled"),
            ("shapez", "mlj.qqbot-shapez", "cutover.write_enabled"),
            ("meme", "mlj.qqbot-meme", "plugin.enabled"),
        )
        values = await asyncio.gather(
            *(self._plugin_flag(plugin_id, path) for _, plugin_id, path in specs)
        )
        enabled = {name: value for (name, _, _), value in zip(specs, values, strict=True)}
        return _build_sections(enabled)

    async def _plugin_flag(self, plugin_id: str, path: str) -> bool:
        try:
            result = await self.ctx.api.call(
                "config.get",
                plugin_id=plugin_id,
                path=path,
                default=False,
            )
        except Exception:
            return False
        if isinstance(result, Mapping):
            return bool(result.get("value", result.get("data", False)))
        return bool(result)

    async def _claim(self, text: str, facts: dict[str, Any]) -> bool:
        result = await self.ctx.api.call(
            "qqbot.route.claim",
            feature="menu",
            text=text,
            user_id=facts["user_id"],
            group_id=facts["group_id"],
            self_id=facts["self_id"],
            timestamp=facts["timestamp"],
            at_target_ids=facts["at_target_ids"],
        )
        payload = require_api_result(result, "菜单命令仲裁")
        if not isinstance(payload, Mapping):
            raise RuntimeError("菜单命令仲裁返回格式无效")
        return bool(payload.get("claimed"))

    async def _send_image(self, group_id: str, user_id: str, path: Path) -> None:
        encoded = await asyncio.to_thread(base64.b64encode, path.read_bytes())
        segment = {
            "type": "image",
            "data": {
                "file": "base64://" + encoded.decode("ascii"),
                "summary": secrets.choice(_IMAGE_SUMMARIES),
            },
        }
        if group_id:
            api_name = "adapter.napcat.group.send_group_msg"
            params = {"group_id": group_id, "message": [segment]}
        else:
            api_name = "adapter.napcat.message.send_private_msg"
            params = {"user_id": user_id, "message": [segment]}
        result = await self.ctx.api.call(api_name, params=params)
        require_api_result(result, "发送菜单图片")

    async def _send_text(self, group_id: str, user_id: str, stream_id: str, text: str) -> None:
        if stream_id:
            await self.ctx.send.text(text, stream_id)
            return
        message = [{"type": "text", "data": {"text": text}}]
        if group_id:
            api_name = "adapter.napcat.group.send_group_msg"
            params = {"group_id": group_id, "message": message}
        else:
            api_name = "adapter.napcat.message.send_private_msg"
            params = {"user_id": user_id, "message": message}
        result = await self.ctx.api.call(api_name, params=params)
        require_api_result(result, "发送菜单文本")


def _build_sections(enabled: Mapping[str, bool]) -> tuple[MenuSection, ...]:
    return (
        MenuSection(
            "群务管理",
            ("群务", "群管理", "群管", "欢迎", "好友邀请"),
            _status(enabled["group"]),
            (
                "通知清理文件：主人统计超期外层群文件并按大小禁言上传者",
                "好友申请与邀请入群按配置自动同意；机器人入群后私聊通知邀请者",
                "真人入群时按当前机器人身份发送欢迎",
            ),
        ),
        MenuSection(
            "棉花糖互动",
            ("互动", "聊天", "AI", "生图", "美图", "用量"),
            _combined_status(enabled["draw"], enabled["usage"], enabled["lolicon"], enabled["reread"]),
            (
                "棉花糖生图 提示词；生图模型；切换生图模型 模型名",
                "查看积分；积分排行；用量",
                "来点美图 / 色图 / 涩图 / 蛇图 / 混合；开关群色图和图片显示",
                "群聊连续相同纯文本按概率自动复读；固定命令和混合消息不参与",
            ),
        ),
        MenuSection(
            "养鲲",
            ("鲲",),
            _status(enabled["kun"]),
            (
                "养鲲 / 摸鲲 / 抓鲲 / 捕鲲 / 属性 / 背包 / 商城 / 签到",
                "挑战 / 排行 / 进击 / 赠送 / 改名 / 洗练及原有养鲲入口",
            ),
        ),
        MenuSection(
            "落樱之都",
            ("樱花", "落樱"),
            _status(enabled["sakura"]),
            (
                "落樱之都 / 更新日志 / 玩法 / 注册角色名 / 改名角色名",
                "个人信息 / 加经验数字 / 嘤数字 / 加数字属性 / 恢复",
            ),
        ),
        MenuSection(
            "Arcaea",
            ("Arc", "Arc查询", "Arc狼人杀", "Arc吃鸡", "arcaea"),
            _status(enabled["arc"]),
            (
                "arctj10.5：按 PTT 推荐谱面；archd / arctz：活动梯子",
                "zm / arczm：字母猜歌；qh / arcqh：曲绘猜歌；arcqh bt / arcqh补图：补图；jx / arcjx：揭晓",
                "xz / arcxz：主人查询并下载最新安装包；后台同步与活动提醒",
            ),
        ),
        MenuSection(
            "JM漫画",
            ("JM", "漫画", "PDF"),
            _status(enabled["comic"]),
            ("JM作品ID：好友可在群聊或私聊请求；加密 PDF 最终由具备好友关系的机器人私聊发送",),
        ),
        MenuSection(
            "Factorio",
            ("异星工厂", "太空时代", "Space Age", "spaceage"),
            _status(enabled["factorio"]),
            ("Factorio下载链接 / 异星下载链接 / 太空时代下载链接：获取 Space Age Windows 安装包",),
        ),
        MenuSection(
            "异形工厂",
            ("shapez", "Shapez"),
            _status(enabled["shapez"]),
            (
                "i / view：渲染短代码；chart：结构图；path：构造路径",
                "p / puzzle：在线谜题入口，缺少登录 token 时明确提示",
            ),
        ),
        MenuSection(
            "表情管理",
            ("表情", "图库", "meme"),
            _status(enabled["meme"]),
            (
                "表情管理 查看图库 / 添加表情 / 恢复默认表情包",
                "清空指定类型 / 清空全部 / 删除类型本身 / 图库统计",
                "开启管理后台 / 关闭管理后台 / 同步状态",
                "同步到云端 / 从云端同步 / 覆盖到云端 / 从云端覆盖",
            ),
        ),
    )


def _status(enabled: bool) -> str:
    return "已启用" if enabled else "未加载"


def _combined_status(*values: bool) -> str:
    if all(values):
        return "已启用"
    if any(values):
        return "部分启用"
    return "未加载"


def _find_section(sections: tuple[MenuSection, ...], key: str) -> MenuSection | None:
    normalized = key.casefold()
    for section in sections:
        if any(normalized == name.casefold() for name in (section.name, *section.aliases)):
            return section
    return None


def _menu_key(text: str) -> str:
    normalized = re.sub(r"^@\S+\s*", "", text.strip(), count=1)
    for prefix in ("菜单", "帮助", "指令"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].strip()
    return ""


def _overview_text(sections: tuple[MenuSection, ...]) -> str:
    lines = ["棉花糖统一指令菜单", _RUNTIME_TEXT]
    lines.extend(f"{section.name}：{section.status}" for section in sections)
    lines.append("发送 菜单模块名 查看详情，例如 菜单JM漫画。")
    return "\n".join(lines)


def _section_text(section: MenuSection) -> str:
    return "\n".join((f"{section.name}：{section.status}", *section.lines))


def _unknown_section_text(key: str, sections: tuple[MenuSection, ...]) -> str:
    names = "、".join(section.name for section in sections)
    return f"没有找到菜单分类“{key}”。可用分类：{names}。"


def _message_facts(message: object, group_id: str, user_id: str) -> dict[str, Any]:
    payload = message if isinstance(message, Mapping) else {}
    info = payload.get("message_info")
    info = info if isinstance(info, Mapping) else {}
    additional = info.get("additional_config")
    additional = additional if isinstance(additional, Mapping) else {}
    raw_message = payload.get("raw_message")
    targets: list[str] = []
    if isinstance(raw_message, list):
        for segment in raw_message:
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
        "group_id": str(group_id or "").strip(),
        "user_id": str(user_id or "").strip(),
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
        parts.append(
            str(data.get("text") or data.get("content") or "")
            if isinstance(data, Mapping)
            else str(data or "")
        )
    return "".join(parts).strip()


def create_plugin() -> QQBotMenuPlugin:
    return QQBotMenuPlugin()
