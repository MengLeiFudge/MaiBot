from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


WELCOME_EXPRESSIONS = (
    "群地位-1",
    "群地位--",
    "群地位=群地位-1",
    "群地位+=-1",
    "群地位-=1",
    "群地位=群地位+(-1)",
    "群地位=群地位+i^2",
    "群地位+=i^2",
    "群地位-=-i^2",
    "群地位(t+1)=群地位(t)-1",
    "群地位=群地位+e^(i*pi)",
    "群地位+=e^(i*pi)",
    "群地位-=(-e^(i*pi))",
    "群地位=群地位+cos(pi)",
    "群地位+=cos(pi)",
)


@dataclass(frozen=True, slots=True)
class OneBotRequest:
    request_type: str
    sub_type: str
    self_id: str
    user_id: str
    group_id: str
    flag: str


@dataclass(frozen=True, slots=True)
class GroupIncreaseNotice:
    self_id: str
    user_id: str
    group_id: str
    sub_type: str


def parse_onebot_request(message: object) -> OneBotRequest | None:
    additional = _additional_config(message)
    payload = additional.get("napcat_request_payload")
    if not isinstance(payload, Mapping):
        return None
    request_type = str(
        additional.get("napcat_request_type") or payload.get("request_type") or ""
    ).strip()
    if not request_type:
        return None
    return OneBotRequest(
        request_type=request_type,
        sub_type=str(
            additional.get("napcat_request_sub_type") or payload.get("sub_type") or ""
        ).strip(),
        self_id=str(additional.get("self_id") or payload.get("self_id") or "").strip(),
        user_id=str(payload.get("user_id") or "").strip(),
        group_id=str(payload.get("group_id") or "").strip(),
        flag=str(payload.get("flag") or "").strip(),
    )


def parse_group_increase_notice(message: object) -> GroupIncreaseNotice | None:
    additional = _additional_config(message)
    payload = additional.get("napcat_notice_payload")
    if not isinstance(payload, Mapping):
        return None
    notice_type = str(
        additional.get("napcat_notice_type") or payload.get("notice_type") or ""
    ).strip()
    if notice_type != "group_increase":
        return None
    self_id = str(additional.get("self_id") or payload.get("self_id") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    group_id = str(payload.get("group_id") or "").strip()
    if not self_id or not user_id or not group_id:
        return None
    return GroupIncreaseNotice(
        self_id=self_id,
        user_id=user_id,
        group_id=group_id,
        sub_type=str(payload.get("sub_type") or "").strip(),
    )


def format_self_join_notice(bot_name: str, group_name: str, group_id: str) -> str:
    normalized_bot = bot_name.strip() or "机器人"
    normalized_group = group_name.strip() or "未知群聊"
    normalized_group_id = group_id.strip() or "未知群号"
    return f"{normalized_bot}已经加入群聊{normalized_group}（{normalized_group_id}）了！"


def format_member_welcome(template: str, expression: str) -> str:
    normalized_template = template if template.strip() else " 欢迎大佬，{expression}"
    normalized_expression = expression.strip() or WELCOME_EXPRESSIONS[0]
    try:
        return normalized_template.format(expression=normalized_expression)
    except (IndexError, KeyError, ValueError):
        return f" 欢迎大佬，{normalized_expression}"


def _additional_config(message: object) -> Mapping[str, object]:
    if not isinstance(message, Mapping) or not bool(message.get("is_notify")):
        return {}
    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        return {}
    additional = message_info.get("additional_config")
    return additional if isinstance(additional, Mapping) else {}
