from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import textwrap

from PIL import Image, ImageDraw, ImageFont

from .sub2api_usage import Sub2APIAccountSevenDayRanking
from .sub2api_usage import Sub2APIAccountUsage
from .sub2api_usage import Sub2APIUsageSnapshot
from .sub2api_usage import Sub2APIUserUsage
from .sub2api_usage import format_datetime, format_time_text
from .sub2api_usage import format_sub2api_user_name
from .sub2api_usage import format_usage_window


CANVAS_WIDTH = 1240
MARGIN = 48
BACKGROUND = (242, 246, 251)
PANEL = (255, 255, 255)
INK = (27, 38, 54)
MUTED = (91, 105, 124)
BORDER = (210, 220, 232)
GOOD = (24, 132, 92)
WARNING = (186, 112, 18)
ERROR = (192, 63, 64)
ROW_HEIGHT = 50


def render_sub2api_usage_image(*, snapshot: Sub2APIUsageSnapshot, output_dir: Path) -> Path:
    """Render a versioned cached report from one immutable usage snapshot."""
    payload = {
        "kind": "sub2api-usage",
        "snapshot": asdict(snapshot),
        "version": 7,
    }
    image_path = _cached_path(output_dir, payload)
    if image_path.is_file():
        return image_path

    fonts = _Fonts(_find_font_path())
    rankings_by_account = {
        ranking.account_id: ranking
        for ranking in snapshot.account_seven_day_rankings
    }
    account_sections = [
        (
            account,
            _account_height(account),
            rankings_by_account.get(
                account.account_id,
                Sub2APIAccountSevenDayRanking(account_id=account.account_id),
            ),
        )
        for account in snapshot.accounts
    ]
    status_height = _status_height(snapshot)
    user_height = _user_table_height(snapshot.users)
    if account_sections:
        account_content_height = sum(
            account_height + _account_ranking_table_height(ranking) + 80
            for _, account_height, ranking in account_sections
        )
    else:
        account_content_height = 72
    canvas_height = (
        156
        + status_height
        + 34
        + 44
        + account_content_height
        + 18
        + 44
        + user_height
        + 48
    )
    image = Image.new("RGB", (CANVAS_WIDTH, canvas_height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    draw.text((MARGIN, 36), "Sub2API 用量报告", font=fonts.title, fill=INK)
    draw.text(
        (MARGIN + 2, 98),
        "各账号当前7d周期榜与额度来自后台缓存；底部消费按 Asia/Shanghai 08:00 业务日边界统计。",
        font=fonts.subtitle,
        fill=MUTED,
    )

    y = 156
    y = _draw_status(draw, fonts, snapshot, y)
    y += 34
    draw.text((MARGIN, y), f"账号用量  共 {len(snapshot.accounts)} 个", font=fonts.section, fill=INK)
    y += 44
    if not account_sections:
        y = _draw_empty_panel(draw, fonts, y, "暂无账号缓存。")
    else:
        for account, account_height, ranking in account_sections:
            _draw_account_panel(draw, fonts, account, y, account_height)
            y += account_height + 14
            draw.text(
                (MARGIN, y),
                f"当前账号7d周期消费榜  共 {len(ranking.users)} 人",
                font=fonts.body_bold,
                fill=INK,
            )
            y += 40
            ranking_height = _account_ranking_table_height(ranking)
            _draw_account_ranking_table(draw, fonts, ranking, y, ranking_height)
            y += ranking_height + 26

    y += 18
    draw.text((MARGIN, y), f"全账号消费榜  共 {len(snapshot.users)} 人", font=fonts.section, fill=INK)
    y += 44
    _draw_user_table(draw, fonts, snapshot.users, y)

    _save(image, image_path)
    return image_path


def _cached_path(output_dir: Path, payload: dict[str, object]) -> Path:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"sub2api-usage-{digest}.png"


def _status_height(snapshot: Sub2APIUsageSnapshot) -> int:
    lines = _status_lines(snapshot)
    return 30 + sum(max(1, len(_wrap_text(text, 66))) * 30 for text, _ in lines)


def _status_lines(snapshot: Sub2APIUsageSnapshot) -> list[tuple[str, tuple[int, int, int]]]:
    """Describe only global account-list and fixed-window user refresh state."""
    lines: list[tuple[str, tuple[int, int, int]]] = []
    if snapshot.accounts_refreshed_at:
        lines.append((f"账号刷新：{format_datetime(snapshot.accounts_refreshed_at)}", GOOD))
    else:
        lines.append(("账号刷新：暂无成功数据", WARNING))
    if snapshot.accounts_error:
        lines.append((f"账号刷新失败，已保留上次成功缓存：{snapshot.accounts_error}", ERROR))
    if snapshot.users_refreshed_at:
        lines.append((f"用户刷新：{format_datetime(snapshot.users_refreshed_at)}", GOOD))
    else:
        lines.append(("用户刷新：暂无成功数据", WARNING))
    if snapshot.users_error:
        lines.append((f"用户刷新失败，已保留上次成功缓存：{snapshot.users_error}", ERROR))
    return lines


def _draw_status(
    draw: ImageDraw.ImageDraw,
    fonts: _Fonts,
    snapshot: Sub2APIUsageSnapshot,
    y: int,
) -> int:
    height = _status_height(snapshot)
    _rounded_rect(draw, (MARGIN, y, CANVAS_WIDTH - MARGIN, y + height), radius=12, fill=PANEL, outline=BORDER)
    line_y = y + 16
    for text, color in _status_lines(snapshot):
        for line in _wrap_text(text, 66):
            draw.text((MARGIN + 22, line_y), line, font=fonts.small, fill=color)
            line_y += 30
    return y + height


def _account_height(usage: Sub2APIAccountUsage) -> int:
    rows = 1
    rows += max(1, len(_wrap_text(_account_meta(usage), 62)))
    rows += max(1, len(_wrap_text(f"5h：{format_usage_window(usage.five_hour)}", 62)))
    rows += max(1, len(_wrap_text(f"7d：{format_usage_window(usage.seven_day)}", 62)))
    if usage.last_used_at:
        rows += 1
    if usage.updated_at:
        rows += 1
    if usage.error:
        rows += max(1, len(_wrap_text(f"错误：{usage.error}", 62)))
    return 28 + rows * 30 + 16


def _draw_account_panel(
    draw: ImageDraw.ImageDraw,
    fonts: _Fonts,
    usage: Sub2APIAccountUsage,
    y: int,
    height: int,
) -> None:
    """Draw one quota card while constraining long account names to the panel."""
    _rounded_rect(draw, (MARGIN, y, CANVAS_WIDTH - MARGIN, y + height), radius=12, fill=PANEL, outline=BORDER)
    title = _ellipsize(
        draw,
        fonts.body_bold,
        usage.name or f"账号 {usage.account_id}",
        CANVAS_WIDTH - 2 * MARGIN - 44,
    )
    draw.text((MARGIN + 22, y + 16), title, font=fonts.body_bold, fill=INK)
    text_y = y + 54
    rows = [
        (_account_meta(usage), MUTED),
        (f"5h：{format_usage_window(usage.five_hour)}", INK),
        (f"7d：{format_usage_window(usage.seven_day)}", INK),
    ]
    if usage.last_used_at:
        rows.append((f"最近使用：{format_time_text(usage.last_used_at)}", MUTED))
    if usage.updated_at:
        rows.append((f"上游更新时间：{format_time_text(usage.updated_at)}", MUTED))
    if usage.error:
        rows.append((f"错误：{usage.error}", ERROR))
    for text, color in rows:
        for line in _wrap_text(text, 62):
            draw.text((MARGIN + 22, text_y), line, font=fonts.small, fill=color)
            text_y += 30


def _account_meta(usage: Sub2APIAccountUsage) -> str:
    parts = []
    if usage.platform:
        parts.append(usage.platform)
    if usage.account_type:
        parts.append(usage.account_type)
    if usage.status:
        parts.append(usage.status)
    if usage.current_concurrency is not None:
        parts.append(f"并发 {usage.current_concurrency}")
    return "状态：" + (" / ".join(parts) if parts else "未提供")


def _account_ranking_status_lines(
    ranking: Sub2APIAccountSevenDayRanking,
) -> list[tuple[str, tuple[int, int, int]]]:
    """Describe one account's independent ranking refresh and stale-cache state."""
    lines = [
        (
            f"刷新：{format_datetime(ranking.refreshed_at)}"
            if ranking.refreshed_at is not None
            else "刷新：暂无成功数据",
            GOOD if ranking.refreshed_at is not None else WARNING,
        )
    ]
    if ranking.error:
        lines.append((f"刷新失败，已保留上次成功缓存：{ranking.error}", ERROR))
    return lines


def _account_ranking_table_height(ranking: Sub2APIAccountSevenDayRanking) -> int:
    """Measure one account ranking including wrapped status text and one empty row."""
    status_height = 26 + sum(
        max(1, len(_wrap_text(text, 66))) * 30
        for text, _ in _account_ranking_status_lines(ranking)
    )
    return 68 + status_height + max(1, len(ranking.users)) * ROW_HEIGHT


def _draw_account_ranking_table(
    draw: ImageDraw.ImageDraw,
    fonts: _Fonts,
    ranking: Sub2APIAccountSevenDayRanking,
    y: int,
    height: int,
) -> None:
    """Draw one account's independent current-cycle ranking and refresh state."""
    _rounded_rect(draw, (MARGIN, y, CANVAS_WIDTH - MARGIN, y + height), radius=12, fill=PANEL, outline=BORDER)
    header_bottom = y + 68
    draw.rounded_rectangle(
        (MARGIN + 1, y + 1, CANVAS_WIDTH - MARGIN - 1, header_bottom),
        radius=11,
        fill=(232, 240, 250),
    )
    name_x = MARGIN + 28
    draw.text((name_x, y + 20), "用户", font=fonts.body_bold, fill=INK)
    draw.text((1010, y + 22), "本周期消费", font=fonts.small, fill=INK)

    status_y = header_bottom + 12
    for text, color in _account_ranking_status_lines(ranking):
        for line in _wrap_text(text, 66):
            draw.text((name_x, status_y), line, font=fonts.small, fill=color)
            status_y += 30
    row_y = y + 68 + 26 + sum(
        max(1, len(_wrap_text(text, 66))) * 30
        for text, _ in _account_ranking_status_lines(ranking)
    )
    draw.line((MARGIN + 1, row_y, CANVAS_WIDTH - MARGIN - 1, row_y), fill=BORDER, width=1)
    if not ranking.users:
        draw.text((name_x, row_y + 13), "当前账号7d周期内暂无消费用户。", font=fonts.body, fill=MUTED)
        return
    for index, usage in enumerate(ranking.users, start=1):
        if index % 2 == 0:
            draw.rectangle((MARGIN + 1, row_y, CANVAS_WIDTH - MARGIN - 1, row_y + ROW_HEIGHT), fill=(248, 250, 253))
        label = _ellipsize(draw, fonts.body, f"#{index}  {format_sub2api_user_name(usage)}", 850)
        draw.text((name_x, row_y + 13), label, font=fonts.body, fill=INK)
        _draw_right_aligned(draw, fonts.body, f"${usage.actual_cost:.2f}", 1175, row_y + 13, GOOD)
        draw.line((MARGIN + 1, row_y + ROW_HEIGHT, CANVAS_WIDTH - MARGIN - 1, row_y + ROW_HEIGHT), fill=BORDER, width=1)
        row_y += ROW_HEIGHT


def _user_table_height(users: tuple[Sub2APIUserUsage, ...]) -> int:
    """Measure the global fixed-window ranking with one explicit empty-state row."""
    return 68 + max(1, len(users)) * ROW_HEIGHT


# Fixed global ranking amount columns (right-aligned within [left, right]).
# Geometry keeps a stable gap from the username column and between amount columns.
_USER_NAME_X = MARGIN + 28
_USER_DAY_COL = (690, 845)
_USER_WEEK_COL = (861, 1016)
_USER_THIRTY_COL = (1032, 1175)
_USER_NAME_MAX_WIDTH = _USER_DAY_COL[0] - _USER_NAME_X - 16
_AMOUNT_FONT_SIZES = (25, 22, 20, 18, 16, 14)


def _draw_user_table(
    draw: ImageDraw.ImageDraw,
    fonts: _Fonts,
    users: tuple[Sub2APIUserUsage, ...],
    y: int,
) -> None:
    """Draw the global day/week/30d ranking without any account-cycle aggregate."""
    height = _user_table_height(users)
    _rounded_rect(draw, (MARGIN, y, CANVAS_WIDTH - MARGIN, y + height), radius=12, fill=PANEL, outline=BORDER)
    header_bottom = y + 68
    draw.rounded_rectangle((MARGIN + 1, y + 1, CANVAS_WIDTH - MARGIN - 1, header_bottom), radius=11, fill=(232, 240, 250))
    name_x = _USER_NAME_X
    day_left, day_right = _USER_DAY_COL
    week_left, week_right = _USER_WEEK_COL
    thirty_left, thirty_right = _USER_THIRTY_COL
    draw.text((name_x, y + 20), "用户", font=fonts.body_bold, fill=INK)
    # Header labels sit near each column's right edge without claiming body font width.
    for label, right_x in (("当日", day_right), ("本周", week_right), ("30d", thirty_right)):
        draw.text((right_x - draw.textlength(label, font=fonts.small), y + 22), label, font=fonts.small, fill=INK)
    if not users:
        draw.text((name_x, header_bottom + 16), "当前统计周期内暂无消费用户。", font=fonts.body, fill=MUTED)
        return
    row_y = header_bottom
    for index, usage in enumerate(users, start=1):
        if index % 2 == 0:
            draw.rectangle((MARGIN + 1, row_y, CANVAS_WIDTH - MARGIN - 1, row_y + ROW_HEIGHT), fill=(248, 250, 253))
        label = _ellipsize(
            draw,
            fonts.body,
            f"#{index}  {format_sub2api_user_name(usage)}",
            _USER_NAME_MAX_WIDTH,
        )
        draw.text((name_x, row_y + 13), label, font=fonts.body, fill=INK)
        _draw_amount_in_column(
            draw,
            fonts,
            usage.current_day_actual_cost,
            day_left,
            day_right,
            row_y + 13,
        )
        _draw_amount_in_column(
            draw,
            fonts,
            usage.current_week_actual_cost,
            week_left,
            week_right,
            row_y + 13,
        )
        _draw_amount_in_column(
            draw,
            fonts,
            usage.thirty_day_actual_cost,
            thirty_left,
            thirty_right,
            row_y + 13,
        )
        draw.line((MARGIN + 1, row_y + ROW_HEIGHT, CANVAS_WIDTH - MARGIN - 1, row_y + ROW_HEIGHT), fill=BORDER, width=1)
        row_y += ROW_HEIGHT


def _format_amount_plain(value: float) -> str:
    """Default money text: currency symbol and two decimal places."""
    return f"${value:.2f}"


def _format_amount_compact(value: float) -> str:
    """Non-misleading compact money text that still keeps $ and two decimals."""
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000_000:
        return f"{sign}${magnitude / 1_000_000_000:.2f}B"
    if magnitude >= 1_000_000:
        return f"{sign}${magnitude / 1_000_000:.2f}M"
    if magnitude >= 10_000:
        return f"{sign}${magnitude / 1_000:.2f}K"
    return _format_amount_plain(value)


def _format_amount_scientific(value: float) -> str:
    """Currency scientific form with two significant decimals, e.g. $1.00e+308."""
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.2e}"


def _draw_amount_in_column(
    draw: ImageDraw.ImageDraw,
    fonts: _Fonts,
    value: float,
    left_x: int,
    right_x: int,
    y: int,
) -> None:
    """Right-align an amount inside a fixed column, shrinking font or compacting text."""
    max_width = max(1, right_x - left_x)
    plain = _format_amount_plain(value)
    text, font = _fit_amount_text(draw, fonts, plain, value, max_width)
    width = draw.textlength(text, font=font)
    # Final text is required to fit; right-align within the fixed column.
    x = right_x - width
    draw.text((x, y), text, font=font, fill=GOOD)


def _fit_amount_text(
    draw: ImageDraw.ImageDraw,
    fonts: _Fonts,
    plain: str,
    value: float,
    max_width: int,
) -> tuple[str, ImageFont.ImageFont]:
    """Pick plain, compact, then scientific text with the largest fitting font."""
    candidates = (
        plain,
        _format_amount_compact(value),
        _format_amount_scientific(value),
    )
    for text in candidates:
        for size in _AMOUNT_FONT_SIZES:
            font = fonts.sized(size)
            if draw.textlength(text, font=font) <= max_width:
                return text, font
    # Scientific at the smallest size is the last guaranteed short form for finite floats.
    scientific = candidates[-1]
    return scientific, fonts.sized(_AMOUNT_FONT_SIZES[-1])


def _draw_empty_panel(draw: ImageDraw.ImageDraw, fonts: _Fonts, y: int, text: str) -> int:
    height = 72
    _rounded_rect(draw, (MARGIN, y, CANVAS_WIDTH - MARGIN, y + height), radius=12, fill=PANEL, outline=BORDER)
    draw.text((MARGIN + 22, y + 22), text, font=fonts.body, fill=MUTED)
    return y + height


def _wrap_text(text: str, width: int) -> list[str]:
    wrapped: list[str] = []
    for raw_line in str(text or "").splitlines() or [""]:
        line = raw_line.strip()
        if line:
            wrapped.extend(textwrap.wrap(line, width=width, break_long_words=True, replace_whitespace=False))
    return wrapped or [""]


def _ellipsize(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, text: str, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    value = text
    while value and draw.textlength(value + ellipsis, font=font) > max_width:
        value = value[:-1]
    return value + ellipsis


def _draw_right_aligned(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    text: str,
    right_x: int,
    y: int,
    fill: tuple[int, int, int],
) -> None:
    draw.text((right_x - draw.textlength(text, font=font), y), text, font=font, fill=fill)


def _rounded_rect(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    *,
    radius: int,
    fill: tuple[int, int, int],
    outline: tuple[int, int, int],
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=2)


class _Fonts:
    def __init__(self, font_path: str | None) -> None:
        self._font_path = font_path
        self._sized: dict[int, ImageFont.ImageFont] = {}
        self.title = self.sized(46)
        self.subtitle = self.sized(24)
        self.section = self.sized(31)
        self.body = self.sized(25)
        self.body_bold = self.sized(27)
        self.small = self.sized(22)

    def sized(self, size: int) -> ImageFont.ImageFont:
        """Return a cached truetype/default font for the requested pixel size."""
        font = self._sized.get(size)
        if font is None:
            font = _font(self._font_path, size)
            self._sized[size] = font
        return font


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


def _save(image: Image.Image, path: Path) -> None:
    tmp_path = path.with_suffix(".tmp.png")
    image.save(tmp_path, "PNG", optimize=True)
    tmp_path.replace(path)
