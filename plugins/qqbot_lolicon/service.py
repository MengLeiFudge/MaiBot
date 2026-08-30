from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import json
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOLICON_API_URL = "https://api.lolicon.app/setu/v2"
LOLICON_USER_AGENT = "qqbot-lolicon/1.0"
LOLICON_MAX_NUM = 20


class LoliconMode(IntEnum):
    NON_R18 = 0
    R18 = 1
    MIXED = 2


@dataclass(frozen=True, slots=True)
class LoliconCommand:
    mode: LoliconMode
    num: int
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LoliconImageItem:
    title: str
    pid: int
    page: int
    author: str
    uid: int
    url: str
    r18: bool
    width: int
    height: int
    tags: tuple[str, ...]
    ext: str
    ai_type: int
    upload_date: int


def parse_lolicon_command(text: str) -> LoliconCommand | None:
    normalized = text.strip()
    if normalized.startswith("来点"):
        normalized = normalized[2:].strip()
    if len(normalized) < 2:
        return None

    prefix = normalized[:2]
    modes = {
        "美图": LoliconMode.NON_R18,
        "色图": LoliconMode.R18,
        "涩图": LoliconMode.R18,
        "蛇图": LoliconMode.R18,
        "混合": LoliconMode.MIXED,
    }
    mode = modes.get(prefix)
    if mode is None:
        return None

    payload = normalized[2:].strip()
    if not payload:
        return LoliconCommand(mode=mode, num=1, tags=())
    if payload.isdigit():
        return LoliconCommand(mode=mode, num=int(payload), tags=())

    parts = payload.split()
    num = 5
    if parts[-1].isdigit():
        num = int(parts.pop())
    return LoliconCommand(mode=mode, num=num, tags=tuple(parts))


def build_lolicon_api_url(
    command: LoliconCommand,
    *,
    endpoint: str = LOLICON_API_URL,
) -> str:
    query: dict[str, object] = {
        "r18": int(command.mode),
        "num": min(max(command.num, 1), LOLICON_MAX_NUM),
        "size": "original",
    }
    if command.tags:
        query["tag"] = list(command.tags)
    return f"{endpoint}?{urlencode(query, doseq=True)}"


def parse_lolicon_response(payload: object) -> tuple[LoliconImageItem, ...]:
    if not isinstance(payload, dict) or payload.get("error"):
        return ()
    raw_items = payload.get("data")
    if not isinstance(raw_items, list):
        return ()

    items: list[LoliconImageItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        urls = raw.get("urls")
        original_url = str(urls.get("original") or "").strip() if isinstance(urls, dict) else ""
        if not original_url:
            continue
        try:
            item = LoliconImageItem(
                title=str(raw.get("title") or ""),
                pid=int(raw.get("pid") or 0),
                page=int(raw.get("p") or 0),
                author=str(raw.get("author") or ""),
                uid=int(raw.get("uid") or 0),
                url=original_url,
                r18=bool(raw.get("r18", False)),
                width=int(raw.get("width") or 0),
                height=int(raw.get("height") or 0),
                tags=tuple(str(tag) for tag in raw.get("tags", ()) if str(tag).strip()),
                ext=str(raw.get("ext") or Path(original_url).suffix.lstrip(".") or "jpg"),
                ai_type=int(raw.get("aiType") or 0),
                upload_date=int(raw.get("uploadDate") or 0),
            )
        except (TypeError, ValueError):
            continue
        items.append(item)
    return tuple(items)


class LoliconClient:
    def __init__(
        self,
        *,
        endpoint: str = LOLICON_API_URL,
        timeout_seconds: float = 20.0,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        self.endpoint = endpoint.rstrip("?")
        self.timeout_seconds = timeout_seconds
        self._opener = opener

    def fetch(self, command: LoliconCommand) -> tuple[LoliconImageItem, ...]:
        request = Request(
            build_lolicon_api_url(command, endpoint=self.endpoint),
            headers={"User-Agent": LOLICON_USER_AGENT},
        )
        with self._opener(request, timeout=self.timeout_seconds) as response:  # type: ignore[attr-defined]
            payload = json.loads(response.read().decode("utf-8"))
        return parse_lolicon_response(payload)
