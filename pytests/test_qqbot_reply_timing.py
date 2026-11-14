"""隔离 SDK 发送出口，核对首条抵扣、续条速度和轮次边界。"""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import asyncio

from maibot_sdk.context import PluginContext
from plugins.qqbot_reply_timing import plugin as module
from plugins.qqbot_reply_timing.plugin import QQBotReplyTimingPlugin


class ReplyTimingTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = 100.0
        self.waits = []
        self.speed = 0.4
        self.plugin = QQBotReplyTimingPlugin()
        self.plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        self.plugin._set_context(PluginContext("mlj.qqbot-reply-timing", self.rpc))
        await self.plugin.on_load()
        self.time_patch = patch.object(module, "time", SimpleNamespace(monotonic=lambda: self.clock))
        self.sleep_patch = patch.object(module, "asyncio", SimpleNamespace(sleep=self.sleep))
        self.time_patch.start()
        self.sleep_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.addCleanup(self.sleep_patch.stop)
        await self.source("source-1")

    async def rpc(self, method, plugin_id, payload, **kwargs):
        self.assertEqual(payload["capability"], "config.get")
        self.assertEqual(payload["args"]["key"], "response_post_process.typing_speed")
        return {"success": True, "value": self.speed}

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.clock += seconds

    async def source(self, message_id):
        await self.plugin.receive(message={"session_id": "session", "platform": "qq", "message_id": message_id})
        await self.prepare(message_id)

    async def prepare(self, message_id):
        await self.plugin.prepare(session_id="session", item_schema_version=1, items=[{
            "item_type": "UserMessageItem", "parts": [{"type": "text", "text": f'<message msg_id="{message_id}" sender="用户">问题</message>'}],
        }])

    def outbound(self, message_id="delivery-1"):
        return {"session_id": "session", "platform": "qq", "message_id": message_id,
                "processed_plain_text": "中" * 30, "is_emoji": False}

    async def test_fast_generation_waits_only_remaining_budget(self):
        self.clock += 1
        result = await self.plugin.before_send(message=self.outbound(), typing=True, storage_message=True)
        self.assertAlmostEqual(self.waits[0], 2.6)
        self.assertFalse(result["modified_kwargs"]["typing"])
        self.assertTrue(result["modified_kwargs"]["storage_message"])

    async def test_slow_generation_sends_immediately(self):
        self.clock += 5
        result = await self.plugin.before_send(message=self.outbound(), typing=True)
        self.assertEqual(self.waits, [])
        self.assertFalse(result["modified_kwargs"]["typing"])

    async def test_later_segment_keeps_native_delay(self):
        self.clock += 5
        first = self.outbound()
        await self.plugin.before_send(message=first, typing=True)
        await self.plugin.after_send(message=first, sent=True)
        later = await self.plugin.before_send(message=self.outbound("delivery-2"), typing=True)
        self.assertNotIn("modified_kwargs", later)
        self.assertAlmostEqual(module.normal_delay("中" * 30, self.speed), 3.6)

    async def test_tool_round_does_not_create_another_first_message(self):
        self.clock += 5
        await self.plugin.before_send(message=self.outbound(), typing=True)
        await self.prepare("source-1")
        later = await self.plugin.before_send(message=self.outbound("delivery-2"), typing=True)
        self.assertNotIn("modified_kwargs", later)

    async def test_new_source_starts_new_budget(self):
        self.clock += 5
        await self.plugin.before_send(message=self.outbound(), typing=True)
        await self.source("source-2")
        self.clock += 1
        await self.plugin.before_send(message=self.outbound("delivery-2"), typing=True)
        self.assertAlmostEqual(self.waits[-1], 2.6)

    async def test_new_message_not_yet_in_context_aborts_old_first_segment(self):
        self.clock += 5
        await self.plugin.receive(message={"session_id": "session", "platform": "qq", "message_id": "not-in-context"})
        await self.prepare("source-1")
        result = await self.plugin.before_send(message=self.outbound(), typing=True)
        self.assertEqual(result, {"action": "abort"})
        self.assertEqual(self.waits, [])

    async def test_unknown_outcome_keeps_claim_but_explicit_failure_releases(self):
        self.clock += 5
        first = self.outbound()
        await self.plugin.before_send(message=first, typing=True)
        second = await self.plugin.before_send(message=self.outbound("delivery-2"), typing=True)
        self.assertNotIn("modified_kwargs", second)
        await self.plugin.after_send(message=first, sent=False)
        retried = await self.plugin.before_send(message=self.outbound("delivery-3"), typing=True)
        self.assertFalse(retried["modified_kwargs"]["typing"])

    async def test_concurrent_sends_only_one_gets_credit(self):
        self.clock += 5
        results = await asyncio.gather(*(self.plugin.before_send(message=self.outbound(f"delivery-{i}"), typing=True) for i in range(2)))
        self.assertEqual(sum("modified_kwargs" in result for result in results), 1)

    async def test_non_typing_and_unrelated_session_do_not_consume_first(self):
        await self.plugin.before_send(message=self.outbound(), typing=False)
        unrelated = self.outbound()
        unrelated["session_id"] = "other"
        result = await self.plugin.before_send(message=unrelated, typing=True)
        self.assertNotIn("modified_kwargs", result)
        self.clock += 5
        first = await self.plugin.before_send(message=self.outbound("real-first"), typing=True)
        self.assertFalse(first["modified_kwargs"]["typing"])

    async def test_cancelled_wait_never_grants_second_credit(self):
        async def cancelled_wait(seconds):
            raise asyncio.CancelledError
        with patch.object(module, "asyncio", SimpleNamespace(sleep=cancelled_wait)):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin.before_send(message=self.outbound(), typing=True)
        later = await self.plugin.before_send(message=self.outbound("delivery-2"), typing=True)
        self.assertNotIn("modified_kwargs", later)

    async def test_runtime_contract_and_config_reload_release_old_turn(self):
        from src.plugin_runtime.runner.plugin_loader import PluginLoader
        PluginLoader._validate_sdk_plugin_contract("mlj.qqbot-reply-timing", self.plugin)
        await self.plugin.on_config_update("self", {}, "0.1.0")
        self.assertEqual(len(self.plugin._turns), 0)
        self.assertEqual(len(self.plugin._received), 0)
        self.assertTrue(self.plugin._enabled())

    async def test_native_first_and_research_supplement_use_one_credit(self):
        """原生首段 typing=False 也进入计时，补充答复不能再抵扣。"""
        self.clock += 1
        message = self.outbound()
        built = await self.plugin.prepare_segment(message=message, processed_plain_text="中" * 30, typing=False)
        self.assertTrue(built["modified_kwargs"]["typing"])
        await self.plugin.before_send(message=message, typing=True)
        self.assertAlmostEqual(self.waits[0], 2.6)
        await self.plugin.after_send(message=message, sent=True)
        supplement = await self.plugin.prepare_segment(
            message=self.outbound("supplement"), processed_plain_text="中" * 30, typing=False)
        self.assertTrue(supplement["modified_kwargs"]["typing"])
        result = await self.plugin.before_send(message=self.outbound("supplement"), typing=True)
        self.assertNotIn("modified_kwargs", result)

    async def test_unprocessed_plugin_message_keeps_requested_typing(self):
        """没有后处理正文的插件通知不被改成模拟打字。"""
        result = await self.plugin.prepare_segment(message=self.outbound(), processed_plain_text="", typing=False)
        self.assertNotIn("modified_kwargs", result)

    async def test_new_message_wakes_first_wait_without_finishing_delay(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked_sleep(seconds):
            entered.set()
            await release.wait()
        with patch.object(module, "asyncio", SimpleNamespace(sleep=blocked_sleep)):
            task = asyncio.create_task(self.plugin.before_send(message=self.outbound(), typing=True))
            await asyncio.wait_for(entered.wait(), 1)
            await self.plugin.receive(message={"session_id": "session", "platform": "qq", "message_id": "source-2"})
            self.assertEqual(await asyncio.wait_for(task, 1), {"action": "abort"})
        self.assertFalse(release.is_set())
        self.assertIsNone(self.plugin._turns["session"].wait_task)

    async def test_new_message_during_first_wait_aborts_but_later_segments_continue(self):
        async def receive_during_wait(seconds):
            await self.plugin.receive(message={"session_id": "session", "platform": "qq", "message_id": "source-2"})
        with patch.object(module, "asyncio", SimpleNamespace(sleep=receive_during_wait)):
            result = await self.plugin.before_send(message=self.outbound(), typing=True)
        self.assertEqual(result, {"action": "abort"})
        self.assertEqual(self.plugin._turns["session"].first_delivery_id, "")
        await self.prepare("source-2")
        self.clock += 5
        first = self.outbound("new-first")
        await self.plugin.before_send(message=first, typing=True)
        await self.plugin.after_send(message=first, sent=True)
        await self.plugin.receive(message={"session_id": "session", "platform": "qq", "message_id": "source-3"})
        later = await self.plugin.before_send(message=self.outbound("second"), typing=True)
        self.assertEqual(later, {"action": "continue"})
