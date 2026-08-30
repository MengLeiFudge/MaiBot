from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from .path_solver import ShapeTree, render_shape_tree_text, solve_shape_path


PATH_NODE_SIZE = 108
PATH_NODE_GAP_X = 34
PATH_NODE_GAP_Y = 54

PATH_COLOR_MAP = {
    "r": "#ff666a",
    "g": "#78ff66",
    "b": "#66a7ff",
    "y": "#fcf52a",
    "p": "#dd66ff",
    "c": "#87fff5",
    "w": "#ffffff",
    "u": "#aaaaaa",
    "x": "#d0d0d0",
}


def render_shape_path_image(output_root: Path, short_code: str) -> tuple[ShapeTree, Path, str]:
    tree = solve_shape_path(short_code)
    image = generate_shape_path_image(tree)
    output = Path(output_root) / "shapez" / "path" / (
        tree.shortcode.replace(":", "：") + ".png"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG")
    return tree, output, render_shape_tree_text(tree)


def generate_shape_path_image(root: ShapeTree) -> Image.Image:
    positions: dict[int, tuple[float, float]] = {}
    next_x = 0

    def layout(node: ShapeTree, depth: int) -> float:
        nonlocal next_x
        node_id = id(node)
        if not node.children:
            x = next_x
            next_x += PATH_NODE_SIZE + PATH_NODE_GAP_X
        else:
            child_xs = [layout(child, depth + 1) for child in node.children]
            x = sum(child_xs) / len(child_xs)
        positions[node_id] = (x, depth * (PATH_NODE_SIZE + PATH_NODE_GAP_Y))
        return x

    layout(root, 0)
    min_x = min(x for x, _ in positions.values()) - PATH_NODE_SIZE / 2
    max_x = max(x for x, _ in positions.values()) + PATH_NODE_SIZE / 2
    min_y = min(y for _, y in positions.values()) - PATH_NODE_SIZE / 2
    max_y = max(y for _, y in positions.values()) + PATH_NODE_SIZE / 2
    padding = 28
    width = int(max_x - min_x + padding * 2)
    height = int(max_y - min_y + padding * 2)
    image = Image.new("RGBA", (width, height), (224, 226, 230, 255))
    draw = ImageDraw.Draw(image)

    def canvas_pos(node: ShapeTree) -> tuple[int, int]:
        x, y = positions[id(node)]
        return int(x - min_x + padding), int(y - min_y + padding)

    def draw_edges(node: ShapeTree) -> None:
        x, y = canvas_pos(node)
        for child in node.children:
            cx, cy = canvas_pos(child)
            draw.line(
                [(x, y + PATH_NODE_SIZE // 2), (cx, cy - PATH_NODE_SIZE // 2)],
                fill=(85, 85, 85, 255),
                width=5,
            )
            draw_edges(child)

    def draw_nodes(node: ShapeTree) -> None:
        x, y = canvas_pos(node)
        shape_image = generate_path_shape_image(node.shortcode, size=PATH_NODE_SIZE)
        image.paste(shape_image, (x - PATH_NODE_SIZE // 2, y - PATH_NODE_SIZE // 2), shape_image)
        for child in node.children:
            draw_nodes(child)

    draw_edges(root)
    draw_nodes(root)
    return image


def generate_path_shape_image(short_code: str, size: int = 128) -> Image.Image:
    scale = 3
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = canvas_size / 2
    base_radius = canvas_size * 0.40
    draw.ellipse(
        [
            center - base_radius * 1.15,
            center - base_radius * 1.15,
            center + base_radius * 1.15,
            center + base_radius * 1.15,
        ],
        fill=(40, 50, 65, 25),
    )

    for layer_index, layer in enumerate(short_code.split(":")):
        layer_scale = max(0.1, 0.9 - layer_index * 0.22)
        radius = base_radius * layer_scale
        for quadrant in range(4):
            token = layer[quadrant * 2 : quadrant * 2 + 2]
            if token == "--":
                continue
            _draw_path_token(draw, center, radius, quadrant, token)
    return image.resize((size, size), resample=Image.Resampling.LANCZOS)


def _draw_path_token(
    draw: ImageDraw.ImageDraw,
    center: float,
    radius: float,
    quadrant: int,
    token: str,
) -> None:
    shape_type = token[0]
    color_text = token[1]
    color = PATH_COLOR_MAP.get(color_text, PATH_COLOR_MAP["u"])
    start_angle = [270, 0, 90, 180][quadrant]
    bbox = [center - radius, center - radius, center + radius, center + radius]
    if shape_type == "C":
        draw.pieslice(bbox, start=start_angle, end=start_angle + 90, fill=color, outline="#555555", width=4)
    elif shape_type in {"R", "X"}:
        draw.polygon(_quadrant_rect_points(center, radius, quadrant), fill=color, outline="#555555")
    elif shape_type == "S":
        draw.polygon(_quadrant_star_points(center, radius, quadrant), fill=color, outline="#555555")
    elif shape_type == "W":
        draw.polygon(_quadrant_windmill_points(center, radius, quadrant), fill=color, outline="#555555")


def _quadrant_rect_points(center: float, radius: float, quadrant: int) -> list[tuple[float, float]]:
    return {
        0: [(center, center), (center + radius, center), (center + radius, center - radius), (center, center - radius)],
        1: [(center, center), (center + radius, center), (center + radius, center + radius), (center, center + radius)],
        2: [(center, center), (center - radius, center), (center - radius, center + radius), (center, center + radius)],
        3: [(center, center), (center - radius, center), (center - radius, center - radius), (center, center - radius)],
    }[quadrant]


def _quadrant_star_points(center: float, radius: float, quadrant: int) -> list[tuple[float, float]]:
    half = radius * 0.6
    return {
        0: [(center, center), (center + radius, center - radius), (center + half, center), (center, center - half)],
        1: [(center, center), (center + radius, center + radius), (center, center + half), (center + half, center)],
        2: [(center, center), (center - radius, center + radius), (center - half, center), (center, center + half)],
        3: [(center, center), (center - radius, center - radius), (center, center - half), (center - half, center)],
    }[quadrant]


def _quadrant_windmill_points(center: float, radius: float, quadrant: int) -> list[tuple[float, float]]:
    half = radius * 0.6
    return {
        0: [(center, center), (center + radius, center - radius), (center + radius, center), (center + half, center)],
        1: [(center, center), (center + radius, center + radius), (center, center + radius), (center, center + half)],
        2: [(center, center), (center - radius, center + radius), (center - radius, center), (center - half, center)],
        3: [(center, center), (center - radius, center - radius), (center, center - radius), (center, center - half)],
    }[quadrant]
