from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from urllib.request import Request, build_opener

from PIL import Image, ImageDraw, ImageFont


CODEXRADAR_EFFICIENCY_URL = "https://codexradar.com/data/intelligence-efficiency.json"
CANVAS_WIDTH = 1240
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_POINTS = 48
EFFORT_ORDER = ("ultra", "max", "xhigh", "high", "medium", "low")
FAMILY_ORDER = ("sol", "terra", "luna", "5.5", "deepseek")
FAMILY_COLORS = {
    "sol": (245, 190, 42),
    "terra": (75, 127, 245),
    "luna": (218, 225, 237),
    "5.5": (62, 210, 232),
    "deepseek": (132, 78, 245),
}


@dataclass(frozen=True)
class CodexRadarEfficiencyPoint:
    """One validated model and effort measurement published by CodexRadar."""

    model: str
    effort: str
    iq: float
    average_price_usd: float | None = None
    average_minutes: float | None = None
    runs_24h: int = 0


@dataclass(frozen=True)
class CodexRadarEfficiencySnapshot:
    """Validated public intelligence-efficiency data used by the image renderer."""

    source_updated_at: str = ""
    runs_24h_total: int = 0
    points: tuple[CodexRadarEfficiencyPoint, ...] = ()


def fetch_codexradar_efficiency(
    *,
    url: str = CODEXRADAR_EFFICIENCY_URL,
    timeout_seconds: float = 10.0,
) -> CodexRadarEfficiencySnapshot:
    """Fetch and validate the bounded public CodexRadar JSON document."""
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "QQBot-CodexRadar-Efficiency/1.0",
        },
    )
    opener = build_opener()
    with opener.open(request, timeout=max(1.0, float(timeout_seconds))) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_RESPONSE_BYTES:
            raise ValueError("CodexRadar response is too large")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("CodexRadar response is too large")
    payload = json.loads(body.decode("utf-8"))
    return parse_codexradar_efficiency(payload)


def parse_codexradar_efficiency(payload: object) -> CodexRadarEfficiencySnapshot:
    """Convert an untrusted JSON value into a bounded immutable snapshot."""
    if not isinstance(payload, dict):
        raise ValueError("CodexRadar response root must be an object")
    raw_points = payload.get("points")
    if not isinstance(raw_points, list):
        raise ValueError("CodexRadar response is missing points")
    points: list[CodexRadarEfficiencyPoint] = []
    for raw in raw_points[:MAX_POINTS]:
        point = _parse_point(raw)
        if point is not None:
            points.append(point)
    points.sort(key=_point_sort_key)
    return CodexRadarEfficiencySnapshot(
        source_updated_at=_bounded_text(payload.get("source_updated_at"), 64),
        runs_24h_total=_bounded_int(payload.get("runs_24h_total"), minimum=0, maximum=10_000_000),
        points=tuple(points),
    )


def render_codexradar_efficiency_image(
    *,
    snapshot: CodexRadarEfficiencySnapshot,
    output_dir: Path,
) -> Path:
    """Render a versioned fixed-width PNG and reuse identical cached output."""
    payload = {
        "kind": "codexradar-intelligence-efficiency",
        "version": 1,
        "source_updated_at": snapshot.source_updated_at,
        "runs_24h_total": snapshot.runs_24h_total,
        "points": [point.__dict__ for point in snapshot.points],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / f"codexradar-efficiency-{digest}.png"
    if image_path.is_file():
        return image_path

    fonts = _Fonts(_find_font_path())
    families = _group_points(snapshot.points)
    row_count = max(1, len(families))
    height = 150 + row_count * 142 + 30
    image = Image.new("RGB", (CANVAS_WIDTH, height), (13, 20, 32))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, CANVAS_WIDTH - 24, height - 24), radius=8, fill=(17, 24, 39), outline=(53, 70, 95), width=2)
    draw.text((48, 48), "智力效率", font=fonts.title, fill=(235, 241, 249))
    updated = _format_updated_at(snapshot.source_updated_at)
    draw.text((220, 58), f"{updated} 更新", font=fonts.meta, fill=(167, 178, 195))
    activity = f"近24小时 {snapshot.runs_24h_total} 次有效作答"
    _draw_right(draw, activity, CANVAS_WIDTH - 48, 58, fonts.meta, (147, 197, 253))

    if not families:
        draw.rounded_rectangle((48, 112, CANVAS_WIDTH - 48, height - 48), radius=8, fill=(24, 32, 51), outline=(53, 70, 95))
        draw.text((72, 145), "暂无可用智力效率数据", font=fonts.body, fill=(167, 178, 195))
    else:
        top = 112
        for family, points in families:
            _draw_family_row(draw, fonts, family, points, top)
            top += 142

    image.save(image_path, format="PNG", optimize=True)
    return image_path


def _parse_point(raw: object) -> CodexRadarEfficiencyPoint | None:
    if not isinstance(raw, dict):
        return None
    model = _bounded_text(raw.get("model"), 48).lower()
    effort = _bounded_text(raw.get("effort"), 16).lower()
    iq = _finite_float(raw.get("iq"))
    if not model or effort not in EFFORT_ORDER or iq is None or iq < 0 or iq > 1000:
        return None
    price = _finite_float(raw.get("average_price_usd"))
    minutes = _finite_float(raw.get("average_minutes"))
    if price is not None and (price < 0 or price > 1_000_000):
        price = None
    if minutes is not None and (minutes < 0 or minutes > 1_000_000):
        minutes = None
    return CodexRadarEfficiencyPoint(
        model=model,
        effort=effort,
        iq=iq,
        average_price_usd=price,
        average_minutes=minutes,
        runs_24h=_bounded_int(raw.get("runs_24h"), minimum=0, maximum=10_000_000),
    )


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return minimum
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return minimum
    return max(minimum, min(maximum, parsed))


def _finite_float(value: object) -> float | None:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _family_key(model: str) -> str:
    lowered = model.lower()
    if "deepseek" in lowered:
        return "deepseek"
    if "5.5" in lowered or "55" in lowered:
        return "5.5"
    for family in ("sol", "terra", "luna"):
        if family in lowered:
            return family
    return lowered[:20]


def _family_label(family: str, model: str) -> str:
    labels = {"sol": "Sol", "terra": "Terra", "luna": "Luna", "5.5": "5.5", "deepseek": "DeepSeek"}
    return labels.get(family, model[:18])


def _point_sort_key(point: CodexRadarEfficiencyPoint) -> tuple[int, str, int]:
    family = _family_key(point.model)
    family_index = FAMILY_ORDER.index(family) if family in FAMILY_ORDER else len(FAMILY_ORDER)
    return family_index, family, EFFORT_ORDER.index(point.effort)


def _group_points(points: tuple[CodexRadarEfficiencyPoint, ...]) -> list[tuple[str, tuple[CodexRadarEfficiencyPoint, ...]]]:
    grouped: dict[str, list[CodexRadarEfficiencyPoint]] = {}
    for point in points:
        grouped.setdefault(_family_key(point.model), []).append(point)
    ordered = sorted(grouped, key=lambda key: (FAMILY_ORDER.index(key) if key in FAMILY_ORDER else len(FAMILY_ORDER), key))
    return [(key, tuple(sorted(grouped[key], key=lambda point: EFFORT_ORDER.index(point.effort)))) for key in ordered]


def _draw_family_row(draw: ImageDraw.ImageDraw, fonts: "_Fonts", family: str, points: tuple[CodexRadarEfficiencyPoint, ...], top: int) -> None:
    card_gap = 10
    left = 48
    right = CANVAS_WIDTH - 48
    card_width = (right - left - card_gap * 5) // 6
    color = FAMILY_COLORS.get(family, (74, 222, 128))
    by_effort = {point.effort: point for point in points}
    for column, effort in enumerate(EFFORT_ORDER):
        point = by_effort.get(effort)
        if point is None:
            continue
        x = left + column * (card_width + card_gap)
        draw.rounded_rectangle((x, top, x + card_width, top + 122), radius=8, fill=(26, 34, 53), outline=color, width=1)
        label = f"{_family_label(family, point.model)} {point.effort}"
        draw.text((x + 10, top + 10), _ellipsize(draw, fonts.card, label, card_width - 20), font=fonts.card, fill=(220, 228, 240))
        draw.text((x + 10, top + 42), f"{point.iq:.1f}", font=fonts.score, fill=color)
        price = "--" if point.average_price_usd is None else f"${point.average_price_usd:.2f}"
        minutes = "--" if point.average_minutes is None else f"{point.average_minutes:.0f}分钟"
        _draw_right(draw, price, x + card_width - 10, top + 47, fonts.card, color)
        _draw_right(draw, minutes, x + card_width - 10, top + 88, fonts.card, color)
        if point.runs_24h:
            draw.text((x + 10, top + 94), str(point.runs_24h), font=fonts.tiny, fill=(135, 149, 170))


def _draw_right(draw: ImageDraw.ImageDraw, text: str, right: int, y: int, font: ImageFont.FreeTypeFont, fill: tuple[int, int, int]) -> None:
    draw.text((right - draw.textlength(text, font=font), y), text, font=font, fill=fill)


def _ellipsize(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, text: str, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    candidate = text
    while candidate and draw.textlength(candidate + "...", font=font) > width:
        candidate = candidate[:-1]
    return candidate + "..."


def _format_updated_at(value: str) -> str:
    if "T" in value:
        date, time_part = value.split("T", 1)
        return f"{date[5:]} {time_part[:5]}"
    return value[:16] or "时间未知"


def _find_font_path() -> str:
    candidates = (
        "C:/Windows/Fonts/msyh.ttc",
        "/mnt/c/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("No usable font found")


class _Fonts:
    """Font set shared by the compact report layout."""

    def __init__(self, path: str) -> None:
        self.title = ImageFont.truetype(path, 34)
        self.body = ImageFont.truetype(path, 24)
        self.meta = ImageFont.truetype(path, 20)
        self.card = ImageFont.truetype(path, 18)
        self.score = ImageFont.truetype(path, 40)
        self.tiny = ImageFont.truetype(path, 14)
