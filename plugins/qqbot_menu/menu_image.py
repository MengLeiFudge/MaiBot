from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import textwrap
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont


class MenuFeature(Protocol):
    name: str
    aliases: tuple[str, ...]
    status: str
    lines: tuple[str, ...]


CANVAS_WIDTH = 1120
BACKGROUND = (244, 247, 251)
INK = (30, 38, 52)
MUTED = (92, 102, 119)
PANEL = (255, 255, 255)
BORDER = (214, 221, 232)
ACCENT = (59, 130, 246)
ANGEL = (42, 157, 143)
DEMON = (132, 80, 195)
WARNING = (180, 83, 9)


def render_overview_menu_image(
    *,
    features: tuple[MenuFeature, ...],
    feature_mode: str,
    output_dir: Path,
) -> Path:
    payload = {
        "kind": "overview",
        "feature_mode": feature_mode,
        "features": [_feature_payload(feature) for feature in features],
        "version": 3,
    }
    image_path = _cached_path(output_dir, payload)
    if image_path.is_file():
        return image_path

    fonts = _load_fonts()
    card_width = 508
    card_gap = 24
    x_left = 40
    x_right = x_left + card_width + card_gap
    y = 40
    card_heights = [_overview_card_height(feature, fonts) for feature in features]
    rows = []
    for index in range(0, len(features), 2):
        left_height = card_heights[index]
        right_height = card_heights[index + 1] if index + 1 < len(features) else 0
        rows.append(max(left_height, right_height))
    canvas_height = 265 + sum(rows) + max(0, len(rows) - 1) * card_gap + 46
    image = Image.new("RGB", (CANVAS_WIDTH, canvas_height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    _draw_header(draw, fonts, "棉花糖统一指令菜单", "固定指令只由一个棉花糖执行；闲聊允许两个棉花糖一起回应。")
    y = 176
    _draw_mode_strip(draw, fonts, feature_mode, y)
    y += 66

    for row_index, row_height in enumerate(rows):
        left_index = row_index * 2
        _draw_overview_card(draw, fonts, x_left, y, card_width, row_height, features[left_index])
        right_index = left_index + 1
        if right_index < len(features):
            _draw_overview_card(draw, fonts, x_right, y, card_width, row_height, features[right_index])
        y += row_height + card_gap

    _draw_footer(draw, fonts, canvas_height - 32)
    _save(image, image_path)
    return image_path


def render_feature_menu_image(
    *,
    feature: MenuFeature,
    feature_mode: str,
    output_dir: Path,
) -> Path:
    payload = {
        "kind": "feature",
        "feature_mode": feature_mode,
        "feature": _feature_payload(feature),
        "version": 3,
    }
    image_path = _cached_path(output_dir, payload)
    if image_path.is_file():
        return image_path

    fonts = _load_fonts()
    lines = feature.lines or ("这个模块暂无单独功能菜单。",)
    wrapped_lines = []
    for line in lines:
        wrapped_lines.extend(_wrap_text(line, 34))

    detail_height = 42 + len(wrapped_lines) * 42
    canvas_height = 310 + detail_height
    image = Image.new("RGB", (CANVAS_WIDTH, canvas_height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    _draw_header(draw, fonts, f"{feature.name} 功能菜单", "模块详情只展示固定指令和限制；执行时仍按双 bot 唯一触发规则处理。")
    y = 176
    _draw_mode_strip(draw, fonts, feature_mode, y)
    y += 84

    _rounded_rect(draw, (40, y, CANVAS_WIDTH - 40, y + detail_height), radius=18, fill=PANEL, outline=BORDER)
    draw.text((74, y + 28), f"状态：{feature.status}", font=fonts.body_bold, fill=_status_color(feature.status))
    line_y = y + 78
    for line in wrapped_lines:
        draw.text((82, line_y), f"- {line}", font=fonts.body, fill=INK)
        line_y += 42
    y += detail_height + 22

    _draw_footer(draw, fonts, canvas_height - 32)
    _save(image, image_path)
    return image_path


def _feature_payload(feature: MenuFeature) -> dict[str, object]:
    if hasattr(feature, "__dataclass_fields__"):
        return asdict(feature)  # type: ignore[arg-type]
    return {
        "name": feature.name,
        "aliases": feature.aliases,
        "status": feature.status,
        "lines": feature.lines,
    }


def _cached_path(output_dir: Path, payload: dict[str, object]) -> Path:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"menu-{payload['kind']}-{digest}.png"


class _Fonts:
    def __init__(self, font_path: str | None) -> None:
        self.title = _font(font_path, 46)
        self.subtitle = _font(font_path, 24)
        self.body = _font(font_path, 25)
        self.body_bold = _font(font_path, 27)
        self.small = _font(font_path, 21)
        self.badge = _font(font_path, 19)


def _load_fonts() -> _Fonts:
    return _Fonts(_find_font_path())


def _find_font_path() -> str | None:
    candidates = (
        os.environ.get("QQBOT_MENU_FONT", ""),
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/mnt/c/Windows/Fonts/msyh.ttc",
        "/mnt/c/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    )
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


def _font(font_path: str | None, size: int) -> ImageFont.ImageFont:
    if font_path:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _draw_header(draw: ImageDraw.ImageDraw, fonts: _Fonts, title: str, subtitle: str) -> None:
    draw.text((40, 38), title, font=fonts.title, fill=INK)
    draw.text((42, 100), subtitle, font=fonts.subtitle, fill=MUTED)
    _badge(draw, fonts, (884, 48), "MaiBot 原生", ANGEL)
    _badge(draw, fonts, (884, 92), "唯一执行", DEMON)


def _draw_mode_strip(draw: ImageDraw.ImageDraw, fonts: _Fonts, feature_mode: str, y: int) -> None:
    text = feature_mode
    _rounded_rect(draw, (40, y, CANVAS_WIDTH - 40, y + 48), radius=16, fill=(233, 241, 255), outline=(192, 213, 255))
    draw.text((66, y + 10), text, font=fonts.small, fill=(37, 75, 135))
    draw.text((748, y + 10), "固定指令：单 bot 执行", font=fonts.small, fill=WARNING)


def _overview_card_height(feature: MenuFeature, fonts: _Fonts) -> int:
    del fonts
    summary = _feature_summary(feature)
    return 154 + max(0, len(_wrap_text(summary, 18)) - 1) * 30


def _draw_overview_card(
    draw: ImageDraw.ImageDraw,
    fonts: _Fonts,
    x: int,
    y: int,
    width: int,
    height: int,
    feature: MenuFeature,
) -> None:
    _rounded_rect(draw, (x, y, x + width, y + height), radius=18, fill=PANEL, outline=BORDER)
    draw.text((x + 24, y + 20), feature.name, font=fonts.body_bold, fill=INK)
    _status_badge(draw, fonts, (x + width - 138, y + 22), feature.status)

    summary_y = y + 66
    for line in _wrap_text(_feature_summary(feature), 18)[:3]:
        draw.text((x + 24, summary_y), line, font=fonts.small, fill=MUTED)
        summary_y += 30

    draw.text((x + 24, y + height - 34), f"菜单{feature.name}", font=fonts.badge, fill=(73, 87, 107))


def _feature_summary(feature: MenuFeature) -> str:
    if feature.lines:
        return feature.lines[0]
    return "发送 菜单模块名 查看详情。"


def _status_color(status: str) -> tuple[int, int, int]:
    if "部分" in status or "基础" in status:
        return WARNING
    if "原生" in status:
        return DEMON
    return ANGEL


def _status_badge(draw: ImageDraw.ImageDraw, fonts: _Fonts, pos: tuple[int, int], status: str) -> None:
    x, y = pos
    color = _status_color(status)
    _rounded_rect(draw, (x, y, x + 112, y + 30), radius=12, fill=(250, 252, 255), outline=color)
    draw.text((x + 12, y + 4), status[:5], font=fonts.badge, fill=color)


def _badge(draw: ImageDraw.ImageDraw, fonts: _Fonts, pos: tuple[int, int], text: str, color: tuple[int, int, int]) -> None:
    x, y = pos
    _rounded_rect(draw, (x, y, x + 190, y + 34), radius=14, fill=(255, 255, 255), outline=color)
    draw.text((x + 14, y + 5), text, font=fonts.badge, fill=color)


def _draw_footer(draw: ImageDraw.ImageDraw, fonts: _Fonts, y: int) -> None:
    draw.text((40, y), "发送 菜单模块名 查看详情，例如 菜单棉花糖互动 / 菜单Arcaea / 菜单JM漫画。", font=fonts.badge, fill=MUTED)


def _wrap_text(text: str, width: int) -> list[str]:
    wrapped: list[str] = []
    for raw_line in str(text or "").splitlines() or [""]:
        line = raw_line.strip()
        if not line:
            continue
        wrapped.extend(textwrap.wrap(line, width=width, break_long_words=True, replace_whitespace=False))
    return wrapped or [""]


def _rounded_rect(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    *,
    radius: int,
    fill: tuple[int, int, int],
    outline: tuple[int, int, int],
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=2)


def _save(image: Image.Image, path: Path) -> None:
    tmp_path = path.with_suffix(".tmp.png")
    image.save(tmp_path, "PNG", optimize=True)
    tmp_path.replace(path)
