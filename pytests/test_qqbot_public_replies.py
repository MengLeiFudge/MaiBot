"""使用真实 SDK 验证群聊、私聊和阶段答复保留原生回复器及证据入口。"""

from copy import deepcopy
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from maibot_sdk.components import collect_components
from maibot_sdk.context import PluginContext
from maibot_sdk.types import ComponentType

from plugins.qqbot_identity.plugin import QQBotIdentityPlugin
from plugins.qqbot_knowledge.plugin import QQBotKnowledgePlugin
from plugins.qqbot_reply_guard.plugin import QQBotReplyGuardPlugin


BOT, USER, SESSION = "2629227874", "123456789", "isolated-session"


def message(message_id="source-1", sender=USER, group_id=""):
    """构造真实身份字段齐全的隔离消息。"""
    return {"message_id": message_id, "session_id": SESSION, "platform": "qq",
            "message_info": {"user_info": {"user_id": sender},
                             "group_info": {"group_id": group_id} if group_id else None},
            "raw_message": [{"type": "text", "data": "隔离问题"}], "processed_plain_text": "隔离问题"}


class Host:
    """提供配置和身份只读查询，直接发送或模型调用会立即失败。"""

    def __init__(self):
        self.bot = BOT
        self.messages = [message()]

    async def rpc(self, method, plugin_id, payload, **kwargs):
        """返回 SDK 实际能力包装。"""
        del plugin_id, kwargs
        if method != "cap.call":
            raise AssertionError(method)
        cap, args = payload["capability"], payload["args"]
        if cap == "config.get":
            return {"success": True, "value": self.bot}
        if cap == "message.get_recent":
            messages = self.messages
            if args.get("filter_mai"):
                messages = [m for m in messages if m["message_info"]["user_info"]["user_id"] != BOT]
            return {"success": True, "messages": deepcopy(messages)}
        raise AssertionError(cap)


def items(source_id="source-1"):
    """构造已经进入规划的真实来源消息。"""
    return [{"item_type": "UserMessageItem", "parts": [
        {"type": "text", "text": f'<message msg_id="{source_id}" sender="用户">隔离问题</message>'}]}]


class PublicReplyTests(IsolatedAsyncioTestCase):
    """原生回复资格、完整工具及身份/知识证据的直接边界。"""

    async def asyncSetUp(self):
        """每个用例使用独立插件和能力出口。"""
        self.host = Host()
        self.plugin = QQBotReplyGuardPlugin()
        self.plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        self.plugin._set_context(PluginContext("mlj.qqbot-reply-guard", self.host.rpc))
        await self.plugin.on_load()
        self.tools = [{"type": "function", "function": {"name": name,
                       "parameters": {"type": "object", "properties": {"attach_pic": {"type": "array"}}}}}
                      for name in ("reply", "web_search")]

    async def prepare(self, tools=None, session_id=SESSION):
        """保留传入的工具和请求标记。"""
        return await self.plugin.prepare_reply(session_id=session_id, items=items(), marker="preserved",
            item_schema_version=1, tool_definitions=deepcopy(self.tools if tools is None else tools))

    async def test_components_only_extend_native_request_hooks(self):
        """实际组件中没有消息发送工具。"""
        components = collect_components(self.plugin)
        self.assertTrue(components)
        self.assertTrue(all(component["type"] == ComponentType.HOOK_HANDLER for component in components))

    async def test_native_reply_research_and_attachments_remain_available(self):
        """普通和阶段答复均使用原生 reply，完整保留工具定义及附件。"""
        prepared = (await self.prepare())["modified_kwargs"]
        self.assertEqual(prepared["tool_definitions"], self.tools)
        self.assertEqual(prepared["items"][:-1], items())
        self.assertEqual(prepared["marker"], "preserved")
        text = prepared["items"][-1]["parts"][0]["text"]
        self.assertIn("原生 reply", text)
        self.assertIn("reply_reference", text)
        self.assertIn("继续查询或研究", text)

    async def test_native_reply_eligibility_is_required(self):
        """原生回复不可用时不增加回复规则或新工具。"""
        self.assertEqual(await self.prepare(tools=self.tools[1:]), {"action": "continue"})

    async def test_wrong_instance_and_empty_session_are_ignored(self):
        """实例绑定不匹配或没有真实会话时保持原请求。"""
        self.host.bot = "3600812036"
        self.assertEqual(await self.prepare(), {"action": "continue"})
        self.host.bot = BOT
        self.assertEqual(await self.prepare(session_id=""), {"action": "continue"})

    async def test_disabled_and_unloaded_plugin_do_not_append_rules(self):
        """启用状态变更即时生效。"""
        self.plugin.set_plugin_config({"plugin": {"enabled": False, "config_version": "0.1.0"}})
        self.assertEqual(await self.prepare(), {"action": "continue"})
        self.plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        await self.plugin.on_unload()
        self.assertEqual(await self.prepare(), {"action": "continue"})

    async def test_group_and_private_contexts_keep_their_own_history(self):
        """规则追加不改写群聊或私聊正文，也不触发消息发送。"""
        for group_id in ("", "987654321"):
            with self.subTest(group_id=group_id):
                self.host.messages = [message(group_id=group_id)]
                prepared = (await self.prepare())["modified_kwargs"]
                self.assertEqual(prepared["items"][:-1], items())

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

    async def test_reload_reads_current_enable_configuration(self):
        """配置重载后按当前设置处理新请求。"""
        await self.prepare()
        self.plugin.set_plugin_config({"plugin": {"enabled": False, "config_version": "0.1.0"}})
        await self.plugin.on_config_update("self", {}, "0.1.0")
        self.assertEqual(await self.prepare(), {"action": "continue"})
