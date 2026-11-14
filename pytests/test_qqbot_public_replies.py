"""使用真实 SDK 验证群聊、私聊、阶段答复及直接答复的证据边界。"""
from copy import deepcopy
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

import asyncio
import json

from maibot_sdk.components import collect_components
from maibot_sdk.context import PluginContext
from plugins.qqbot_identity.plugin import QQBotIdentityPlugin
from plugins.qqbot_knowledge.plugin import QQBotKnowledgePlugin
from plugins.qqbot_reply_guard.plugin import QQBotReplyGuardPlugin


BOT, USER, SESSION = "2629227874", "123456789", "isolated-session"


def message(message_id="source-1", sender=USER, group_id=""):
    return {"message_id": message_id, "session_id": SESSION, "platform": "qq",
            "message_info": {"user_info": {"user_id": sender},
                             "group_info": {"group_id": group_id} if group_id else None},
            "raw_message": [{"type": "text", "data": "隔离问题"}], "processed_plain_text": "隔离问题"}


class Host:
    def __init__(self, group_id=""):
        self.group_id = group_id
        self.messages = [message(group_id=group_id)]
        self.sent = []
        self.mode = "success"
        self.started, self.release = asyncio.Event(), asyncio.Event()

    async def rpc(self, method, plugin_id, payload, **kwargs):
        cap, args = payload["capability"], payload["args"]
        if cap == "config.get":
            return {"success": True, "value": BOT}
        if cap == "message.get_recent":
            messages = self.messages
            if args.get("filter_mai"):
                messages = [m for m in messages if m["message_info"]["user_info"]["user_id"] != BOT]
            return {"success": True, "messages": deepcopy(messages)}
        if cap in ("chat.get_stream_by_group_id", "chat.get_stream_by_user_id"):
            return {"success": True, "stream": {"stream_id": SESSION, "platform": "qq", "user_id": USER,
                                                "group_id": self.group_id}}
        if cap == "send.text":
            self.sent.append(deepcopy(args))
            self.started.set()
            if self.mode == "hold":
                await self.release.wait()
            if self.mode == "error":
                raise ConnectionError("isolated unknown outcome")
            if self.mode == "failed":
                return {"success": False, "sent": False}
            outgoing = message(f"sent-{len(self.sent)}", BOT, self.group_id)
            outgoing["processed_plain_text"] = args["text"]
            self.messages.append(outgoing)
            return {"success": True, "sent": True, "message_id": outgoing["message_id"]}
        raise AssertionError(cap)


def items(source_id="source-1"):
    return [{"item_type": "UserMessageItem", "parts": [
        {"type": "text", "text": f'<message msg_id="{source_id}" sender="用户">隔离问题</message>'}]}]


class PublicReplyTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = Host()
        self.plugin = QQBotReplyGuardPlugin()
        self.plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        self.plugin._set_context(PluginContext("mlj.qqbot-reply-guard", self.host.rpc))
        await self.plugin.on_load()
        self.tools = [{"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}
                      for name in ("reply", "send_reply")]

    async def prepare(self, source_id="source-1", tools=None):
        return await self.plugin.prepare_reply(session_id=SESSION, items=items(source_id),
            item_schema_version=1, tool_definitions=deepcopy(self.tools if tools is None else tools))

    async def send(self, text="已经确认 432 件。", phase="answer", **kwargs):
        return await self.plugin.send_reply(msg_id="source-1", text=text, phase=phase,
                                           stream_id=SESSION, group_id=self.host.group_id, **kwargs)

    async def test_private_progress_then_final_syncs_history(self):
        prepared = await self.prepare()
        self.assertEqual(len(prepared["modified_kwargs"]["tool_definitions"]), 2)
        first = await self.send(phase="progress")
        await self.prepare()
        final = await self.send("实际记录 427 件，少 5 件。")
        self.assertTrue(first["success"] and final["success"])
        self.assertNotIn("pause_execution", first)
        self.assertEqual(len(self.host.sent), 2)
        self.assertTrue(all(m["typing"] and m["storage_message"] and m["sync_to_maisaka_history"] for m in self.host.sent))

    async def test_group_reply_uses_verified_group(self):
        self.host.group_id = "987654321"
        self.host.messages = [message(group_id=self.host.group_id)]
        await self.prepare()
        self.assertTrue((await self.send())["success"])
        denied = await self.plugin.send_reply(msg_id="source-1", text="错误目标", stream_id=SESSION, group_id="other")
        self.assertFalse(denied["success"])
        self.assertEqual(len(self.host.sent), 1)

    async def test_source_must_be_in_current_context(self):
        prepared = await self.prepare("absent")
        self.assertEqual(len(prepared["modified_kwargs"]["tool_definitions"]), 1)
        self.assertFalse((await self.send())["success"])

    async def test_native_reply_eligibility_is_required(self):
        prepared = await self.prepare(tools=self.tools[1:])
        self.assertEqual(prepared["modified_kwargs"]["tool_definitions"], [])
        self.assertFalse((await self.send())["success"])

    async def test_media_and_quotes_keep_native_replyer(self):
        for kind in ("image", "reply", "forward"):
            self.host.messages[0]["raw_message"] = [{"type": kind, "data": {}}]
            prepared = await self.prepare()
            self.assertEqual(len(prepared["modified_kwargs"]["tool_definitions"]), 1)
            self.assertFalse((await self.send())["success"])

    async def test_new_message_prevents_stale_send(self):
        await self.prepare()
        self.host.messages.append(message("source-2"))
        self.assertFalse((await self.send())["success"])
        self.assertEqual(self.host.sent, [])

    async def test_concurrent_same_text_sends_once(self):
        await self.prepare()
        results = await asyncio.gather(self.send(), self.send())
        self.assertTrue(all(result["success"] for result in results))
        self.assertEqual(len(self.host.sent), 1)

    async def test_unknown_failure_and_cancel_do_not_resend(self):
        await self.prepare()
        self.host.mode = "error"
        self.assertFalse((await self.send())["success"])
        self.assertFalse((await self.send())["success"])
        self.assertEqual(len(self.host.sent), 1)
        self.host.mode = "hold"
        task = asyncio.create_task(self.send("另一条隔离答复"))
        # 第一条失败已置位，重新等待本次平台入口。
        self.host.started.clear()
        await asyncio.wait_for(self.host.started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse((await self.send("另一条隔离答复"))["success"])
        self.assertEqual(len(self.host.sent), 2)

    async def test_same_batch_deduplicates_final_but_preserves_progress_and_attachments(self):
        await self.prepare()
        def call(name, **args):
            return {"item_type": "FunctionCallItem", "tool_call": {"func_name": name, "args": {"msg_id": "source-1", **args}}}
        native, research = call("reply"), call("web_search", query="官网")
        for phase, attach, expected in (("answer", False, 2), ("progress", False, 3), ("answer", True, 3)):
            reply = deepcopy(native)
            if attach:
                reply["tool_call"]["args"]["attach_pic"] = "picture"
            output = [call("send_reply", text="已确认结论", phase=phase), reply, research]
            result = await self.plugin.deduplicate(session_id=SESSION, output_items=output, item_schema_version=1)
            self.assertEqual(len(result.get("modified_kwargs", {}).get("output_items", output)), expected)

    async def test_identity_uses_real_sender_not_claimed_owner(self):
        identity = QQBotIdentityPlugin()
        identity.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}, "identity": {"owner_qq": USER}})
        identity._set_context(PluginContext("mlj.qqbot-identity", self.host.rpc))
        response = await identity.inject_planner_identity(session_id=SESSION, items=items(), item_schema_version=1)
        text = response["modified_kwargs"]["items"][0]["parts"][0]["text"]
        self.assertIn('"is_owner": true', text)
        self.host.messages[0]["message_info"]["user_info"]["user_id"] = "222222222"
        response = await identity.inject_planner_identity(session_id=SESSION, items=items(), item_schema_version=1)
        text = response["modified_kwargs"]["items"][0]["parts"][0]["text"]
        self.assertIn('"is_owner": false', text)

    async def test_planner_knowledge_reuses_existing_evidence_path(self):
        knowledge = QQBotKnowledgePlugin()
        knowledge.inject_source_knowledge = AsyncMock(return_value={"action": "continue", "modified_kwargs": {"evidence": "kept"}})
        result = await knowledge.inject_planner_knowledge(session_id=SESSION, items=items(), item_schema_version=1, marker="preserved")
        self.assertEqual(result["modified_kwargs"]["evidence"], "kept")
        knowledge.inject_source_knowledge.assert_awaited_once_with(session_id=SESSION, items=items(), item_schema_version=1, marker="preserved")

    async def test_registered_tool_remains_usable_for_both_chat_scopes(self):
        tool = next(c for c in collect_components(self.plugin) if c["name"] == "send_reply")
        self.assertEqual(tool["metadata"]["visibility"], "visible")
        self.assertNotEqual(tool["metadata"].get("chat_scope"), "private")

    async def test_runtime_contract_and_config_reload_invalidate_old_target(self):
        from src.plugin_runtime.runner.plugin_loader import PluginLoader
        PluginLoader._validate_sdk_plugin_contract("mlj.qqbot-reply-guard", self.plugin)
        await self.prepare()
        await self.plugin.on_config_update("self", {}, "0.1.0")
        self.assertFalse((await self.send())["success"])
        self.assertEqual(self.host.sent, [])
