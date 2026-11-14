"""首条送达前的新消息取消：真实 SDK、请求序列化和 Host 组件规格，隔离网络。"""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import asyncio

from maibot_sdk.context import PluginContext
from plugins.cpa_session import plugin as module
from plugins.cpa_session.plugin import CPASessionPlugin
from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.model_client.base_client import APIResponse, ResponseRequest
from src.llm_models.model_client.plugin_client import PluginLLMClient
from src.llm_models.request_snapshot import deserialize_context_items_snapshot, serialize_response_request_snapshot
from src.chat.message_receive.bot import register_chat_hook_specs
from src.maisaka.chat_loop_service import register_maisaka_hook_specs
from src.plugin_runtime.host.component_registry import ComponentRegistry
from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry
from src.plugin_runtime.runner.plugin_loader import PluginLoader
from src.services.send_service import register_send_service_hook_specs


def items(*sources):
    """保留原生消息头，不把引用文本里的消息编号认作新源。"""
    return [{'item_type': 'UserMessageItem', 'meta': {'item_id': source, 'logical_turn_id': None,
             'timestamp': datetime.now().isoformat()},
             'parts': [{'type': 'text', 'text': f'<message msg_id="{source}" sender="用户">隔离输入</message>'}]} for source in sources]


class ReplyInterruptionTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = CPASessionPlugin()
        self.plugin.set_plugin_config({'plugin': {'enabled': True, 'config_version': '0.1.0'}})
        self.plugin._set_context(PluginContext('mlj.cpa-session', AsyncMock()))
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.cancelled = False
        self.client = SimpleNamespace(get_response=self.generate, client=SimpleNamespace(close=AsyncMock()))
        self.client_patch = patch.object(module, 'OpenAIResponsesClient', return_value=self.client)
        self.factory = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addAsyncCleanup(self.plugin.on_unload)
        await self.receive('image')

    async def receive(self, source, session='session'):
        await self.plugin.receive(message={'platform': 'qq', 'session_id': session, 'message_id': source})

    async def generate(self, request):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        output = deserialize_context_items_snapshot([{
            'item_type': 'AssistantMessageItem', 'meta': {'item_id': 'output', 'logical_turn_id': request.logical_turn_id,
            'timestamp': datetime.now().isoformat()}, 'parts': [{'type': 'text', 'text': '已结合全部输入生成回复。'}]}])
        return APIResponse(output_items=cast(Any, tuple(output)))

    async def start_request(self, sources=('image',), replyer=False):
        context = items(*sources)
        await (self.plugin.replyer_session if replyer else self.plugin.planner_session)(session_id='session', items=context, tool_definitions=[{'type': 'function', 'function': {'name': 'reply'}}])
        request = serialize_response_request_snapshot(ResponseRequest(
            model_info=ModelInfo(name='isolated', model_identifier='isolated', api_provider='isolated'),
            context_items=deserialize_context_items_snapshot(context), tool_options=[], max_tokens=8192,
            extra_params={'reasoning': {'effort': 'high'}}))
        request['api_provider'] = APIProvider(name='isolated', base_url='http://127.0.0.1:9/v1', api_key='not-real').model_dump(mode='json')
        return asyncio.create_task(self.plugin.generate('response', request))

    async def test_message_during_image_preparation_skips_stale_request_then_merges(self):
        await self.receive('text')
        old = await self.start_request()
        result = await asyncio.wait_for(old, 1)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['output_items'], [])
        self.assertFalse(self.entered.is_set())
        rebuilt = await self.start_request(('image', 'text'))
        self.release.set()
        result = await asyncio.wait_for(rebuilt, 1)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.plugin._interruption.turns['session'].source_id, 'text')

    async def test_inflight_planner_is_cancelled_without_public_output(self):
        task = await self.start_request()
        await asyncio.wait_for(self.entered.wait(), 1)
        await self.receive('text')
        result = await asyncio.wait_for(task, 1)
        self.assertTrue(self.cancelled)
        self.assertIsNone(result['usage'])
        response = PluginLLMClient._build_api_response(result, 'isolated', 'isolated', require_output_items=True)
        self.assertFalse(response.output_items)
        self.assertFalse(self.plugin._interruption.pending)

    async def test_replyer_is_cancelled_before_first_message(self):
        await self.plugin.planner_session(session_id='session', tool_definitions=[{'type': 'function', 'function': {'name': 'reply'}}], items=items('image'))
        task = await self.start_request(replyer=True)
        await asyncio.wait_for(self.entered.wait(), 1)
        await self.receive('text')
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(self.cancelled)

    async def test_completed_old_plan_and_reply_are_suppressed(self):
        original = items('image')
        saved = deepcopy(original)
        await self.plugin.planner_session(session_id='session', tool_definitions=[{'type': 'function', 'function': {'name': 'reply'}}], items=original)
        await self.plugin.replyer_session(session_id='session', items=original)
        await self.receive('text')
        planned = await self.plugin.discard_old_plan(session_id='session', output_items=[{'tool': 'reply'}])
        self.assertEqual(planned['modified_kwargs']['output_items'], [])
        reply = await self.plugin.discard_old_reply(session_id='session', response='旧正文', enable_splitter=True)
        self.assertEqual(reply['modified_kwargs']['response'], '')
        self.assertTrue(reply['modified_kwargs']['enable_splitter'])
        self.assertEqual(await self.plugin.check_before_send(message={'session_id': 'session'}), {'action': 'abort'})
        self.assertEqual(original, saved)

    async def test_first_success_preserves_remaining_segments_and_research(self):
        await self.plugin.planner_session(session_id='session', tool_definitions=[{'type': 'function', 'function': {'name': 'reply'}}], items=items('image'))
        await self.plugin.replyer_session(session_id='session', items=items('image'))
        await self.plugin.mark_first_sent(message={'session_id': 'session'}, sent=True)
        task = await self.start_request(replyer=True)
        await asyncio.wait_for(self.entered.wait(), 1)
        await self.receive('text')
        self.assertFalse(self.cancelled)
        self.assertEqual(await self.plugin.check_before_send(message={'session_id': 'session'}), {'action': 'continue'})
        self.release.set()
        self.assertEqual((await asyncio.wait_for(task, 1))['status'], 'completed')
        await self.plugin.planner_session(session_id='session', tool_definitions=[{'type': 'function', 'function': {'name': 'reply'}}], items=items('image', 'text'))
        self.assertFalse(self.plugin._interruption.turns['session'].first_sent)

    async def test_failure_does_not_close_cancellation_window(self):
        await self.plugin.planner_session(session_id='session', tool_definitions=[{'type': 'function', 'function': {'name': 'reply'}}], items=items('image'))
        await self.plugin.replyer_session(session_id='session', items=items('image'))
        await self.plugin.mark_first_sent(message={'session_id': 'session'}, sent=False)
        await self.receive('text')
        self.assertEqual(await self.plugin.check_before_send(message={'session_id': 'session'}), {'action': 'abort'})

    async def test_duplicate_and_other_session_do_not_cancel(self):
        task = await self.start_request()
        await asyncio.wait_for(self.entered.wait(), 1)
        await self.receive('image')
        await self.receive('text', session='other')
        self.release.set()
        self.assertEqual((await asyncio.wait_for(task, 1))['status'], 'completed')
        self.assertFalse(self.cancelled)

    async def test_helper_request_keeps_parent_turn_and_session_headers(self):
        task = await self.start_request()
        await asyncio.wait_for(self.entered.wait(), 1)
        turn = self.plugin._interruption.turns['session']
        helper = [{'item_type': 'SystemMessageItem', 'meta': {'item_id': 'helper'},
                   'parts': [{'type': 'text', 'text': '隔离辅助请求'}]}]
        await self.plugin.planner_session(session_id='session', items=helper, tool_definitions=[])
        self.assertIs(self.plugin._interruption.turns['session'], turn)
        self.assertFalse(turn.stale)
        self.assertEqual(self.plugin._sessions['helper'], ('session', False))
        self.release.set()
        self.assertEqual((await asyncio.wait_for(task, 1))['status'], 'completed')

    async def test_unload_cancels_request_without_turning_shutdown_into_new_message(self):
        task = await self.start_request()
        await asyncio.wait_for(self.entered.wait(), 1)
        await self.plugin.on_unload()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.plugin._interruption.pending)

    async def test_actual_host_registration_and_partial_unload(self):
        registry = HookSpecRegistry()
        register_chat_hook_specs(registry)
        register_maisaka_hook_specs(registry)
        register_send_service_hook_specs(registry)
        components = ComponentRegistry(hook_spec_registry=registry)
        PluginLoader._validate_sdk_plugin_contract('mlj.cpa-session', self.plugin)
        entries = [{**entry, 'component_type': entry['type']} for entry in self.plugin.get_components()]
        self.assertEqual(components.register_plugin_components('mlj.cpa-session', entries), len(entries))
        await CPASessionPlugin().on_unload()

    async def test_native_scheduler_keeps_new_message_queued_when_old_cycle_stops(self):
        """取消后的无工具结束不会清空已排队的新消息，下一轮原生收集保留新输入。"""
        from unittest.mock import Mock
        from src.maisaka.runtime import MaisakaHeartFlowChatting
        from src.maisaka import turn_scheduler
        runtime = cast(Any, SimpleNamespace(
            session_id='isolated-session', chat_stream=SimpleNamespace(is_group_session=False),
            _agent_state='running', _STATE_RUNNING='running', _STATE_WAIT='wait', _STATE_STOP='stop',
            _message_turn_scheduled=False, _internal_turn_queue=asyncio.Queue(),
            _deferred_message_turn_task=None, _get_pending_message_count=lambda: 1,
            _get_effective_reply_frequency=lambda: 1.0, _is_reply_frequency_silent=lambda: False,
            _has_forced_turn_trigger=lambda: False, _idle_backoff=SimpleNamespace(should_delay=lambda _: False),
            _get_message_trigger_threshold=lambda: 1, log_prefix='[隔离调度]',
            _reset_consecutive_wait_count=Mock(), _clear_wait_state=Mock(),
            message_cache=['image', 'text'], _last_processed_index=1,
        ))
        runtime._cancel_deferred_message_turn_task = lambda: MaisakaHeartFlowChatting._cancel_deferred_message_turn_task(runtime)
        runtime._enqueue_message_turn = lambda: MaisakaHeartFlowChatting._enqueue_message_turn(runtime)
        with patch.object(turn_scheduler.focus_mode_manager, 'can_decide', return_value=True), \
                patch.object(turn_scheduler, 'is_reply_necessity_trigger_enabled', return_value=False):
            turn_scheduler.MessageTurnScheduler(runtime).schedule_message_turn()
        MaisakaHeartFlowChatting._enter_stop_state(runtime)
        self.assertEqual(runtime._internal_turn_queue.get_nowait(), 'message')
        with patch('src.maisaka.runtime.focus_mode_manager.mark_read'):
            self.assertEqual(MaisakaHeartFlowChatting._collect_pending_messages(runtime), ['text'])
        self.assertEqual(runtime.message_cache, ['image', 'text'])
