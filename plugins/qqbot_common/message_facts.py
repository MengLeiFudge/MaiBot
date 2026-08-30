from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class MessageFacts:
    """插件命令仲裁需要的稳定消息事实。"""

    text: str
    user_id: str
    group_id: str
    self_id: str
    timestamp: float
    at_target_ids: tuple[str, ...]

    @classmethod
    def from_kwargs(cls, kwargs: Mapping[str, Any]) -> "MessageFacts":
        """从 MaiBot Command/EventHandler 参数构造消息事实。"""

        message = kwargs.get("message")
        message_dict = message if isinstance(message, Mapping) else {}
        message_info = message_dict.get("message_info")
        message_info_dict = message_info if isinstance(message_info, Mapping) else {}
        additional = message_info_dict.get("additional_config")
        additional_dict = additional if isinstance(additional, Mapping) else {}
        raw_message = message_dict.get("raw_message")

        at_target_ids: list[str] = []
        if isinstance(raw_message, list):
            for segment in raw_message:
                if not isinstance(segment, Mapping) or segment.get("type") != "at":
                    continue
                data = segment.get("data")
                if not isinstance(data, Mapping):
                    continue
                target_id = str(data.get("target_user_id") or "").strip()
                if target_id and target_id not in at_target_ids:
                    at_target_ids.append(target_id)

        raw_timestamp = message_dict.get("timestamp")
        try:
            timestamp = float(raw_timestamp)
        except (TypeError, ValueError):
            timestamp = 0.0

        return cls(
            text=str(kwargs.get("text") or message_dict.get("processed_plain_text") or "").strip(),
            user_id=str(kwargs.get("user_id") or "").strip(),
            group_id=str(kwargs.get("group_id") or "").strip(),
            self_id=str(additional_dict.get("self_id") or "").strip(),
            timestamp=timestamp,
            at_target_ids=tuple(at_target_ids),
        )


def message_user_names(message: object) -> tuple[str, str]:
    """返回标准消息里的昵称和群名片。"""

    if not isinstance(message, Mapping):
        return "", ""
    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        return "", ""
    user_info = message_info.get("user_info")
    if not isinstance(user_info, Mapping):
        return "", ""
    nickname = str(user_info.get("user_nickname") or "").strip()
    cardname = str(user_info.get("user_cardname") or "").strip()
    return nickname, cardname
