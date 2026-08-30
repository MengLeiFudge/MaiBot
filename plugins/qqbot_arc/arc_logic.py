from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import json
import random
import re
import sqlite3

from PIL import Image, ImageDraw, ImageFont

from .storage import ArcSessionStore


DIFFICULTY_LABELS = {0: "PST", 1: "PRS", 2: "FTR", 3: "BYD", 4: "ETR"}
EVENT_BLOCK_RE = re.compile(
    r"(?:\}\})?\{\{#ifeq:\{\{\{1\|(?P<title>[^}]+)\}\}\}\|(?P=title)\|(?P<body>.*?)(?=(?:\}\}\{\{#ifeq:)|(?:\n\[\[Category:)|\Z)",
    re.S,
)
PLAYABLE_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})")
LIST_ITEM_RE = re.compile(r"<li>(.*?)</li>", re.S)
REWARD_LINE_RE = re.compile(r"^\|\s*[^|]*\|\s*[^|]*\|\s*(.+)$")
WIKI_LINK_RE = re.compile(r"\[\[(?:[^|\]]+\|)?([^\]]+)\]\]")
TAG_RE = re.compile(r"<[^>]+>")
RESOURCE_RE = re.compile(
    r"(?i)^(?P<count>\d+)\s+(?P<name>Fragments?|Ether Drop|Memory Archive Ticket|World Extend Ticket|Core.*)$"
)


@dataclass(frozen=True, slots=True)
class ArcMessage:
    text: str
    image_path: Path | None = None


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    song_id: str
    title: str
    aliases: tuple[str, ...]
    song: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ArcChart:
    song_id: str
    title: str
    artist: str
    bpm: str
    pack: str
    version: str
    difficulty: str
    constant: float
    chart_designer: str
    jacket_designer: str
    jacket_path: Path | None


class ArcCatalog:
    """Read-only access to a configured Arcaea asset tree."""

    def __init__(
        self,
        assets_root: Path,
        aliases_path: Path | None = None,
        cache_database_path: Path | None = None,
    ) -> None:
        self.assets_root = Path(assets_root)
        self.chart_root = self.assets_root / "官谱"
        self.aliases_path = Path(aliases_path) if aliases_path else None
        self.cache_database_path = Path(cache_database_path) if cache_database_path else None

    def entries(self) -> list[CatalogEntry]:
        aliases = self._static_aliases()
        result: list[CatalogEntry] = []
        for song in self._song_payload().get("songs", []):
            if song.get("deleted"):
                continue
            song_id = str(song.get("id") or "").strip()
            localized = song.get("title_localized")
            localized = localized if isinstance(localized, dict) else {}
            title = str(
                localized.get("en")
                or localized.get("zh-Hans")
                or localized.get("zh-Hant")
                or localized.get("ja")
                or song_id
            ).strip()
            if not song_id or not title:
                continue
            names = [str(value).strip() for value in localized.values() if str(value).strip()]
            names.extend(aliases.get(song_id, []))
            words = re.findall(r"[A-Za-z0-9]+", title)
            if len(words) > 1:
                names.append("".join(word[0] for word in words).lower())
            deduplicated = tuple(dict.fromkeys([title, *names]))
            result.append(CatalogEntry(song_id, title, deduplicated, song))
        return result

    def charts(self) -> list[ArcChart]:
        charts: list[ArcChart] = []
        cached_constants = self._cached_constants()
        for entry in self.entries():
            song = entry.song
            for difficulty in song.get("difficulties", []):
                rating_class = int(difficulty.get("ratingClass", -1))
                rating = int(difficulty.get("rating", 0))
                if rating_class not in DIFFICULTY_LABELS or rating <= 0:
                    continue
                constant = cached_constants.get(entry.song_id, {}).get(
                    str(rating_class),
                    float(rating) + (0.7 if difficulty.get("ratingPlus") else 0.0),
                )
                charts.append(
                    ArcChart(
                        song_id=entry.song_id,
                        title=entry.title,
                        artist=str(difficulty.get("artist") or song.get("artist") or "未知"),
                        bpm=str(difficulty.get("bpm") or song.get("bpm") or "未知"),
                        pack=str(song.get("set") or ""),
                        version=str(difficulty.get("version") or song.get("version") or ""),
                        difficulty=DIFFICULTY_LABELS[rating_class],
                        constant=constant,
                        chart_designer=str(difficulty.get("chartDesigner") or "未知"),
                        jacket_designer=str(difficulty.get("jacketDesigner") or ""),
                        jacket_path=self.jacket_path(entry.song_id, rating_class),
                    )
                )
        return charts

    def recommend(self, ptt: float, picker: Callable[[list[ArcChart]], ArcChart] | None = None) -> ArcChart:
        charts = self.charts()
        low, high = max(1.0, round(ptt - 2.0, 1)), max(1.0, round(ptt - 0.5, 1))
        candidates = [chart for chart in charts if low <= chart.constant <= high]
        if not candidates:
            candidates = sorted(charts, key=lambda item: abs(item.constant - max(1.0, ptt - 0.7)))[:10]
        if not candidates:
            raise ValueError("当前本地曲库中没有可推荐的谱面。")
        return (picker or random.choice)(candidates)

    def jacket_path(self, song_id: str, rating_class: int | None = None) -> Path | None:
        names: list[str] = []
        if rating_class is not None:
            key = DIFFICULTY_LABELS[rating_class].lower()
            names.extend((f"1080_{key}.jpg", f"{key}.jpg"))
        names.extend(("1080_base.jpg", "base.jpg"))
        for directory in (self.chart_root / song_id, self.chart_root / f"dl_{song_id}"):
            for name in names:
                candidate = directory / name
                if candidate.is_file():
                    return candidate
        return None

    def recommendation_text(self, ptt: float, chart: ArcChart) -> str:
        lines = [
            f"PTT {ptt:.1f} 推荐：",
            f"{chart.title} [{chart.difficulty}]",
            f"曲师：{chart.artist}",
            f"定数：{chart.constant:.1f}",
            f"BPM：{chart.bpm}",
            f"谱师：{chart.chart_designer}",
        ]
        if chart.jacket_designer:
            lines.append(f"曲绘：{chart.jacket_designer}")
        if chart.pack:
            lines.append(f"曲包：{chart.pack}")
        if chart.version:
            lines.append(f"版本：{chart.version}")
        if chart.jacket_path is None:
            lines.append("曲绘文件：当前本地资源缺失")
        return "\n".join(lines)

    def _song_payload(self) -> dict[str, Any]:
        for path in (self.chart_root / "songlist", self.chart_root / "songlist.json"):
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"未找到 Arcaea songlist：{self.chart_root}")

    def _static_aliases(self) -> dict[str, list[str]]:
        result = self._cached_aliases()
        if self.aliases_path is None or not self.aliases_path.is_file():
            return result
        payload = json.loads(self.aliases_path.read_text(encoding="utf-8"))
        songs = payload.get("songs", payload)
        if not isinstance(songs, dict):
            return result
        for song_id, value in songs.items():
            raw = value.get("aliases", []) if isinstance(value, dict) else value
            if isinstance(raw, list):
                existing = result.setdefault(str(song_id), [])
                existing.extend(str(alias).strip() for alias in raw if str(alias).strip() and str(alias).strip() not in existing)
        return result

    def _cached_aliases(self) -> dict[str, list[str]]:
        rows = self._cache_rows("SELECT song_id, aliases FROM arc_alias_cache")
        result: dict[str, list[str]] = {}
        for song_id, raw in rows:
            aliases = json.loads(raw)
            if isinstance(aliases, list):
                result[str(song_id)] = [str(alias) for alias in aliases if str(alias)]
        return result

    def _cached_constants(self) -> dict[str, dict[str, float]]:
        rows = self._cache_rows("SELECT song_id, constants FROM arc_constant_cache")
        result: dict[str, dict[str, float]] = {}
        for song_id, raw in rows:
            constants = json.loads(raw)
            if isinstance(constants, dict):
                result[str(song_id)] = {str(key): float(value) for key, value in constants.items()}
        return result

    def _cache_rows(self, query: str) -> list[tuple[str, str]]:
        if self.cache_database_path is None or not self.cache_database_path.is_file():
            return []
        try:
            with sqlite3.connect(self.cache_database_path, timeout=10) as connection:
                return [(str(row[0]), str(row[1])) for row in connection.execute(query).fetchall()]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise


class ArcGuessGame:
    def __init__(
        self,
        catalog: ArcCatalog,
        store: ArcSessionStore,
        output_root: Path,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.output_root = Path(output_root)
        self.timeout_seconds = timeout_seconds

    def start_letters(self, group_id: str, count: int, *, now: float | None = None, picker=None) -> ArcMessage:
        entries = self.catalog.entries()
        if not entries:
            return ArcMessage("当前本地曲库为空，暂时不能开始 Arc 猜歌。")
        size = min(max(1, count), len(entries))
        chooser = picker or (lambda items, amount: random.sample(items, amount))

        def factory() -> dict[str, Any]:
            selected = chooser(entries, size)
            return {
                "mode": "letters",
                "opened_letters": [],
                "questions": [
                    {
                        "index": index,
                        "song_id": item.song_id,
                        "title": item.title,
                        "aliases": list(item.aliases),
                        "solved": False,
                        "solved_by": "",
                    }
                    for index, item in enumerate(selected, 1)
                ],
            }

        created, previous, session = self.store.start(
            group_id,
            factory,
            now=now,
            timeout_seconds=self.timeout_seconds,
        )
        if not created:
            return ArcMessage("当前已经有一局 Arc 猜歌了，请先发送 jx。")
        prefix = "已开始 Arc 猜歌："
        if previous is not None:
            prefix = "上一局 Arc 猜歌已经超时。\n\n" + prefix
        return ArcMessage(f"{prefix}\n{self.render_letters(session)}")

    def start_or_open_art(
        self,
        group_id: str,
        grid_size: int | str | None = None,
        *,
        now: float | None = None,
        picker=None,
        tile_picker=None,
    ) -> ArcMessage:
        entries = [entry for entry in self.catalog.entries() if self.catalog.jacket_path(entry.song_id)]
        if not entries:
            return ArcMessage("当前本地曲库没有可用曲绘，暂时不能开始 Arc 曲绘猜歌。")
        selected = (picker or random.choice)(entries)
        jacket = self.catalog.jacket_path(selected.song_id)
        assert jacket is not None
        with Image.open(jacket) as image:
            maximum = max(1, min(image.size))
        size = maximum if grid_size == "max" else max(1, min(int(grid_size or 5), maximum))

        def factory() -> dict[str, Any]:
            total = size * size
            tile = (tile_picker or random.choice)(list(range(1, total + 1)))
            return {
                "mode": "art",
                "song_id": selected.song_id,
                "title": selected.title,
                "aliases": list(selected.aliases),
                "jacket_path": str(jacket),
                "grid_size": size,
                "opened_tiles": [tile],
            }

        created, previous, session = self.store.start(
            group_id,
            factory,
            now=now,
            timeout_seconds=self.timeout_seconds,
        )
        if not created:
            if session.get("mode") == "art":
                return self.open_art(group_id, now=now, tile_picker=tile_picker, tile_count=1)
            return ArcMessage("当前进行的是字母猜歌，不能补图。")
        prefix = "已开始 Arc 曲绘猜歌。"
        if previous is not None:
            prefix = "上一局 Arc 猜歌已经超时。\n\n" + prefix
        return ArcMessage(
            f"{prefix}\n已开放格子：1/{size * size}\n直接发送曲名作答，或发送 arcqh。",
            self.render_art(group_id, session),
        )

    def open_art(self, group_id: str, *, now: float | None = None, tile_picker=None, tile_count: int = 1) -> ArcMessage:
        def mutate(session: dict[str, Any]):
            if self._expired(session, now):
                return None, self._reveal(session, group_id, "这一局 Arc 猜歌已经超时")
            if session.get("mode") != "art":
                return session, ArcMessage("当前进行的是字母猜歌，不能补图。")
            total = int(session["grid_size"]) ** 2
            choices = [item for item in range(1, total + 1) if item not in session["opened_tiles"]]
            if not choices:
                return session, ArcMessage(f"{total} 个格子已经全部开完，可以继续猜或发送 jx。")
            for _ in range(min(max(1, tile_count), len(choices))):
                tile = (tile_picker or random.choice)(choices)
                session["opened_tiles"].append(tile)
                choices.remove(tile)
            image = self.render_art(group_id, session)
            count = len(session["opened_tiles"])
            return session, ArcMessage(f"补充了曲绘。\n已开放格子：{count}/{total}\n直接发送曲名作答。", image)

        result = self.store.mutate(group_id, mutate, now=now)
        return result or ArcMessage("当前没有进行中的 Arc 曲绘猜歌，发送 arcqh 开始一局。")

    def handle_answer(self, group_id: str, text: str, player_name: str, *, now: float | None = None) -> ArcMessage | None:
        def mutate(session: dict[str, Any]):
            if self._expired(session, now):
                return None, self._reveal(session, group_id, "这一局 Arc 猜歌已经超时")
            if session.get("mode") == "letters":
                letter = parse_open_letter(text)
                if letter is not None:
                    return self._open_letter(session, group_id, letter)
                submission = parse_letter_submission(text)
                if submission is None:
                    return session, None
                return self._guess_letter(session, group_id, submission[0], submission[1], player_name)
            answer = parse_art_submission(text)
            if answer is None:
                return session, None
            aliases = list(session.get("aliases", []))
            if not text.strip().startswith("猜") and not is_plausible_answer(answer, aliases):
                return session, None
            if is_plausible_answer(answer, aliases):
                message = ArcMessage(f"{player_name} 答对了：{session['title']}", Path(session["jacket_path"]))
                return None, message
            return session, ArcMessage("不对，这首曲绘还没猜出来。")

        return self.store.mutate(group_id, mutate, now=now)

    def recognizes_answer(self, session: dict[str, Any], text: str) -> bool:
        if session.get("mode") == "letters":
            return parse_open_letter(text) is not None or parse_letter_submission(text) is not None
        answer = parse_art_submission(text)
        return answer is not None and (
            text.strip().startswith("猜") or is_plausible_answer(answer, list(session.get("aliases", [])))
        )

    def expire_sessions(self, *, now: float | None = None) -> list[tuple[str, ArcMessage]]:
        current = datetime.now().timestamp() if now is None else float(now)
        expired = self.store.collect_expired(now=current, timeout_seconds=self.timeout_seconds)
        return [
            (group_id, self._reveal(session, group_id, "这一局 Arc 猜歌已经超时"))
            for group_id, session in expired
        ]

    def reveal(self, group_id: str) -> ArcMessage:
        def mutate(session: dict[str, Any]):
            return None, self._reveal(session, group_id, "游戏结束")

        return self.store.mutate(group_id, mutate) or ArcMessage("当前没有进行中的 Arc 猜歌。")

    def _open_letter(self, session: dict[str, Any], group_id: str, letter: str):
        if letter in session["opened_letters"]:
            return session, ArcMessage(f"字符 {letter} 已经开过了。\n{self.render_letters(session)}")
        session["opened_letters"].append(letter)
        for question in session["questions"]:
            if not question["solved"] and "*" not in mask_answer(question["title"], session["opened_letters"]):
                question["solved"], question["solved_by"] = True, "开字符"
        if all(question["solved"] for question in session["questions"]):
            return None, ArcMessage("游戏结束，答案如下：", self.render_answers(group_id, session))
        return session, ArcMessage(f"已开字符：{letter}\n{self.render_letters(session)}")

    def _guess_letter(self, session: dict[str, Any], group_id: str, index: int, answer: str, player: str):
        question = next((item for item in session["questions"] if item["index"] == index), None)
        if question is None:
            return session, ArcMessage(f"没有第 {index} 首题。")
        if question["solved"]:
            return session, ArcMessage(f"第 {index} 首已经被 {question['solved_by']} 猜出。")
        if not is_plausible_answer(answer, question["aliases"]):
            return session, ArcMessage(f"不对，第 {index} 首还没猜出来。\n{self.render_letters(session)}")
        question["solved"], question["solved_by"] = True, player
        if all(item["solved"] for item in session["questions"]):
            return None, ArcMessage("游戏结束，答案如下：", self.render_answers(group_id, session))
        return session, ArcMessage(
            f"答对了第 {index} 首：{question['title']}\n{self.render_letters(session)}",
            self.catalog.jacket_path(question["song_id"]),
        )

    def _reveal(self, session: dict[str, Any], group_id: str, prefix: str) -> ArcMessage:
        if session.get("mode") == "art":
            return ArcMessage(f"{prefix}，答案是：{session['title']}", Path(session["jacket_path"]))
        return ArcMessage(f"{prefix}，答案如下：", self.render_answers(group_id, session))

    def _expired(self, session: dict[str, Any], now: float | None) -> bool:
        current = datetime.now().timestamp() if now is None else float(now)
        return current - float(session.get("_updated_at", current)) > self.timeout_seconds

    def render_letters(self, session: dict[str, Any]) -> str:
        lines: list[str] = []
        for question in session["questions"]:
            if question["solved"]:
                lines.append(f"{question['index']}. {question['title']}（被 {question['solved_by']} 猜出）")
            else:
                lines.append(f"{question['index']}. {mask_answer(question['title'], session['opened_letters'])}")
        opened = "无" if not session["opened_letters"] else " ".join(session["opened_letters"])
        lines.append(f"已开字符：{opened}")
        return "\n".join(lines)

    def render_art(self, group_id: str, session: dict[str, Any]) -> Path:
        output = self.output_root / "guess_art_tiles" / str(group_id) / "panel.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        size = int(session["grid_size"])
        with Image.open(session["jacket_path"]) as source:
            image = source.convert("RGB")
            width, height = image.size
            panel = Image.new("RGB", image.size, (88, 88, 88))
            for tile in session["opened_tiles"]:
                row, column = divmod(int(tile) - 1, size)
                box = (
                    column * width // size,
                    row * height // size,
                    (column + 1) * width // size,
                    (row + 1) * height // size,
                )
                panel.paste(image.crop(box), box[:2])
            panel.save(output, "PNG")
        return output

    def render_answers(self, group_id: str, session: dict[str, Any]) -> Path:
        output = self.output_root / "guess_answer_panels" / str(group_id) / "answers.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        row_height, width = 130, 900
        canvas = Image.new("RGB", (width, max(row_height, row_height * len(session["questions"]))), "white")
        draw = ImageDraw.Draw(canvas)
        font = _font(24)
        for row, question in enumerate(session["questions"]):
            top = row * row_height
            jacket = self.catalog.jacket_path(question["song_id"])
            if jacket:
                with Image.open(jacket) as source:
                    canvas.paste(source.convert("RGB").resize((96, 96)), (18, top + 17))
            draw.text((135, top + 25), f"{question['index']}. {question['title']}", fill=(30, 30, 30), font=font)
            if question["solved_by"]:
                draw.text((135, top + 68), f"被 {question['solved_by']} 猜出", fill=(80, 80, 80), font=font)
        canvas.save(output, "PNG")
        return output


def parse_recommend_ptt(text: str) -> float | None:
    match = re.fullmatch(r"arctj\s*([0-9]+(?:\.[0-9]+)?)", text.strip(), re.I)
    return float(match.group(1)) if match else None


def parse_guess_count(text: str) -> int | None:
    match = re.fullmatch(r"(?:arczm|zm)\s*([1-9][0-9]*)?", text.strip(), re.I)
    return (int(match.group(1)) if match and match.group(1) else 10) if match else None


def parse_art_grid(text: str) -> int | str | None:
    match = re.fullmatch(r"(?:arcqh|qh)\s*([1-9][0-9]*|max)?", text.strip(), re.I)
    if not match:
        return None
    value = (match.group(1) or "5").lower()
    return value if value == "max" else int(value)


def parse_open_letter(text: str) -> str | None:
    match = re.fullmatch(r"开\s*(\S)", text.strip())
    return match.group(1).lower() if match else None


def parse_letter_submission(text: str) -> tuple[int, str] | None:
    match = re.fullmatch(r"(?:猜\s*)?([1-9][0-9]*)\s*(.+)", text.strip())
    return (int(match.group(1)), match.group(2).strip()) if match else None


def parse_art_submission(text: str) -> str | None:
    stripped = text.strip()
    if parse_letter_submission(stripped) is not None:
        return None
    match = re.fullmatch(r"猜\s*(.+)", stripped)
    if match:
        return match.group(1).strip()
    if stripped.startswith("猜") or is_control_command(stripped):
        return None
    return stripped or None


def is_control_command(text: str) -> bool:
    stripped = text.strip()
    return bool(
        parse_recommend_ptt(stripped) is not None
        or parse_guess_count(stripped) is not None
        or parse_art_grid(stripped) is not None
        or re.fullmatch(r"arc(?:hd|tz)", stripped, re.I)
        or re.fullmatch(r"(?:arcjx|jx)", stripped, re.I)
        or re.fullmatch(r"(?:xz|arcxz)", stripped, re.I)
    )


def mask_answer(answer: str, opened_letters: list[str]) -> str:
    opened = set(opened_letters)
    return "".join(char if char == " " or char.lower() in opened else "*" for char in answer)


def normalize_answer(text: str) -> str:
    return "".join(char.lower() for char in text if char.isalnum())


def is_plausible_answer(answer: str, aliases: list[str] | tuple[str, ...]) -> bool:
    normalized = normalize_answer(answer)
    if not normalized:
        return False
    for alias in aliases:
        candidate = normalize_answer(alias)
        if normalized == candidate:
            return True
        if normalized in candidate and len(normalized) / len(candidate) >= 0.5:
            return True
        if min(len(normalized), len(candidate)) >= 5 and SequenceMatcher(None, normalized, candidate).ratio() >= 0.9:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ArcWorldEvent:
    title: str
    ends_at: datetime
    rewards: tuple[str, ...]


class ArcEventService:
    def __init__(self, timezone_name: str = "Asia/Shanghai", fetcher=None) -> None:
        self.zone = _zone(timezone_name)
        self.fetcher = fetcher or self._fetch

    def messages(self, now: datetime | None = None) -> list[str]:
        current = now.astimezone(self.zone) if now and now.tzinfo else (now.replace(tzinfo=self.zone) if now else datetime.now(self.zone))
        events: list[ArcWorldEvent] = []
        for match in EVENT_BLOCK_RE.finditer(self.fetcher("World Mode Data Past Events")):
            active = self._active_range(match.group("body"), current)
            if active is None:
                continue
            rewards = self._rewards(match.group("body"))[:4]
            title = match.group("title").replace("0-LE ", "").replace("0-WE ", "").split(" / ", 1)[0]
            events.append(ArcWorldEvent(title.removesuffix(" Event").strip(), active, tuple(rewards)))
        if not events:
            return ["当前没有活动梯子。"]
        return [
            f"限时：{event.title}\n剩余时间：{_remaining(event.ends_at - current)}\n关键奖励：{'、'.join(event.rewards) or '暂无关键奖励信息'}"
            for event in sorted(events, key=lambda item: item.ends_at)
        ]

    def _active_range(self, body: str, now: datetime) -> datetime | None:
        for start, end in PLAYABLE_RANGE_RE.findall(body):
            started = datetime.fromisoformat(start).replace(tzinfo=timezone.utc).astimezone(self.zone)
            ended = (datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(hours=15)).astimezone(self.zone)
            if started <= now <= ended:
                return ended
        return None

    def _rewards(self, body: str) -> list[str]:
        listed = [_clean_markup(item) for item in LIST_ITEM_RE.findall(body)]
        if listed:
            return [item for item in listed if item]
        items: list[str] = []
        totals: dict[str, int] = {}
        for line in body.splitlines():
            match = REWARD_LINE_RE.match(line.strip())
            if not match:
                continue
            reward = _clean_markup(match.group(1))
            resource = RESOURCE_RE.match(reward)
            if resource:
                name = resource.group("name")
                totals[name] = totals.get(name, 0) + int(resource.group("count"))
            elif reward and reward != "-" and reward not in items:
                items.append(reward)
        items.extend(f"{name} x{count}" for name, count in totals.items())
        return items

    @staticmethod
    def _fetch(title: str) -> str:
        query = urlencode({"action": "parse", "page": title, "prop": "wikitext", "formatversion": "2", "format": "json"})
        request = Request(f"https://arcaea.fandom.com/api.php?{query}", headers={"User-Agent": "qqbot-arc/0.1"})
        with urlopen(request, timeout=20) as response:
            return str(json.loads(response.read().decode("utf-8"))["parse"]["wikitext"])


def _clean_markup(text: str) -> str:
    cleaned = WIKI_LINK_RE.sub(r"\1", text.replace("&times;", "x").replace("'''", ""))
    cleaned = TAG_RE.sub("", cleaned).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _remaining(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts = ([f"{days}天"] if days else []) + ([f"{hours}小时"] if hours or days else []) + [f"{minutes}分钟"]
    return " ".join(parts)


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8)) if name == "Asia/Shanghai" else timezone.utc


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.ImageFont:
    for path in ("C:/Windows/Fonts/msyh.ttc", "/mnt/c/Windows/Fonts/msyh.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()
