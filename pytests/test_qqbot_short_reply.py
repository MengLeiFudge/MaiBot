"""实际执行原生 reply、后处理和 Platform IO，隔离模型、聊天存储及 QQ 出口。"""
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from maibot_sdk.context import PluginContext

from plugins.qqbot_reply_guard.plugin import QQBotReplyGuardPlugin
from plugins.qqbot_reply_timing import plugin as timing
from src.chat.message_receive.message import SessionMessage
from src.chat.utils import utils as chat_utils
from src.common.data_models.mai_message_data_model import MessageInfo, UserInfo
from src.common.data_models.message_component_data_model import AtComponent, MessageSequence, TextComponent
from src.common.data_models.reply_generation_data_models import ReplyGenerationResult
from src.core.tooling import ToolInvocation
from src.maisaka.builtin_tool import reply
from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
from src.platform_io.drivers.base import PlatformIODriver
from src.platform_io.manager import PlatformIOManager
from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverDescriptor, DriverKind, RouteBinding, RouteKey
from src.plugin_runtime.host.hook_dispatcher import HookDispatchResult
from src.services import send_service


class ShortReplyTests(IsolatedAsyncioTestCase):
    """覆盖正文完整性、长度边界、原生续条、附件和明确失败。"""

    async def asyncSetUp(self):
        self.account = "3600812036"
        self.scope = "lanling"
        self.clock = 100.0
        self.waits = []
        self.deliveries = []
        self.fail_delivery = False
        self.max_length = 512
        self.text = "还是 403 打不开，服务器直接把请求挡住了……不是不想看呀。\n\n直接发文件过来就好。"
        self.guard = QQBotReplyGuardPlugin()
        self.guard.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        self.guard._set_context(PluginContext("mlj.qqbot-reply-guard", self.rpc))
        await self.guard.on_load()
        self.timing = timing.QQBotReplyTimingPlugin()
        self.timing.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        self.timing._set_context(PluginContext("mlj.qqbot-reply-timing", self.rpc))
        await self.timing.on_load()
        self.source = SessionMessage(message_id="source-1", platform="qq", timestamp=datetime.now())
        self.source.session_id = "isolated-session"
        self.source.raw_message = MessageSequence([TextComponent("隔离消息")])
        self.source.message_info = MessageInfo(user_info=UserInfo(user_id="123456789", user_nickname="隔离用户"))
        self.stream = SimpleNamespace(session_id=self.source.session_id, platform="qq", account_id=self.account,
                                      user_id="123456789", group_id="", group_name="", context=None)
        self.runtime = SimpleNamespace(session_id=self.stream.session_id, chat_stream=self.stream,
            _chat_history=[], log_prefix="[隔离验证]", find_source_message_by_id=lambda _: self.source,
            _update_stage_status=lambda *args: None)
        self.context = BuiltinToolRuntimeContext(cast(Any, None), cast(Any, self.runtime))
        self.store = AsyncMock()
        self.history = Mock()
        self.broker = PlatformIOManager()
        owner = self

        class CaptureDriver(PlatformIODriver):
            async def start(self):
                pass

            async def stop(self):
                pass

            async def send_message(self, message, route_key, metadata=None):
                owner.deliveries.append((message, route_key))
                return DeliveryReceipt(internal_message_id=message.message_id, route_key=route_key,
                    status=DeliveryStatus.FAILED if owner.fail_delivery else DeliveryStatus.SENT,
                    driver_id=self.driver_id, driver_kind=DriverKind.PLUGIN)

        driver = CaptureDriver(DriverDescriptor(driver_id="isolated-napcat", kind=DriverKind.PLUGIN,
                                                platform="qq", account_id=self.account, scope=self.scope))
        self.broker.register_driver(driver)
        self.broker.bind_send_route(RouteBinding(route_key=RouteKey("qq", self.account, self.scope),
                                                driver_id=driver.driver_id, driver_kind=DriverKind.PLUGIN))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for obj, key, value in [
            (timing, "time", SimpleNamespace(monotonic=lambda: self.clock)),
            (timing, "asyncio", SimpleNamespace(sleep=self.sleep)),
            (send_service, "asyncio", SimpleNamespace(sleep=self.sleep)),
            (send_service, "_get_runtime_manager", lambda: self),
            (self.context, "get_runtime_manager", lambda: self),
            (send_service, "_chat_manager", SimpleNamespace(get_session_by_session_id=lambda _: self.stream)),
            (send_service, "get_platform_io_manager", lambda: self.broker),
            (self.broker, "ensure_send_pipeline_ready", AsyncMock()),
            (send_service, "_store_sent_message", self.store),
            (send_service, "_notify_memory_automation_on_message_sent", AsyncMock()),
            (send_service, "_sync_sent_message_to_maisaka_history", self.history),
            (send_service, "_record_sent_emoji_usage", Mock()),
            (send_service, "_dispatch_adapter_callbacks", AsyncMock()),
            (reply.replyer_manager, "get_replyer", lambda **kwargs: SimpleNamespace(generate_reply_with_context=self.generate)),
            (send_service.global_config.response_post_process, "typing_speed", 0.2),
            (chat_utils.global_config.response_post_process, "enable_response_post_process", True),
            (chat_utils.global_config.response_splitter, "enable", True),
            (chat_utils.global_config.response_splitter, "max_length", 512),
            (chat_utils.global_config.chinese_typo, "enable", False),
            (reply.config_module.global_config.experimental, "enable_rich_reply", False),
        ]:
            self.stack.enter_context(patch.object(obj, key, value))
        await self.timing.receive(message={"platform": "qq", "session_id": self.stream.session_id, "message_id": "source-1"})
        await self.timing.prepare(session_id=self.stream.session_id, item_schema_version=1, items=[{
            "item_type": "UserMessageItem", "parts": [{"type": "text", "text": '<message msg_id="source-1" sender="用户">问题</message>'}]}])

    async def rpc(self, method, plugin_id, payload, **kwargs):
        cap, args = payload["capability"], payload["args"]
        if cap == "config.get":
            values = {"response_post_process.typing_speed": 0.2, "bot.qq_account": self.account,
                      "response_splitter.max_length": self.max_length}
            return {"success": True, "value": values[args["key"]]}
        if cap == "config.get_plugin":
            return {"success": True, "value": {"napcat_server": {"connection_id": self.scope}}}
        raise AssertionError(cap)

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.clock += seconds

    async def invoke_hook(self, name, **kwargs):
        handlers = {
            "maisaka.reply.before_post_process": [self.guard.preserve_short_reply],
            "send_service.after_build_message": [self.timing.prepare_segment],
            "send_service.before_send": [self.timing.prepare_route, self.timing.before_send],
            "send_service.after_send": [self.timing.after_send],
        }.get(name, [])
        for handler in handlers:
            result = await handler(**kwargs)
            if result.get("action") == "abort":
                return HookDispatchResult(name, kwargs, aborted=True)
            kwargs.update(result.get("modified_kwargs", {}))
        return HookDispatchResult(name, kwargs)

    async def generate(self, **kwargs):
        result = ReplyGenerationResult(success=True)
        result.completion.response_text = self.text
        return True, result

    async def send_reply(self, **arguments):
        return await reply.handle_tool(self.context, ToolInvocation(tool_name="reply", call_id="isolated-call",
            arguments={"msg_id": "source-1", "set_quote": False, **arguments}))

    async def test_short_reply_reaches_gateway_with_all_punctuation_and_line_breaks(self):
        self.clock += 100
        with patch.object(chat_utils, "split_into_sentences_w_remove_punctuation", side_effect=AssertionError("短回复不应随机拆分")):
            result = await self.send_reply()
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.structured_content["reply_segments"], [self.text])
        self.assertEqual([message.processed_plain_text for message, _ in self.deliveries], [self.text])
        self.assertTrue(all(route == RouteKey("qq", self.account, self.scope) for _, route in self.deliveries))
        self.assertEqual(self.waits, [])
        self.assertEqual(self.store.await_count, 1)
        self.assertEqual(self.history.call_count, 1)

    async def test_chinese_sentence_spaces_become_line_breaks_at_gateway(self):
        cases = (
            ("资料看完了，条目也核对好了 接下来整理结果", "资料看完了，条目也核对好了\n接下来整理结果"),
            ("知道啦~  \t刚刚已经记下了", "知道啦~\n刚刚已经记下了"),
        )
        for original, expected in cases:
            with self.subTest(text=original):
                self.text = original
                self.clock += 100
                self.deliveries.clear()
                with patch.object(chat_utils, "split_into_sentences_w_remove_punctuation",
                                  side_effect=AssertionError("显式段落不应进入随机拆分")):
                    result = await self.send_reply()
                self.assertTrue(result.success, result.error_message)
                self.assertEqual(result.structured_content["reply_segments"], [expected])
                self.assertEqual([message.processed_plain_text for message, _ in self.deliveries], [expected])
                self.assertEqual("".join(expected.split()), "".join(original.split()))

    async def test_english_numbers_units_and_quoted_spaces_are_preserved(self):
        cases = (
            "状态是 403 Forbidden，先检查 API Key 再看日志",
            "英文 Hello world，数值 3.5 kg，状态 403 打不开",
            '原文是“机器 学习”，代码是`保存 记录`，状态是"待 处理"',
            "原文是「机器 学习」和『待 处理』，也有‘两个 词’和'待 处理'",
        )
        for original in cases:
            with self.subTest(text=original):
                self.text = original
                self.clock += 100
                self.deliveries.clear()
                result = await self.send_reply()
                self.assertTrue(result.success, result.error_message)
                self.assertEqual(result.structured_content["reply_segments"], [original])
                self.assertEqual([message.processed_plain_text for message, _ in self.deliveries], [original])

    async def test_length_boundary_applies_to_private_and_group_replies(self):
        self.max_length = 20
        for session in ("isolated-private", "isolated-group"):
            for length in (19, 20, 21):
                with self.subTest(session=session, length=length):
                    result = await self.guard.preserve_short_reply(session_id=session, response="中" * length)
                    if length <= 20:
                        self.assertFalse(result["modified_kwargs"]["enable_splitter"])
                    else:
                        self.assertEqual(result, {"action": "continue"})

    async def test_explicit_options_disabled_plugin_and_foreign_account_are_preserved(self):
        options = {"session_id": self.stream.session_id, "response": self.text,
                   "reply_message_id": "source-1", "reply_tool_args": {"reply_reference": "已确认的事实"},
                   "skip_post_process": False, "enable_splitter": True, "enable_chinese_typo": True}
        result = await self.guard.preserve_short_reply(**options)
        self.assertEqual(result["modified_kwargs"], {**options, "enable_splitter": False})
        for changes in ({"response": " \n "}, {"skip_post_process": True}, {"enable_splitter": False}):
            with self.subTest(changes=changes):
                self.assertEqual(await self.guard.preserve_short_reply(**{**options, **changes}), {"action": "continue"})
        self.account = "987654321"
        self.assertEqual(await self.guard.preserve_short_reply(**options), {"action": "continue"})
        self.account = "3600812036"
        self.guard.set_plugin_config({"plugin": {"enabled": False, "config_version": "0.1.0"}})
        self.assertEqual(await self.guard.preserve_short_reply(**options), {"action": "continue"})
        self.guard.set_plugin_config({"plugin": {"enabled": True, "config_version": "0.1.0"}})
        await self.guard.on_unload()
        self.assertEqual(await self.guard.preserve_short_reply(**options), {"action": "continue"})

    async def test_long_reply_keeps_native_splitter_and_later_segment_delays(self):
        self.text = ("说明" * 45 + "。\n") * 6
        self.clock += 100
        with patch.object(chat_utils, "split_into_sentences_w_remove_punctuation",
                          wraps=chat_utils.split_into_sentences_w_remove_punctuation) as splitter:
            result = await self.send_reply()
        splitter.assert_called_once()
        self.assertTrue(result.success, result.error_message)
        segments = result.structured_content["reply_segments"]
        self.assertGreater(len(segments), 1)
        self.assertEqual([message.processed_plain_text for message, _ in self.deliveries], segments)
        self.assertEqual(len(self.waits), len(segments) - 1)
        for delay, segment in zip(self.waits, segments[1:]):
            self.assertAlmostEqual(delay, timing.normal_delay(segment, 0.2))

    async def test_first_wait_uses_complete_short_reply_and_supplement_keeps_normal_wait(self):
        self.clock += 0.1
        result = await self.send_reply()
        self.assertTrue(result.success, result.error_message)
        self.assertAlmostEqual(self.waits[0], max(0, timing.normal_delay(self.text, 0.2) - 0.1))
        self.waits.clear()
        result = await self.send_reply()
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(len(self.waits), 1)
        self.assertAlmostEqual(self.waits[0], timing.normal_delay(self.text, 0.2))

    async def test_gateway_failure_does_not_store_or_report_success(self):
        self.clock += 100
        self.fail_delivery = True
        result = await self.send_reply()
        self.assertFalse(result.success)
        self.assertEqual(len(self.deliveries), 1)
        self.store.assert_not_awaited()
        self.history.assert_not_called()
        self.assertEqual(self.timing._turns[self.stream.session_id].first_delivery_id, "")

    async def test_new_source_still_cancels_unsent_short_reply(self):
        await self.timing.receive(message={"platform": "qq", "session_id": self.stream.session_id, "message_id": "source-2"})
        result = await self.send_reply()
        self.assertFalse(result.success)
        self.assertEqual(self.deliveries, [])
        self.store.assert_not_awaited()

    async def test_short_reply_keeps_rich_reply_attachments(self):
        self.clock += 100
        with patch.object(reply.config_module.global_config.experimental, "enable_rich_reply", True):
            result = await self.send_reply(attach_at=[{"user_id": "123456789"}])
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(len(self.deliveries), 1)
        message, _ = self.deliveries[0]
        self.assertIn(self.text, message.processed_plain_text)
        self.assertEqual("".join(item.text for item in message.raw_message.components
                                 if isinstance(item, TextComponent)), self.text)
        self.assertTrue(any(isinstance(item, AtComponent) and item.target_user_id == "123456789"
                            for item in message.raw_message.components))
