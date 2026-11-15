"""用真实 SDK 与原生请求类型检查完整媒体、历史翻页及会话隔离，隔离外部通信。"""
from copy import deepcopy
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import base64
import json

from PIL import Image
from maibot_sdk.context import PluginContext
from plugins.qqbot_visual import plugin as module
from plugins.qqbot_visual.history import read_history
from src.chat.message_receive.bot import register_chat_hook_specs
from src.maisaka.chat_loop_service import register_maisaka_hook_specs
from src.plugin_runtime.component_query import ComponentQueryService
from src.plugin_runtime.host.component_registry import ComponentRegistry
from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry
from src.plugin_runtime.runner.plugin_loader import PluginLoader


def picture():
    """构造真实可解码图片，不连接图片或模型服务。"""
    stream = BytesIO()
    Image.new('RGB', (2, 2), 'red').save(stream, 'PNG')
    return base64.b64encode(stream.getvalue()).decode()


def message(index, stamp='2026-11-14T10:00:00', session='session'):
    return {'platform': 'qq', 'session_id': session, 'message_id': f'{index:04}', 'timestamp': stamp,
            'message_info': {'user_info': {'user_id': '123', 'user_nickname': '测试用户'},
                             'group_info': {'group_id': '456'}, 'additional_config': {}},
            'raw_message': [{'type': 'text', 'data': f'原始消息{index}'}], 'reply_to': None}


class HistoryAPI:
    def __init__(self, rows):
        self.rows = rows
        self.logger = Mock()
        self.message = SimpleNamespace(get_by_id=AsyncMock(side_effect=self.get_by_id))

    async def get_by_id(self, message_id, **kwargs):
        return deepcopy(next((row for row in self.rows if row['message_id'] == message_id), None))

    async def call_capability(self, capability, **args):
        if capability == 'message.get_recent':
            return deepcopy(self.rows)
        if capability != 'message.get_by_time_in_chat':
            raise AssertionError(capability)
        rows = [row for row in self.rows if row['session_id'] == args['chat_id']
                and args['start_time'] <= datetime.fromisoformat(row['timestamp']).timestamp() <= args['end_time']]
        # 与仅按时间排序的数据库一致：同一时刻没有隐含的消息ID次序保证。
        rows.sort(key=lambda row: row['timestamp'], reverse=True)
        if args.get('limit'):
            rows = rows[:args['limit']]
        return deepcopy(rows)


class MessageContextTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = module.QQBotVisualPlugin()
        self.plugin.set_plugin_config({'plugin': {'enabled': True, 'config_version': '0.1.0'}})
        self.plugin._set_context(PluginContext('mlj.qqbot-visual', AsyncMock()))
        self.plugin._bot_id = '999'

        self.addAsyncCleanup(self.plugin.on_unload)

    async def test_unmentioned_group_media_and_nested_forward_remain_complete(self):
        incoming = message(1)
        incoming['raw_message'] = [{'type': 'image', 'data': '', 'hash': 'source', 'binary_data_base64': picture()},
                                   {'type': 'forward', 'data': [{'content': [
                                       {'type': 'emoji', 'data': '', 'binary_data_base64': picture()}]}]}]
        saved = deepcopy(incoming)
        result = await self.plugin.read_video(message=incoming)
        self.assertEqual(result, {'action': 'continue'})
        self.assertEqual(incoming, saved)

    async def test_history_paging_does_not_lose_same_timestamp_messages(self):
        api = HistoryAPI([message(i) for i in range(100)])
        seen, before = [], ''
        for _ in range(5):
            result = await read_history(api, 'session', 30, before)
            self.assertTrue(result['success'])
            page = json.loads(result['content'])
            seen.extend(row['message_id'] for row in page['messages'])
            before = page['next_before_message_id']
            if not before:
                break
        self.assertEqual(len(seen), 100)
        self.assertEqual(set(seen), {f'{i:04}' for i in range(100)})
        self.assertEqual(len(seen), len(set(seen)))

    async def test_history_image_is_real_native_tool_media(self):
        row = message(1)
        encoded = picture()
        row['raw_message'] = [{'type': 'text', 'data': '完整原文'},
                              {'type': 'image', 'data': '原描述', 'binary_data_base64': encoded, 'hash': 'image'}]
        result = await read_history(HistoryAPI([row]), 'session')
        self.assertTrue(result['success'])
        media = ComponentQueryService._parse_tool_content_items(result['content_items'])
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].content_type, 'image')
        self.assertEqual(media[0].data, encoded)
        self.assertEqual(media[0].mime_type, 'image/png')
        content = json.loads(result['content'])['messages'][0]
        self.assertEqual(content['content'][0]['data'], '完整原文')
        self.assertNotIn(encoded, result['content'])

    async def test_history_rejects_foreign_cursor_and_invalid_count(self):
        api = HistoryAPI([message(1, session='foreign')])
        self.assertFalse((await read_history(api, 'session', 30, '0001'))['success'])
        for count in [True, 0, 101, '30']:
            self.assertFalse((await read_history(api, 'session', count))['success'])

    async def test_history_query_failure_is_not_empty_success(self):
        api = HistoryAPI([])
        api.call_capability = AsyncMock(side_effect=OSError('isolated failure'))
        result = await read_history(api, 'session')
        self.assertFalse(result['success'])
        self.assertNotIn('content_items', result)

    async def test_history_tool_is_available_in_groups_and_keeps_bilibili_private(self):
        tools = {entry['name']: entry for entry in self.plugin.get_components() if entry['type'] == 'TOOL'}
        self.assertNotEqual(tools['read_chat_history'].get('chat_scope'), 'private')
        for name in ['bilibili_search', 'bilibili_watch', 'watch_local_video']:
            if name in tools:
                self.assertEqual(tools[name]['chat_scope'], 'private')
        with patch.object(module, 'read_history', new=AsyncMock(return_value={'success': True})) as reader:
            await self.plugin.read_chat_history(stream_id='actual-session', count=30)
            reader.assert_awaited_once_with(self.plugin.ctx, 'actual-session', 30, '')

    async def test_ordinary_image_and_per_turn_history_layers_are_removed(self):
        names = {entry['name'] for entry in self.plugin.get_components()}
        self.assertTrue(names.isdisjoint({'qqbot_visual_receive_gate', 'qqbot_visual_private_planner',
                                         'qqbot_visual_private_replyer', 'prepare_history'}))

    async def test_actual_host_registration_accepts_all_components(self):
        specs = HookSpecRegistry()
        register_chat_hook_specs(specs)
        register_maisaka_hook_specs(specs)
        registry = ComponentRegistry(hook_spec_registry=specs)
        PluginLoader._validate_sdk_plugin_contract('mlj.qqbot-visual', self.plugin)
        entries = [{**entry, 'component_type': entry['type']} for entry in self.plugin.get_components()]
        self.assertEqual(registry.register_plugin_components('mlj.qqbot-visual', entries), len(entries))

    async def test_unmentioned_group_image_enters_native_context_directly(self):
        from src.common.data_models.message_component_data_model import ImageComponent, MessageSequence, TextComponent
        from src.maisaka.context.messages import SessionBackedMessage
        encoded = picture()
        raw = MessageSequence([TextComponent('原始正文'), ImageComponent(binary_hash='source', binary_data=base64.b64decode(encoded))])
        native = SessionBackedMessage(raw, '群内未@的图片', datetime.now(), message_id='0001')
        for stage in ('planner', 'replyer'):
            with self.subTest(stage=stage):
                item = native.to_context_item(enable_visual_message=True)
                images = [part for part in item.parts if type(part).__name__ == 'ContextImagePart']
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0].image_base64, encoded)

    async def test_broken_history_image_does_not_drop_other_messages(self):
        row = message(1)
        row['raw_message'] = [{'type': 'image', 'binary_data_base64': 'broken'},
                              {'type': 'text', 'data': '仍需保留的完整文字'},
                              {'type': 'image', 'binary_data_base64': picture()}]
        result = await read_history(HistoryAPI([row]), 'session')
        self.assertTrue(result['success'])
        self.assertIn('仍需保留的完整文字', result['content'])
        self.assertEqual(len(result['content_items']), 1)

    async def test_history_recovers_every_image_in_one_message(self):
        row = message(1)
        row['raw_message'] = [{'type': 'image', 'data': f'原描述{i}'} for i in range(9)]
        api = HistoryAPI([row])
        async def call(name, **args):
            if name == 'adapter.napcat.message.get_msg':
                return {'message_id': '0001', 'message_type': 'group', 'group_id': '456',
                        'sender': {'user_id': '123'}, 'message': [
                            {'type': 'image', 'data': {'file': f'verified-{i}'}} for i in range(9)]}
            self.assertEqual(name, 'adapter.napcat.file.get_image')
            return {'status': 'ok', 'data': {'base64': picture()}}
        api.api = SimpleNamespace(call=AsyncMock(side_effect=call))
        result = await read_history(api, 'session')
        self.assertTrue(result['success'])
        self.assertEqual(len(result['content_items']), 9)
        self.assertIn('原描述8', result['content'])

    async def test_history_paging_completes_older_timestamp_boundaries(self):
        rows = [message(i, stamp=f'2026-11-14T10:00:{i // 40:02}') for i in range(120)]
        api = HistoryAPI(rows)
        before, seen = '', []
        for _ in range(10):
            result = await read_history(api, 'session', 17, before)
            self.assertTrue(result['success'])
            page = json.loads(result['content'])
            seen.extend(row['message_id'] for row in page['messages'])
            before = page['next_before_message_id']
            if not before:
                break
        self.assertEqual(len(seen), 120)
        self.assertEqual(len(set(seen)), 120)
