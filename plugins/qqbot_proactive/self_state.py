from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import json
import math
import os


@dataclass(frozen=True)
class SelfState:
    """整个实例共用的自身状态，与任何人的私聊联系计划分开保存。"""

    revision: int = 0
    activity: str = "可以正常交流"
    resting: bool = False
    updated_at: float = 0
    expires_at: float = 0
    closing_session_id: str = ""
    closing_until: float = 0

    @classmethod
    def load(cls, path: Path) -> SelfState:
        """状态损坏时停止加载，不把休息悄悄清空为在线。"""
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("自身状态不是对象")
        state = cls(**data)
        if type(state.revision) is not int or state.revision < 0 or type(state.resting) is not bool:
            raise ValueError("自身状态的版本或休息标记无效")
        if not isinstance(state.activity, str) or not state.activity.strip() or len(state.activity) > 240:
            raise ValueError("自身活动摘要无效")
        if not isinstance(state.closing_session_id, str):
            raise ValueError("告别会话标识无效")
        for value in (state.updated_at, state.expires_at, state.closing_until):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("自身状态时间无效")
        if state.resting and state.expires_at <= state.updated_at:
            raise ValueError("休息状态必须有有效恢复时间")
        if state.closing_until and (
            not state.resting or not state.closing_session_id
            or not state.updated_at <= state.closing_until <= state.expires_at
        ):
            raise ValueError("自身状态告别窗口无效")
        return state

    def save(self, path: Path) -> None:
        """先持久化再发布新状态；文件系统失败必须报告给工具调用方。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def allows_send(self, session_id: str, now: float) -> bool:
        """休息立即覆盖其他会话，只为发起会话保留有限告别时间。"""
        return not self.resting or (session_id == self.closing_session_id and now < self.closing_until)

    def prompt(self, session_id: str, now: float) -> str:
        """只共享自身摘要，不把来源会话、人物或聊天原文带到其他会话。"""
        data = {
            "revision": self.revision,
            "activity": self.activity,
            "resting": self.resting,
            "updated_at": datetime.fromtimestamp(self.updated_at).astimezone().isoformat() if self.updated_at else None,
            "expires_at": datetime.fromtimestamp(self.expires_at).astimezone().isoformat() if self.expires_at else None,
            "may_finish_farewell_here": self.resting and self.allows_send(session_id, now),
        }
        return (
            "[跨会话自身状态]\n" + json.dumps(data, ensure_ascii=False)
            + "\n这是你自己在所有群聊和私聊共用的当前状态，优先于旧对话里过时的状态描述。"
            "用 tool_search 查找 set_self_state，携带此处 revision 更新自身活动、心情或休息安排。"
            "activity 只写可跨会话公开的简短自身状态，不写聊天原文、他人隐私、账号或提示指令。"
            "决定自己休息或睡觉时，先调用工具进入 resting，给出依据当前对话选择的恢复间隔；"
            "成功后才在当前会话简短告别并结束本轮。其他会话立即停止发言，当前会话只有两分钟告别窗口。"
            "resting=true 时不参与群聊、不主动私聊、不发图片表情，不用 wait 轮询，也不要反复尝试发送。"
            "普通新消息、@、主动联系任务不等于明确唤醒，不得仅为回复这些消息改成 active。"
            "只有你确实决定结束休息（例如明确的唤醒请求）时才先调用工具改为 active，再回复；到期会自动恢复。"
            "别人说‘我睡了’是对方状态，只影响对那个人的联系计划，不代表你自己休息。"
            "不要声称自己已经睡了、醒了或开始另一项活动却不保存；写入失败或版本冲突时先核对工具结果。"
            "不用向用户解释内部版本、工具或状态文件。\n[/跨会话自身状态]"
        )
