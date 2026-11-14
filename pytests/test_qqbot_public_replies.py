"""使用真实 SDK 和隔离的能力出口验证阶段答复，不连接 QQ 或模型。"""

from copy import deepcopy
from typing import Any
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch
import asyncio

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF
from maibot_sdk.components import collect_components
from maibot_sdk.context import PluginContext

from plugins.qqbot_reply_guard.plugin import QQBotReplyGuardPlugin, _meta
from plugins.qqbot_reply_guard.text_splitter import split_reply
from plugins.qqbot_reply_timing import plugin as timing_module


BOT = "3600812036"
OWNER = "123456789"
SESSION = "isolated-private-session"
LONG_REPLY = (
    "第一部分说明已经确认的事实和依据。"
    "第二部分解释现有记录之间的关系和差异。"
    "第三部分保留需要进一步研究的问题和完整的结论。"
)


def message(message_id: str, sender: str = OWNER) -> dict[str, Any]:
    """构造包含框架真实身份字段的隔离私聊消息。"""
    return {
        "message_id": message_id, "session_id": SESSION, "platform": "qq",
        "message_info": {"user_info": {"user_id": sender}, "group_info": None},
        "raw_message": [{"type": "text", "data": "测试消息"}], "processed_plain_text": "测试消息",
    }


class PrivateHost:
    """截获 SDK 能力请求，记录真实发送参数并维护隔离消息历史。"""

    def __init__(self) -> None:
        self.messages = [message("source-1")]
        self.sent: list[dict[str, Any]] = []
        self.owner = OWNER
        self.bot = BOT
        self.post_process = True
        self.splitter: dict[str, object] = {"enable": True, "max_split_num": 3}
        self.send_mode = "success"
        self.send_modes: list[str] = []
        self.new_message_after: int | None = None
        self.send_started = asyncio.Event()
        self.held_send_started = asyncio.Event()
        self.send_released = asyncio.Event()

    async def rpc(self, method: str, plugin_id: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        """使用 SDK 的实际返回包装，未知能力立即失败。"""
        del plugin_id, kwargs
        if method != "cap.call":
            raise AssertionError(method)
        cap, args = payload["capability"], payload["args"]
        if cap == "config.get":
            values = {
                "bot.qq_account": self.bot,
                "response_post_process.enable_response_post_process": self.post_process,
                "response_post_process.typing_speed": 0.2,
                "response_splitter.enable": self.splitter["enable"],
                "response_splitter.max_split_num": self.splitter["max_split_num"],
            }
            return {"success": True, "value": values[args["key"]]}
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
        if cap == "send.text":
            self.sent.append(deepcopy(args))
            self.send_started.set()
            mode = self.send_modes.pop(0) if self.send_modes else self.send_mode
            if mode == "hold":
                self.held_send_started.set()
                await self.send_released.wait()
            if mode == "error":
                raise ConnectionError("isolated send outcome unknown")
            if mode == "failed":
                return {"success": False, "sent": False, "message_id": None}
            message_id = f"sent-{len(self.sent)}"
            sent = message(message_id, BOT)
            sent["processed_plain_text"] = args["text"]
            self.messages.append(sent)
            if len(self.sent) == self.new_message_after:
                self.messages.append(message("source-2"))
            return {"success": True, "sent": True, "message_id": message_id}
        raise AssertionError(f"Unexpected capability: {cap}")


class PublicReplyTests(IsolatedAsyncioTestCase):
    """覆盖会话归属、续答、并发去重和发送取消等有副作用边界。"""

    async def asyncSetUp(self) -> None:
        """每个用例创建独立插件、配置和 SDK 上下文。"""
        self.host = PrivateHost()
        self.plugin = QQBotReplyGuardPlugin()
        self.plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        self.plugin._set_context(PluginContext("mlj.qqbot-reply-guard", self.host.rpc))
        await self.plugin.on_load()
        self.plugin._settled_message_id = "source-1"
        self.direct = next(c for c in collect_components(self.plugin) if c["name"] == "send_reply")
        meta = self.direct["metadata"]
        props = {k: {p: v for p, v in value.items() if p != "required"} for k, value in meta["parameters_raw"].items()}
        self.tools = [
            {"type": "function", "function": {"name": "reply", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "send_reply", "description": meta["description"],
                "parameters": {"type": "object", "properties": props, "required": ["msg_id", "text"]}}},
        ]

    async def prepare(self, session_id: str = SESSION, message_id: str = "source-1") -> dict[str, Any]:
        """通过实际规划 Hook 建立发送目标，不直接伪造插件状态。"""
        item = {"item_type": "UserMessageItem", "meta": _meta(), "parts": [
            {"type": "text", "text": f'<message msg_id="{message_id}" sender="主人">测试内容</message>'},
        ]}
        result = await self.plugin.prepare_reply(
            session_id=session_id, items=[item], tool_definitions=deepcopy(self.tools), item_schema_version=1,
        )
        return result["modified_kwargs"]

    async def send(self, text: str = "已确认共有 432 件。", phase: str = "answer") -> dict[str, object]:
        """模拟原生工具分发注入的真实会话字段。"""
        return await self.plugin.send_reply(msg_id="source-1", text=text, phase=phase, stream_id=SESSION, user_id=OWNER)

    async def test_long_reply_preserves_all_text_and_each_send_contract(self) -> None:
        """同文并发只发一组三段，每段独立保存历史和启用正常速度。"""
        await self.prepare()
        first, second = await asyncio.gather(self.send(LONG_REPLY), self.send(LONG_REPLY))
        self.assertTrue(first["success"])
        self.assertEqual(first, second)
        self.assertEqual(first["sent_parts"], 3)
        self.assertEqual(first["message_ids"], ["sent-1", "sent-2", "sent-3"])
        self.assertEqual("".join(item["text"] for item in self.host.sent), LONG_REPLY)
        for item in self.host.sent:
            self.assertTrue(item["typing"])
            self.assertTrue(item["storage_message"])
            self.assertTrue(item["sync_to_maisaka_history"])
            self.assertEqual(item["maisaka_source_kind"], "outbound_send")

    async def test_existing_split_switches_and_limit_are_respected(self) -> None:
        """关闭分段或限制为一条时保留整段；两条上限不会丢掉第三句。"""
        for post, enabled, limit, expected in ((False, True, 3, 1), (True, False, 3, 1),
                                               (True, True, 1, 1), (True, True, 2, 2)):
            with self.subTest(post=post, enabled=enabled, limit=limit):
                self.host = PrivateHost()
                self.host.post_process = post
                self.host.splitter.update(enable=enabled, max_split_num=limit)
                self.plugin._set_context(PluginContext("mlj.qqbot-reply-guard", self.host.rpc))
                await self.plugin.on_load()
                self.plugin._settled_message_id = "source-1"
                await self.prepare()
                self.assertTrue((await self.send(LONG_REPLY))["success"])
                self.assertEqual(len(self.host.sent), expected)
                self.assertEqual("".join(item["text"] for item in self.host.sent), LONG_REPLY)

    async def test_invalid_split_configuration_does_not_send(self) -> None:
        """无效条数上限不能悄悄退回整段发送。"""
        await self.prepare()
        self.host.splitter["max_split_num"] = 0
        self.assertFalse((await self.send(LONG_REPLY))["success"])
        self.assertEqual(self.host.sent, [])

    async def test_partial_failure_stops_and_does_not_resend_whole_reply(self) -> None:
        """第二段失败时准确保留第一段回执，不发送第三段或重复全文。"""
        await self.prepare()
        self.host.send_modes = ["success", "failed"]
        first = await self.send(LONG_REPLY)
        self.assertFalse(first["success"])
        self.assertEqual(first["sent_parts"], 1)
        self.assertEqual(first["message_ids"], ["sent-1"])
        self.assertEqual(await self.send(LONG_REPLY), first)
        self.assertEqual(len(self.host.sent), 2)

    async def test_cancellation_after_first_part_keeps_confirmed_receipt(self) -> None:
        """第二段结果未知时仍保留第一段成功，并且不重试未知段或续发。"""
        await self.prepare()
        self.host.send_modes = ["success", "hold"]
        task = asyncio.create_task(self.send(LONG_REPLY))
        await asyncio.wait_for(self.host.held_send_started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        receipt = await self.send(LONG_REPLY)
        self.assertFalse(receipt["success"])
        self.assertEqual(receipt["sent_parts"], 1)
        self.assertEqual(receipt["message_ids"], ["sent-1"])
        self.assertEqual(len(self.host.sent), 2)

    async def test_new_message_after_first_part_stops_old_reply(self) -> None:
        """发送第一段期间到来的新消息阻止旧正文继续发送。"""
        await self.prepare()
        self.host.new_message_after = 1
        receipt = await self.send(LONG_REPLY)
        self.assertFalse(receipt["success"])
        self.assertEqual(receipt["sent_parts"], 1)
        self.assertEqual(len(self.host.sent), 1)
        self.assertFalse((await self.send(LONG_REPLY))["success"])
        self.assertEqual(len(self.host.sent), 1)

    async def test_split_parts_only_discount_first_typing_delay(self) -> None:
        """真实等待插件只抵扣首段，后两段保持原生逐段打字时间。"""
        timing = timing_module.QQBotReplyTimingPlugin()
        timing.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        timing._set_context(PluginContext("mlj.qqbot-reply-timing", self.host.rpc))
        await timing.on_load()
        now = [10.0]
        waits: list[float] = []
        flags: list[bool] = []

        async def sleep(seconds: float) -> None:
            waits.append(seconds)
            now[0] += seconds

        rpc = self.host.rpc

        async def routed_rpc(method: str, plugin_id: str, payload: dict[str, Any], **kwargs: Any) -> Any:
            if payload.get("capability") == "send.text":
                args = payload["args"]
                result = await timing.before_send(
                    message={"platform": "qq", "session_id": SESSION,
                             "message_id": f"delivery-{len(flags)}", "processed_plain_text": args["text"]},
                    typing=args["typing"],
                )
                typing = result.get("modified_kwargs", {}).get("typing", args["typing"])
                flags.append(typing)
                if typing:
                    await sleep(timing_module.normal_delay(args["text"], 0.2))
            return await rpc(method, plugin_id, payload, **kwargs)

        with patch.object(timing_module, "time", SimpleNamespace(monotonic=lambda: now[0])), \
                patch.object(timing_module, "asyncio", SimpleNamespace(sleep=sleep)):
            await timing.receive(message={"platform": "qq", "session_id": SESSION, "message_id": "source-1"})
            await timing.prepare(session_id=SESSION, item_schema_version=1, items=[{
                "item_type": "UserMessageItem",
                "parts": [{"type": "text", "text": '<message msg_id="source-1" sender="主人">消息</message>'}],
            }])
            now[0] += 1
            self.plugin._set_context(PluginContext("mlj.qqbot-reply-guard", routed_rpc))
            await self.prepare()
            text = ("确" * 60 + "。") * 3
            self.assertTrue((await self.send(text))["success"])
        delay = timing_module.normal_delay("确" * 60 + "。", 0.2)
        self.assertEqual(flags, [False, True, True])
        self.assertEqual(len(waits), 3)
        self.assertAlmostEqual(waits[0], delay - 1)
        self.assertAlmostEqual(waits[1], delay)
        self.assertAlmostEqual(waits[2], delay)

    async def test_progress_then_final_preserves_native_send_contract(self) -> None:
        """阶段答复后机器人消息不会阻止同一来源的最终补充。"""
        prepared = await self.prepare()
        self.assertIn("公开答复规则", prepared["items"][-1]["parts"][0]["text"])
        first = await self.send(phase="progress")
        self.assertTrue(first["success"])
        self.assertEqual(first["message_id"], "sent-1")
        self.assertNotIn("pause_execution", first)
        self.assertEqual(await self.plugin._pending_message_id(SESSION), "")
        await self.prepare()
        final = await self.send("库存记录是 427 件，比计算值少 5 件。")
        self.assertTrue(final["success"])
        self.assertEqual(len(self.host.sent), 2)
        for sent in self.host.sent:
            self.assertEqual(sent["stream_id"], SESSION)
            self.assertTrue(sent["sync_to_maisaka_history"])
            self.assertTrue(sent["storage_message"])
            self.assertEqual(sent["maisaka_source_kind"], "outbound_send")
            self.assertTrue(sent["typing"])
            self.assertFalse(sent["set_reply"])

    async def test_concurrent_duplicate_sends_once(self) -> None:
        """同文并发调用共享平台回执，避免网络重入造成重复发送。"""
        await self.prepare()
        first, second = await asyncio.gather(self.send(), self.send())
        self.assertTrue(first["success"])
        self.assertEqual(first, second)
        self.assertEqual(len(self.host.sent), 1)

    async def test_new_owner_message_rejects_old_answer(self) -> None:
        """新消息尚未进入 Planner 时，也不能发送过期目标。"""
        await self.prepare()
        self.host.messages.append(message("source-2"))
        self.assertFalse((await self.send())["success"])
        self.assertEqual(self.host.sent, [])

    async def test_other_chat_cannot_use_visible_tool(self) -> None:
        """其他会话的请求中移除工具，调用端也拒绝跨会话发送。"""
        await self.prepare()
        prepared = await self.prepare(session_id="another-chat")
        self.assertEqual([t["function"]["name"] for t in prepared["tool_definitions"]], ["reply"])
        result = await self.plugin.send_reply(msg_id="source-1", text="不应发送", stream_id="another-chat")
        self.assertFalse(result["success"])
        self.assertEqual(self.host.sent, [])

    async def test_unseen_source_cannot_grant_send(self) -> None:
        """消息 API 知道目标但当前规划未看到目标时，不提供发送入口。"""
        prepared = await self.prepare(message_id="unseen")
        self.assertEqual([t["function"]["name"] for t in prepared["tool_definitions"]], ["reply"])
        self.assertFalse((await self.send())["success"])

    async def test_owner_binding_change_blocks_send(self) -> None:
        """身份插件里的主人变化会使旧消息失去发送资格。"""
        await self.prepare()
        self.host.owner = "987654321"
        with self.assertRaises(ValueError):
            await self.send()
        self.assertEqual(self.host.sent, [])

    async def test_config_reload_invalidates_grant(self) -> None:
        """配置重载不能继续使用先前登记的目标。"""
        await self.prepare()
        await self.plugin.on_config_update(CONFIG_RELOAD_SCOPE_SELF, {}, "0.1.0")
        self.assertFalse((await self.send())["success"])

    async def test_failed_send_is_not_claimed_success_or_retried(self) -> None:
        """平台未确认成功时不写成功回执，也不重复同文发送。"""
        await self.prepare()
        self.host.send_mode = "failed"
        first, second = await self.send(), await self.send()
        self.assertFalse(first["success"])
        self.assertEqual(first, second)
        self.assertEqual(len(self.host.sent), 1)

    async def test_transport_error_keeps_uncertain_receipt(self) -> None:
        """发送异常保留未知结果，防止自动重试造成重复 QQ 消息。"""
        await self.prepare()
        self.host.send_mode = "error"
        self.assertFalse((await self.send())["success"])
        self.assertFalse((await self.send())["success"])
        self.assertEqual(len(self.host.sent), 1)

    async def test_cancelled_send_is_not_reissued(self) -> None:
        """发送交给平台后取消，后续调用不推断平台未发出。"""
        await self.prepare()
        self.host.send_mode = "hold"
        task = asyncio.create_task(self.send())
        await asyncio.wait_for(self.host.send_started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse((await self.send())["success"])
        self.assertEqual(len(self.host.sent), 1)

    async def test_tool_response_does_not_trigger_repair_model(self) -> None:
        """明确的发送工具调用由原生循环执行，不额外请求漏执行核对模型。"""
        await self.prepare()
        result = await self.plugin.complete_reply(session_id=SESSION, item_schema_version=1, output_items=[
            {"item_type": "FunctionCallItem", "meta": _meta(), "tool_call": {
                "call_id": "call-1", "func_name": "send_reply", "args": {"msg_id": "source-1", "text": "公开答复"},
            }},
        ])
        self.assertEqual(result, {"action": "continue"})
        self.assertEqual(self.host.sent, [])

    async def test_same_batch_final_reply_uses_one_delivery_path(self) -> None:
        """同批两个最终回复入口只保留直接答复，不移除后续研究工具。"""
        await self.prepare()
        direct = {"item_type": "FunctionCallItem", "meta": _meta(), "tool_call": {
            "call_id": "direct", "func_name": "send_reply", "args": {"msg_id": "source-1", "text": "已经确认的答复"},
        }}
        native = {"item_type": "FunctionCallItem", "meta": _meta(), "tool_call": {
            "call_id": "native", "func_name": "reply", "args": {"msg_id": "source-1"},
        }}
        research = {"item_type": "FunctionCallItem", "meta": _meta(), "tool_call": {
            "call_id": "research", "func_name": "query_memory", "args": {"query": "继续核对"},
        }}
        result = await self.plugin.complete_reply(
            session_id=SESSION, output_items=[direct, native, research], item_schema_version=1,
        )
        self.assertEqual(result["modified_kwargs"]["output_items"], [direct, research])

    async def test_source_leaving_context_invalidates_previous_grant(self) -> None:
        """来源移出当前规划后，旧的授权快照不能用于直接发送。"""
        await self.prepare()
        await self.prepare(message_id="unseen")
        self.assertFalse((await self.send())["success"])
        self.assertEqual(self.host.sent, [])

    async def test_same_batch_attachments_keep_native_delivery(self) -> None:
        """直接正文不能吞掉同批由原生回复工具携带的图片、表情和提及。"""
        await self.prepare()
        direct = {"item_type": "FunctionCallItem", "meta": _meta(), "tool_call": {
            "call_id": "direct", "func_name": "send_reply", "args": {"msg_id": "source-1", "text": "已确认的正文"},
        }}
        for name, value in (("attach_pic", [{"msg_id": "source-1", "index": 0}]),
                            ("attach_emoji", "开心"), ("attach_at", ["source-1"])):
            with self.subTest(attachment=name):
                native = {"item_type": "FunctionCallItem", "meta": _meta(), "tool_call": {
                    "call_id": "native", "func_name": "reply", "args": {"msg_id": "source-1", name: value},
                }}
                result = await self.plugin.complete_reply(
                    session_id=SESSION, output_items=[direct, native], item_schema_version=1,
                )
                self.assertEqual(result, {"action": "continue"})

    async def test_same_batch_progress_preserves_native_followup(self) -> None:
        """公开阶段结论不能阻止后续由原生回复器组织的完整答复。"""
        await self.prepare()
        calls = [
            {"item_type": "FunctionCallItem", "meta": _meta(), "tool_call": {
                "call_id": "progress", "func_name": "send_reply",
                "args": {"msg_id": "source-1", "text": "已经能确定的部分", "phase": "progress"},
            }},
            {"item_type": "FunctionCallItem", "meta": _meta(), "tool_call": {
                "call_id": "native", "func_name": "reply", "args": {"msg_id": "source-1"},
            }},
        ]
        result = await self.plugin.complete_reply(session_id=SESSION, output_items=calls, item_schema_version=1)
        self.assertEqual(result, {"action": "continue"})


class ReplySplitterTests(TestCase):
    """检查正文完整性与需要保持完整的文本结构。"""

    def test_punctuation_and_paragraphs_preserve_every_character(self) -> None:
        text = "第一段内容要保留。\n\n第二段提问要保留？第三段感叹也要保留！最后一句完整保留。"
        parts = split_reply(text, 3)
        self.assertEqual(len(parts), 3)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(part.strip() for part in parts))

    def test_links_code_quotes_and_numbers_are_not_cut_inside(self) -> None:
        protected = ("https://example.invalid/a?q=x,y;z=2", '`first, second; third`',
                     '“引用里有逗号，句号。和问号？”', "（说明里有逗号，句号。）",
                     '```python\nprint("a,b")\nprint("c")\n```', "1,234,567.89")
        text = "。".join(protected) + "。最后保留这段完整结论。"
        parts = split_reply(text, 20)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)
        for fragment in protected:
            with self.subTest(fragment=fragment):
                self.assertTrue(any(fragment in part for part in parts))

    def test_short_chat_is_not_forced_into_tiny_messages(self) -> None:
        self.assertEqual(split_reply("好呀，晚上见！", 3), ["好呀，晚上见！"])
        self.assertEqual(split_reply("第一段\n第二段", 3), ["第一段\n", "第二段"])

    def test_long_answer_is_not_replaced_or_truncated(self) -> None:
        text = ("这段回答保留完整的事实和依据。" * 100)[:2000]
        parts = split_reply(text, 3)
        self.assertEqual(len(parts), 3)
        self.assertEqual("".join(parts), text)
