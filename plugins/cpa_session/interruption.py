"""按真实源消息关联生成任务，首条送达前淘汰过期请求，不改聊天上下文。"""
from collections import OrderedDict
from dataclasses import dataclass
from html import escape
from typing import Any

import asyncio

from src.llm_models.model_client.base_client import APIResponse, BaseClient, ResponseRequest


@dataclass
class ReplyTurn:
    """一轮回复持有接收列表引用；后续轮次不改写本轮的发送状态。"""

    source_id: str | None
    received: list[str]
    first_sent: bool = False
    proactive_item_id: str = ""

    @property
    def stale(self) -> bool:
        """首条未送达且已经收到更新的真实消息时，本轮失效。"""
        return not self.first_sent and bool(self.received) and self.source_id != self.received[-1]


class ReplyInterruption:
    """仅管理本插件的请求任务，消息重收集与回复生成仍由原生循环负责。"""

    def __init__(self) -> None:
        self.received: OrderedDict[str, list[str]] = OrderedDict()
        self.turns: dict[str, ReplyTurn] = {}
        self.reply_turns: dict[str, ReplyTurn] = {}
        self.pending: dict[asyncio.Task[APIResponse], ReplyTurn] = {}
        self.closing = False

    def observe(self, message: dict[str, Any]) -> None:
        """重复消息不算新输入；只取消尚未公开回复的过期模型任务。"""
        session_id, message_id = message.get('session_id'), message.get('message_id')
        if (self.closing or message.get('platform') != 'qq' or message.get('is_command')
                or not isinstance(session_id, str) or not session_id
                or not isinstance(message_id, str) or not message_id):
            return
        received = self.received.setdefault(session_id, [])
        self.received.move_to_end(session_id)
        if message_id in received:
            return
        received.append(message_id)
        del received[:-64]
        for task, turn in self.pending.items():
            if turn.received is received and turn.stale:
                task.cancel()
        # 活动请求保留其列表引用；只回收没有生成任务的旧会话。
        for old_id in list(self.received)[:-4096]:
            old = self.received[old_id]
            if any(turn.received is old for turn in self.pending.values()):
                continue
            self.received.pop(old_id)
            self.turns.pop(old_id, None)
            self.reply_turns.pop(old_id, None)

    def bind(self, session_id: str, items: list[dict[str, Any]], replyer: bool) -> None:
        """以请求实际包含的最新源消息建轮，缺少更新消息的快照保持过期。"""
        if replyer:
            turn = self.turns.get(session_id)
            if turn is not None:
                self.reply_turns[session_id] = turn
            return
        received = self.received.setdefault(session_id, [])
        texts = [part.get('text', '') for item in items if item.get('item_type') == 'UserMessageItem'
                 for part in item.get('parts', []) if part.get('type') == 'text']
        source_id = next((source for source in reversed(received)
                          if any(text.startswith(f'<message msg_id="{escape(source, quote=True)}" ') for text in texts)), None)
        proactive_item_id = next((
            item['meta']['item_id'] for item in reversed(items)
            if item.get('item_type') == 'UserMessageItem' and any(
                part.get('type') == 'text' and part.get('text', '').startswith('<plugin_proactive_task ')
                for part in item.get('parts', [])
            )
        ), '')
        current = self.turns.get(session_id)
        # 主动续聊可以复用同一条用户消息，但不能继承上一轮已经送达的状态。
        if (current is None or current.source_id != source_id
                or proactive_item_id and current.proactive_item_id != proactive_item_id):
            self.turns[session_id] = ReplyTurn(source_id, received, proactive_item_id=proactive_item_id)

    async def request(
        self, client: BaseClient, request: ResponseRequest, turn: ReplyTurn | None,
    ) -> APIResponse | None:
        """过期请求不发起或立即取消；外部卸载/超时取消继续向调用方传播。"""
        if turn is not None and turn.stale:
            return None
        task = asyncio.create_task(client.get_response(request))
        if turn is not None:
            self.pending[task] = turn
        try:
            response = await task
            return None if turn is not None and turn.stale else response
        except asyncio.CancelledError:
            if not self.closing and turn is not None and turn.stale:
                return None
            raise
        finally:
            self.pending.pop(task, None)

    async def close(self) -> None:
        """卸载时取消并收束本插件请求，清除瞬时关联。"""
        self.closing = True
        tasks = list(self.pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.received.clear()
        self.turns.clear()
        self.reply_turns.clear()
