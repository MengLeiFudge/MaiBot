from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re

from PIL import Image, ImageDraw

from .path_renderer import render_shape_path_image


SHAPE_TOKEN_PATTERN = r"(?:--|__|P-|c[rgbypcuwolmhzik]|[CRSW123456X][rgbypcuwolmhzik_])"
SHAPE_PATTERN = re.compile(rf"{SHAPE_TOKEN_PATTERN}{{4}}(?::{SHAPE_TOKEN_PATTERN}{{4}}){{0,4}}")
MAX_LAYERS = 5
QUADS_PER_LAYER = 4
LINKABLE_SUB_SHAPES = frozenset("CRSW123456X")
SUB_SHAPES = LINKABLE_SUB_SHAPES | frozenset("Pc")

COLOR_MAP = {
    "u": "#aaaaaa",
    "r": "#ff666a",
    "g": "#78ff66",
    "b": "#66a7ff",
    "y": "#fcf52a",
    "p": "#dd66ff",
    "c": "#87fff5",
    "w": "#ffffff",
    "o": "#fdad4a",
    "l": "#bafa48",
    "m": "#3cfdb2",
    "h": "#33d1ff",
    "z": "#a186ff",
    "i": "#ee66b4",
    "k": "#202020",
    "s": "#00000000",
}

CRYSTAL_COLOR_MAP = {
    "r": ("#ff0000", "#993333"),
    "g": ("#00ff00", "#339933"),
    "b": ("#0000ff", "#333399"),
    "y": ("#ffff00", "#999933"),
    "p": ("#ff00ff", "#993399"),
    "c": ("#00ffff", "#339999"),
    "w": ("#ffffff", "#999999"),
    "u": ("#aaaaaa", "#444444"),
    "o": ("#ff7700", "#996633"),
    "l": ("#77ff00", "#669933"),
    "m": ("#00ff77", "#339966"),
    "h": ("#0077ff", "#336699"),
    "z": ("#7700ff", "#663399"),
    "i": ("#ff0077", "#993366"),
    "k": ("#000000", "#333333"),
}

CHART_SHAPE_MAP = {
    "C": "图",
    "R": "图",
    "S": "图",
    "W": "图",
    "1": "图",
    "2": "图",
    "3": "图",
    "4": "图",
    "5": "图",
    "6": "图",
    "X": "图",
    "P": "顶",
    "c": "晶",
    "-": "　",
}


@dataclass(frozen=True, slots=True)
class ShapeCode:
    short_key: str

    def __post_init__(self) -> None:
        normalized = normalize_shape_code(self.short_key)
        object.__setattr__(self, "short_key", normalized)
        validate_shape_code(normalized)

    @property
    def layers(self) -> list[str]:
        return self.short_key.split(":")

    @property
    def layer_count(self) -> int:
        return len(self.layers)


def render_shape_image(shape: ShapeCode, output: Path, size: int = 384) -> Path:
    image = generate_shape_image(shape, size=size)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG")
    return output


def render_shape_code(output_root: Path, short_code: str) -> tuple[ShapeCode, Path]:
    shape = ShapeCode(short_code)
    output = Path(output_root) / "shapez" / "shape" / (
        shape.short_key.replace(":", "：") + ".png"
    )
    render_shape_image(shape, output)
    return shape, output


def render_shape_chart(output_root: Path, short_code: str) -> tuple[ShapeCode, Path, str]:
    shape = ShapeCode(short_code)
    output = Path(output_root) / "shapez" / "chart" / (
        shape.short_key.replace(":", "：") + ".png"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    generate_shape_chart(shape).save(output, format="PNG")
    return shape, output, describe_shape_layers(shape)


def render_shape_path(output_root: Path, short_code: str):
    return render_shape_path_image(output_root, short_code)


def generate_shape_image(shape: ShapeCode, size: int = 384) -> Image.Image:
    scale = 3
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (245, 245, 245, 255))
    draw = ImageDraw.Draw(image)
    center = canvas_size / 2
    base_radius = canvas_size * 0.38
    draw.ellipse(
        [
            center - base_radius * 1.15,
            center - base_radius * 1.15,
            center + base_radius * 1.15,
            center + base_radius * 1.15,
        ],
        fill=(225, 226, 227, 255),
    )

    for layer_index, layer in enumerate(shape.layers):
        layer_scale = max(0.1, 0.9 - layer_index * 0.18)
        layer_radius = base_radius * layer_scale
        for quadrant, token in enumerate(iter_effective_layer_tokens(layer)):
            if token != "--":
                _draw_shape_token(draw, center, layer_radius, quadrant, token)
    return image.resize((size, size), resample=Image.Resampling.LANCZOS)


def generate_shape_chart(shape: ShapeCode) -> Image.Image:
    row_height = 50
    col_width = 50
    x_start = 10
    y_start = 10
    width = 2 * x_start + QUADS_PER_LAYER * col_width
    height = 2 * y_start + MAX_LAYERS * row_height
    y_end = y_start + MAX_LAYERS * row_height
    image = Image.new("RGB", (width, height), color="lightgray")
    draw = ImageDraw.Draw(image)
    draw.rectangle([-1, -1, width + 1, y_start], fill="whitesmoke", outline="lightslategray", width=1)
    draw.rectangle([-1, y_end, width + 1, height + 1], fill="darkgray", outline="black", width=1)

    for layer_index, layer in enumerate(shape.layers):
        y = y_end - (layer_index + 1) * row_height
        for quadrant, token in enumerate(iter_effective_layer_tokens(layer)):
            if token == "--":
                continue
            x = x_start + quadrant * col_width
            shape_text, color_text = token
            if shape_text == "P":
                shrink = 10
                draw.rectangle(
                    [x + shrink, y, x + col_width - shrink, y + row_height],
                    fill="lightslategray",
                    outline="black",
                    width=1,
                )
            elif shape_text == "c":
                _draw_chart_crystal(draw, x, y, col_width, row_height, color_text)
            else:
                fill = COLOR_MAP.get(color_text if color_text != "_" else "u", COLOR_MAP["u"])
                draw.rectangle([x, y, x + col_width, y + row_height], fill=fill, outline="black", width=1)
            draw.text((x + 14, y + 7), shape_text, fill="dimgray")
    return image


def normalize_shape_code(short_code: str) -> str:
    return short_code.replace("：", ":").strip()


def validate_shape_code(short_code: str) -> None:
    layers = short_code.split(":")
    if not short_code or len(layers) > MAX_LAYERS:
        raise ValueError(f"invalid shape code: {short_code}")
    for layer in layers:
        if len(layer) != QUADS_PER_LAYER * 2:
            raise ValueError(f"invalid shape layer: {layer}")
        if layer == "--" * QUADS_PER_LAYER:
            raise ValueError(f"empty shape layer: {layer}")
        for index in range(QUADS_PER_LAYER):
            validate_shape_token(layer[index * 2 : index * 2 + 2])
        validate_linked_tokens(layer)


def validate_shape_token(token: str) -> None:
    shape_text, color_text = token
    if token == "--":
        return
    if shape_text == "P" and color_text == "-":
        return
    if shape_text == "c" and color_text in CRYSTAL_COLOR_MAP:
        return
    if shape_text in LINKABLE_SUB_SHAPES and color_text in COLOR_MAP and color_text != "s":
        return
    if shape_text == "_" and color_text == "_":
        return
    if shape_text in SUB_SHAPES and color_text == "_":
        return
    raise ValueError(f"invalid shape token: {token}")


def validate_linked_tokens(layer: str) -> None:
    previous_shape: str | None = None
    previous_color: str | None = None
    for index in range(QUADS_PER_LAYER):
        shape_text, color_text = layer[index * 2 : index * 2 + 2]
        if shape_text == "-" or shape_text in {"P", "c"}:
            previous_shape = None
            previous_color = None
            continue
        if color_text == "_":
            if previous_shape is None or previous_color is None:
                raise ValueError(f"linked token has no previous segment: {layer}")
            if shape_text == "_":
                shape_text = previous_shape
        if shape_text in LINKABLE_SUB_SHAPES:
            previous_shape = shape_text
            previous_color = color_text if color_text != "_" else previous_color
        else:
            previous_shape = None
            previous_color = None


def iter_effective_layer_tokens(layer: str) -> list[str]:
    tokens: list[str] = []
    previous_shape: str | None = None
    previous_color: str | None = None
    for index in range(QUADS_PER_LAYER):
        shape_text, color_text = layer[index * 2 : index * 2 + 2]
        if shape_text == "-" or shape_text in {"P", "c"}:
            previous_shape = None
            previous_color = None
            tokens.append(shape_text + color_text)
            continue
        if color_text == "_":
            if previous_shape is None or previous_color is None:
                raise ValueError(f"linked token has no previous segment: {layer}")
            if shape_text == "_":
                shape_text = previous_shape
            color_text = previous_color
        tokens.append(shape_text + color_text)
        if shape_text in LINKABLE_SUB_SHAPES:
            previous_shape = shape_text
            previous_color = color_text
        else:
            previous_shape = None
            previous_color = None
    return tokens


def describe_shape_layers(shape: ShapeCode) -> str:
    return "\n".join(
        "".join(CHART_SHAPE_MAP.get(token[0], "图") for token in iter_effective_layer_tokens(layer))
        for layer in reversed(shape.layers)
    )


def _draw_shape_token(
    draw: ImageDraw.ImageDraw,
    center: float,
    radius: float,
    quadrant: int,
    token: str,
) -> None:
    shape_type = token[0]
    color_text = token[1]
    start_angle = [270, 0, 90, 180][quadrant]
    bbox = [center - radius, center - radius, center + radius, center + radius]

    if shape_type == "P":
        _draw_pin_quadrant(draw, center, radius, quadrant)
        return
    if shape_type == "c":
        _draw_crystal_quadrant(draw, center, radius, quadrant, color_text)
        return

    color = COLOR_MAP.get(color_text if color_text != "_" else "u", COLOR_MAP["u"])
    if color == "#00000000":
        return
    if shape_type == "C":
        draw.pieslice(bbox, start=start_angle, end=start_angle + 90, fill=color, outline="#555555", width=4)
    elif shape_type in {"R", "X", "2", "3"}:
        _draw_polygon_quadrant(draw, center, radius, quadrant, color)
    elif shape_type in {"S", "1", "6"}:
        _draw_star_quadrant(draw, center, radius, quadrant, color)
    elif shape_type in {"W", "4", "5"}:
        _draw_windmill_quadrant(draw, center, radius, quadrant, color)


def _draw_polygon_quadrant(
    draw: ImageDraw.ImageDraw,
    center: float,
    radius: float,
    quadrant: int,
    color: str,
) -> None:
    points = {
        0: [(center, center), (center + radius, center), (center + radius, center - radius), (center, center - radius)],
        1: [(center, center), (center + radius, center), (center + radius, center + radius), (center, center + radius)],
        2: [(center, center), (center - radius, center), (center - radius, center + radius), (center, center + radius)],
        3: [(center, center), (center - radius, center), (center - radius, center - radius), (center, center - radius)],
    }[quadrant]
    draw.polygon(points, fill=color, outline="#555555")


def _draw_star_quadrant(
    draw: ImageDraw.ImageDraw,
    center: float,
    radius: float,
    quadrant: int,
    color: str,
) -> None:
    half = radius / 2
    points = {
        0: [(center, center), (center + radius, center - radius), (center + half, center), (center, center - half)],
        1: [(center, center), (center + radius, center + radius), (center, center + half), (center + half, center)],
        2: [(center, center), (center - radius, center + radius), (center - half, center), (center, center + half)],
        3: [(center, center), (center - radius, center - radius), (center, center - half), (center - half, center)],
    }[quadrant]
    draw.polygon(points, fill=color, outline="#555555")


def _draw_windmill_quadrant(
    draw: ImageDraw.ImageDraw,
    center: float,
    radius: float,
    quadrant: int,
    color: str,
) -> None:
    half = radius / 2
    points = {
        0: [(center, center), (center + radius, center - radius), (center + radius, center), (center + half, center)],
        1: [(center, center), (center + radius, center + radius), (center, center + radius), (center, center + half)],
        2: [(center, center), (center - radius, center + radius), (center - radius, center), (center - half, center)],
        3: [(center, center), (center - radius, center - radius), (center, center - radius), (center, center - half)],
    }[quadrant]
    draw.polygon(points, fill=color, outline="#555555")


def _draw_pin_quadrant(draw: ImageDraw.ImageDraw, center: float, radius: float, quadrant: int) -> None:
    angle = math.radians([315, 45, 135, 225][quadrant])
    pin_center = (center + math.cos(angle) * radius * 0.45, center + math.sin(angle) * radius * 0.45)
    pin_radius = radius * 0.18
    draw.ellipse(
        [
            pin_center[0] - pin_radius,
            pin_center[1] - pin_radius,
            pin_center[0] + pin_radius,
            pin_center[1] + pin_radius,
        ],
        fill=COLOR_MAP["u"],
        outline="#555555",
        width=4,
    )


def _draw_crystal_quadrant(
    draw: ImageDraw.ImageDraw,
    center: float,
    radius: float,
    quadrant: int,
    color_text: str,
) -> None:
    start_color, end_color = CRYSTAL_COLOR_MAP[color_text]
    start_angle = [270, 0, 90, 180][quadrant]
    step_count = 6
    for index in range(step_count):
        shrink = index * radius * 0.09
        bbox = [center - radius + shrink, center - radius + shrink, center + radius - shrink, center + radius - shrink]
        draw.pieslice(
            bbox,
            start=start_angle + index * 3,
            end=start_angle + 90 - index * 3,
            fill=_interpolate_hex(start_color, end_color, index / max(1, step_count - 1)),
        )
    draw.line(
        [
            (center, center),
            _polar_point(center, radius, start_angle),
            _polar_point(center, radius, start_angle + 90),
            (center, center),
        ],
        fill="#555555",
        width=3,
    )


def _draw_chart_crystal(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    color_text: str,
) -> None:
    start_color, end_color = CRYSTAL_COLOR_MAP[color_text]
    for index in range(4):
        ratio = index / 3
        shrink = int(20 * ratio)
        draw.rectangle(
            [x + shrink, y, x + width - shrink, y + height],
            fill=_interpolate_hex(start_color, end_color, ratio),
            width=0,
        )
    draw.rectangle([x, y, x + width, y + height], outline="dimgray", width=1)


def _interpolate_hex(start: str, end: str, ratio: float) -> tuple[int, int, int]:
    start_rgb = tuple(int(start[index : index + 2], 16) for index in (1, 3, 5))
    end_rgb = tuple(int(end[index : index + 2], 16) for index in (1, 3, 5))
    return tuple(round(start_rgb[index] + (end_rgb[index] - start_rgb[index]) * ratio) for index in range(3))


def _polar_point(center: float, radius: float, degree: float) -> tuple[float, float]:
    radian = math.radians(degree)
    return center + math.cos(radian) * radius, center + math.sin(radian) * radius
