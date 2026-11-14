"""使用真实 SDK 验证公开答复始终交给原生 reply；不连接 QQ 或模型。"""

from copy import deepcopy
from typing import Any
from unittest import IsolatedAsyncioTestCase

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF
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
        self.new_message_during_model = False
        self.model_result: dict[str, Any] = {"success": True, "tool_calls": [{"function": {
            "name": "reply", "arguments": {"msg_id": "source-1", "set_quote": False, "reply_reference": "已经核验的事实"},
        }}]}

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
        if cap == "llm.generate_with_tools":
            self.model_calls += 1
            if self.new_message_during_model:
                self.messages.append(message("source-2"))
            return deepcopy(self.model_result)
        raise AssertionError(f"Unexpected capability: {cap}")


class PublicReplyTests(IsolatedAsyncioTestCase):
    """覆盖原生回复保留、来源检查和单次漏执行修复。"""

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

    async def complete(self) -> dict[str, Any]:
        """只向动作核对提供内部规划数据，正文由原生回复器生成。"""
        return await self.plugin.complete_reply(session_id=SESSION, item_schema_version=1, output_items=[{
            "item_type": "AssistantMessageItem", "meta": _meta("turn-1"),
            "parts": [{"type": "text", "text": "已经确定需要回应当前消息。"}],
        }])

    async def test_registered_components_only_extend_hooks(self) -> None:
        """实际组件中没有可绕过回复器的发送工具。"""
        components = collect_components(self.plugin)
        self.assertTrue(components)
        self.assertTrue(all(component["type"] == ComponentType.HOOK_HANDLER for component in components))

    async def test_native_tools_and_research_guidance_are_preserved(self) -> None:
        """阶段结论使用原生 reply，工具定义和原历史均保留。"""
        result = (await self.prepare())["modified_kwargs"]
        self.assertEqual(result["tool_definitions"], self.tools)
        text = result["items"][-1]["parts"][0]["text"]
        self.assertIn("原生 reply", text)
        self.assertIn("reply_reference", text)
        self.assertIn("继续查询或研究", text)
        self.assertEqual(self.host.model_calls, 0)

    async def test_existing_reply_attachments_and_research_calls_are_untouched(self) -> None:
        """已产生的原生正文、附件和后续查询不被去重规则删掉。"""
        await self.prepare()
        output = [{"item_type": "FunctionCallItem", "meta": _meta("turn-1"), "tool_call": {
            "call_id": "reply-1", "func_name": "reply",
            "args": {"msg_id": "source-1", "reply_reference": "阶段已知结论",
                     "attach_pic": [{"msg_id": "source-1", "index": 0}], "attach_at": ["source-1"]},
        }}, {"item_type": "FunctionCallItem", "meta": _meta("turn-1"), "tool_call": {
            "call_id": "research-1", "func_name": "web_search", "args": {"query": "继续核验"},
        }}]
        saved = deepcopy(output)
        result = await self.plugin.complete_reply(session_id=SESSION, output_items=output, item_schema_version=1)
        self.assertEqual(result, {"action": "continue"})
        self.assertEqual(output, saved)
        self.assertEqual(self.host.model_calls, 0)

    async def test_missing_execution_adds_only_native_reply(self) -> None:
        """核对只恢复原生工具及参考信息，不包含可以直接发送的正文。"""
        await self.prepare()
        result = await self.complete()
        call = result["modified_kwargs"]["output_items"][-1]
        self.assertEqual(call["tool_call"]["func_name"], "reply")
        self.assertEqual(call["meta"]["logical_turn_id"], "turn-1")
        self.assertEqual(call["tool_call"]["args"], {
            "msg_id": "source-1", "set_quote": False, "reply_reference": "已经核验的事实",
        })
        self.assertEqual(self.host.model_calls, 1)

    async def test_same_source_is_checked_only_once(self) -> None:
        """同一来源后续纯文本轮次不会循环核对。"""
        await self.prepare()
        await self.complete()
        await self.prepare()
        self.assertEqual(await self.complete(), {"action": "continue"})
        self.assertEqual(self.host.model_calls, 1)

    async def test_unseen_or_other_chat_source_does_not_get_repair(self) -> None:
        """消息 API 与当前规划必须指向同一个真实来源。"""
        self.assertEqual(await self.prepare(message_id="unseen"), {"action": "continue"})
        self.assertEqual(await self.prepare(session_id="other-chat"), {"action": "continue"})
        self.assertEqual(await self.complete(), {"action": "continue"})
        self.assertEqual(self.host.model_calls, 0)

    async def test_new_message_before_repair_is_not_answered_as_old_source(self) -> None:
        """新消息尚未进入规划时也不能恢复旧目标。"""
        await self.prepare()
        self.host.messages.append(message("source-2"))
        self.assertEqual(await self.complete(), {"action": "continue"})
        self.assertEqual(self.host.model_calls, 0)

    async def test_new_message_during_repair_discards_old_call(self) -> None:
        """核对期间收到新消息后，旧工具调用不进入执行。"""
        await self.prepare()
        self.host.new_message_during_model = True
        self.assertEqual(await self.complete(), {"action": "continue"})
        self.assertEqual(self.host.model_calls, 1)

    async def test_silence_and_failed_repair_do_not_send(self) -> None:
        """静默和请求失败均保留原规划，不补正文或重试。"""
        for response in ({"success": True, "tool_calls": []}, {"success": False}):
            with self.subTest(response=response):
                await self.plugin.on_load()
                self.plugin._settled_message_id = "source-1"
                self.host.model_result = response
                await self.prepare()
                self.assertEqual(await self.complete(), {"action": "continue"})

    async def test_reload_and_unload_drop_pending_repair(self) -> None:
        """配置变化和卸载后，旧快照不能生成回复调用。"""
        await self.prepare()
        await self.plugin.on_config_update(CONFIG_RELOAD_SCOPE_SELF, {}, "0.1.0")
        self.assertEqual(await self.complete(), {"action": "continue"})
        await self.prepare()
        await self.plugin.on_unload()
        self.assertEqual(await self.complete(), {"action": "continue"})

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
            self.assertIsNone(self.plugin._pending)
