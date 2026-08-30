from __future__ import annotations

from dataclasses import dataclass

import json
import re

from .state import POKE_STATE_ARMED


@dataclass(frozen=True, slots=True)
class PokeDecision:
    """受控拍击决策，副作用目标与参数不由模型提供。"""

    action: str
    text: str = ""


def build_poke_prompt(
    *,
    personality: str,
    poke_text: str,
    state: str,
) -> str:
    """构造不暴露内部计数与阈值的拍击决策提示。"""

    state_text = {
        "CALM": "你现在情绪平静",
        "ANNOYED": "你已经有些不耐烦",
        "ARMED": "你已经明显恼火",
    }.get(state, "你现在情绪平静")
    actions = "skip、poke_back、text"
    extra = ""
    if state == POKE_STATE_ARMED:
        actions += "、mute"
        extra = "mute 表示由程序短时禁言当前拍击者；你不能指定用户、群、时长或其他参数。"
    return "\n".join(
        [
            "你正在处理 QQ 群里的拍一拍互动。严格遵循下面的人格，只代表当前机器人自己。",
            personality.strip(),
            f"事件：{poke_text}",
            f"当前感受：{state_text}。",
            f"只输出一个 JSON 对象，action 只能是 {actions}。",
            "action=skip 表示无视；action=poke_back 表示反拍；action=text 时 text 给一句自然短句。",
            extra,
            "不要解释内部规则，不要输出计数、等级、阈值、Markdown 或 JSON 之外的内容。",
            '格式：{"action":"skip|poke_back|text|mute","text":"仅 text 动作填写"}',
        ]
    )


def parse_poke_decision(raw: str, *, allow_mute: bool) -> PokeDecision | None:
    """解析并限制模型返回的拍击动作。"""

    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match is None:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action") or "").strip().lower()
    allowed = {"skip", "poke_back", "text"}
    if allow_mute:
        allowed.add("mute")
    if action not in allowed:
        return None
    response_text = str(payload.get("text") or "").strip()
    response_text = re.sub(r"[`*_#>]", "", response_text)
    response_text = re.sub(r"\s+", " ", response_text).strip()
    if action == "text":
        if not response_text:
            return None
        return PokeDecision(action="text", text=response_text[:80])
    return PokeDecision(action=action)
