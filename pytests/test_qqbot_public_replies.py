"""使用真实 SDK 验证公开答复始终交给原生 reply；不连接 QQ 或模型。"""

from copy import deepcopy
from typing import Any
from unittest import IsolatedAsyncioTestCase


from maibot_sdk.components import collect_components
from maibot_sdk.context import PluginContext
from maibot_sdk.types import ComponentType

from plugins.qqbot_reply_guard.plugin import QQBotReplyGuardPlugin, _meta


BOT = "3600812036"
OWNER = "123456789"
SESSION = "isolated-private-session"


def message(message_id: str, sender: str = OWNER) -> dict[str, Any]:
    """构造含框架真实身份字段的隔离私聊消息。"""
    return {
        "message_id": message_id, "session_id": SESSION, "platform": "qq",
        "message_info": {"user_info": {"user_id": sender}, "group_info": None},
        "raw_message": [{"type": "text", "data": "隔离消息"}], "processed_plain_text": "隔离消息",
    }


class PrivateHost:
    """提供只读消息及隔离动作核对；未知能力（包括直接发送）立即失败。"""

    def __init__(self) -> None:
        self.messages = [message("source-1")]
        self.owner = OWNER
        self.bot = BOT
        self.model_calls = 0


    async def rpc(self, method: str, plugin_id: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        """沿用实际 SDK 包装，动作核对只返回原生工具调用。"""
        del plugin_id, kwargs
        if method != "cap.call":
            raise AssertionError(method)
        cap, args = payload["capability"], payload["args"]
        if cap == "config.get":
            return {"success": True, "value": self.bot}
        if cap == "config.get_plugin":
            return {"success": True, "value": {"plugin": {"enabled": True}, "identity": {"owner_qq": self.owner}}}
        if cap == "chat.get_stream_by_user_id":
            return {"success": True, "stream": {
                "stream_id": SESSION, "platform": "qq", "user_id": self.owner, "group_id": "",
            }}
        if cap == "message.get_recent":
            return {"success": True, "messages": deepcopy(self.messages[-args["limit"]:])}
        if cap == "message.get_by_id":
            found = next((m for m in self.messages if m["message_id"] == args["message_id"]), None)
            return {"success": True, "message": deepcopy(found)}

        raise AssertionError(f"Unexpected capability: {cap}")


class PublicReplyTests(IsolatedAsyncioTestCase):
    """覆盖连发收集、引用和原生流程保留，不允许任何模型或发送能力调用。"""

    async def asyncSetUp(self) -> None:
        """每个用例创建独立插件和真实 SDK 上下文。"""
        self.host = PrivateHost()
        self.plugin = QQBotReplyGuardPlugin()
        self.plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        self.plugin._set_context(PluginContext("mlj.qqbot-reply-guard", self.host.rpc))
        await self.plugin.on_load()
        self.plugin._settled_message_id = "source-1"
        self.tools = [{"type": "function", "function": {"name": name,
                       "parameters": {"type": "object", "properties": {}}}} for name in ("reply", "web_search")]

    async def prepare(self, message_id: str = "source-1", session_id: str = SESSION) -> dict[str, Any]:
        """通过实际请求 Hook 建立原生回复目标。"""
        return await self.plugin.prepare_reply(
            session_id=session_id, item_schema_version=1, items=[{
                "item_type": "UserMessageItem", "meta": _meta(),
                "parts": [{"type": "text", "text": f'<message msg_id="{message_id}" sender="主人">隔离内容</message>'}],
            }], tool_definitions=deepcopy(self.tools),
        )

    async def test_only_collection_and_quote_hooks_remain(self) -> None:
        """没有生成后动作核对，也没有可绕过原生回复器的工具。"""
        components = collect_components(self.plugin)
        self.assertTrue(all(component["type"] == ComponentType.HOOK_HANDLER for component in components))
        self.assertFalse(any(component.get("name") == "qqbot_reply_guard_complete" for component in components))
        self.assertEqual(await self.prepare(), {"action": "continue"})
        self.assertEqual(self.host.model_calls, 0)

    async def test_unseen_and_foreign_session_keep_original_request(self) -> None:
        self.assertEqual(await self.prepare(message_id="unseen"), {"action": "continue"})
        self.assertEqual(await self.prepare(session_id="other-chat"), {"action": "continue"})

    async def test_quote_source_and_replyer_context_are_preserved(self) -> None:
        """真实引用仍同时提供给规划和待回复的原生回复器。"""
        quoted = message("quoted-1", BOT)
        quoted["processed_plain_text"] = "此前已经核验的公开内容"
        self.host.messages.insert(0, quoted)
        self.host.messages[-1]["raw_message"].append({"type": "reply", "data": {"target_message_id": "quoted-1"}})
        prepared = (await self.prepare())["modified_kwargs"]
        self.assertIn("此前已经核验的公开内容", prepared["items"][-1]["parts"][0]["text"])
        replyer = await self.plugin.prepare_search_context(session_id=SESSION, extra_prompt="原有要求")
        prompt = replyer["modified_kwargs"]["extra_prompt"]
        self.assertIn("原有要求", prompt)
        self.assertIn("此前已经核验的公开内容", prompt)

    async def test_settle_window_credits_elapsed_preparation_and_preserves_new_message_check(self) -> None:
        """慢准备不重复等两秒；快准备补足窗口，等待中新消息仍废弃旧目标。"""
        from types import SimpleNamespace
        from unittest.mock import patch
        from plugins.qqbot_reply_guard import plugin as module
        waits = []
        clock = [100.0]
        async def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds
        with patch.object(module, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            await self.plugin.receive(message=self.host.messages[-1])
            self.plugin._settled_message_id = ""
            clock[0] += 5
            with patch.object(module.asyncio, "sleep", sleep):
                await self.prepare()
            self.assertEqual(waits, [])
            self.host.messages.append(message("source-2"))
            await self.plugin.receive(message=self.host.messages[-1])
            clock[0] += 0.5
            await self.plugin.receive(message=self.host.messages[-1])
            with patch.object(module.asyncio, "sleep", sleep):
                await self.prepare("source-2")
            self.assertEqual(waits, [1.5])
            self.host.messages.append(message("source-3"))
            await self.plugin.receive(message=self.host.messages[-1])
            async def receive_new(seconds):
                self.host.messages.append(message("source-4"))
                await self.plugin.receive(message=self.host.messages[-1])
            with patch.object(module.asyncio, "sleep", receive_new):
                result = await self.prepare("source-3")
            self.assertEqual(result, {"action": "continue"})
            self.assertEqual(self.host.model_calls, 0)
