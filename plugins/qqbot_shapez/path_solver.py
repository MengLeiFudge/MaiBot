from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache


MAX_LAYER = 4
QUAD_NUM = 4


class ShapeMethod(str, Enum):
    BASIC = "basic"
    CUT = "cut"
    IMPOSSIBLE = "impossible"
    STACK = "stack"


@dataclass(frozen=True, slots=True)
class ShapeInfo:
    step: int
    parent1: int
    parent2: int


@dataclass(slots=True)
class ShapeTree:
    shortcode: str
    method: ShapeMethod
    children: list["ShapeTree"] = field(default_factory=list)


def solve_shape_path(shortcode: str) -> ShapeTree:
    return _solve_shape(_from_short_key(shortcode.strip()))


def render_shape_tree_text(root: ShapeTree) -> str:
    if not root.children:
        return f"{root.shortcode} ({root.method.value})"

    lines: list[str] = []

    def walk(node: ShapeTree, prefix: str = "", is_last: bool = True) -> None:
        connector = "" if not prefix else ("└─ " if is_last else "├─ ")
        lines.append(f"{prefix}{connector}{node.shortcode} ({node.method.value})")
        child_prefix = prefix + ("   " if is_last else "│  ") if prefix else ""
        for index, child in enumerate(node.children):
            walk(child, child_prefix, index == len(node.children) - 1)

    walk(root)
    return "\n".join(lines)


def _solve_shape(shape: list[list[tuple[str, str] | None]]) -> ShapeTree:
    try:
        shape_top, shape_bottom = _shape_unstack(shape)
        return ShapeTree(
            _to_short_key(shape),
            ShapeMethod.STACK,
            children=[_solve_shape(shape_bottom), _solve_shape(shape_top)],
        )
    except _BasicShape:
        if len(shape) == 1:
            return ShapeTree(_to_short_key(shape), ShapeMethod.BASIC)
        shape_before = _shape_uncut(shape)
        return ShapeTree(
            _to_short_key(shape),
            ShapeMethod.CUT,
            children=[ShapeTree(_to_short_key(shape_before), ShapeMethod.BASIC)],
        )
    except _ImpossibleShape:
        return ShapeTree(_to_short_key(shape), ShapeMethod.IMPOSSIBLE)


def _from_short_key(key: str) -> list[list[tuple[str, str] | None]]:
    source_layers = key.split(":")
    if not key or len(source_layers) > MAX_LAYER:
        raise ValueError(f"Only {MAX_LAYER} layers allowed")

    layers: list[list[tuple[str, str] | None]] = []
    for text in source_layers:
        if len(text) != 8:
            raise ValueError(f"Invalid layer: {text}")
        if text == "--" * QUAD_NUM:
            raise ValueError(f"Empty layers are not allowed: {text}")
        quads: list[tuple[str, str] | None] = [None] * QUAD_NUM
        for index in range(QUAD_NUM):
            shape_text = text[index * 2]
            color_text = text[index * 2 + 1]
            if shape_text == "-" or color_text == "-":
                if shape_text != "-" or color_text != "-":
                    raise ValueError(f"Invalid shape key: {shape_text}{color_text}")
                continue
            if shape_text not in {"C", "R", "S", "W", "X"}:
                raise ValueError(f"Unsupported path shape: {shape_text}")
            if color_text not in {"r", "g", "b", "y", "p", "c", "w", "u", "x"}:
                raise ValueError(f"Unsupported path color: {color_text}")
            quads[index] = (shape_text, color_text)
        layers.append(quads)
    return layers


def _to_short_key(shape: list[list[tuple[str, str] | None]]) -> str:
    layers: list[str] = []
    for layer in shape:
        parts: list[str] = []
        for quad in layer:
            parts.append("--" if quad is None else quad[0] + quad[1])
        layers.append("".join(parts))
    return ":".join(layers)


def _to_id(shape: list[list[tuple[str, str] | None]]) -> int:
    result = 0
    for layer_index, layer in enumerate(shape):
        for quad_index, quad in enumerate(layer):
            if quad is not None:
                result |= 1 << (layer_index * QUAD_NUM + quad_index)
    return result


def _extract(shape: list[list[tuple[str, str] | None]], mask: int) -> list[list[tuple[str, str] | None]]:
    layers = [
        [quad if mask & (1 << (layer_index * QUAD_NUM + quad_index)) else None for quad_index, quad in enumerate(layer)]
        for layer_index, layer in enumerate(shape)
    ]
    if mask >> (MAX_LAYER * QUAD_NUM):
        layers.append(_from_short_key("CuCuCuCu")[0])
    return [layer for layer in layers if any(layer)]


def _fill(shape: list[list[tuple[str, str] | None]], fill_quad_key: str) -> list[list[tuple[str, str] | None]]:
    fill_quad = (fill_quad_key[0], fill_quad_key[1])
    return [[quad if quad is not None else fill_quad for quad in layer] for layer in shape]


class _BasicShape(Exception):
    pass


class _ImpossibleShape(Exception):
    pass


def _shape_unstack(shape: list[list[tuple[str, str] | None]]) -> tuple[list[list[tuple[str, str] | None]], list[list[tuple[str, str] | None]]]:
    shape_id = _to_id(shape)
    table = _shape_table()
    if shape_id not in table:
        raise _ImpossibleShape()
    info = table[shape_id]
    if info.parent1 == -1:
        raise _BasicShape()
    top_id = _stack_raw(info.parent1, info.parent2) & ~info.parent2
    return _extract(shape, top_id), _extract(shape, info.parent2)


def _shape_uncut(shape: list[list[tuple[str, str] | None]]) -> list[list[tuple[str, str] | None]]:
    shape_id = _to_id(shape)
    support_quads = 0x3333
    while shape_id & support_quads:
        support_quads = _rotate90(support_quads)
    return _extract(_fill(shape, "Xx"), shape_id | support_quads)


@lru_cache(maxsize=1)
def _shape_table() -> dict[int, ShapeInfo]:
    shapes = {
        shape_id: ShapeInfo(step, -1, -1)
        for step, shape_ids in [
            (0, {0xF}),
            (1, range(0x1, 0xF)),
            (2, _get_family(0x12)),
            (3, _get_family(0x121)),
            (4, _get_family(0x1212)),
        ]
        for shape_id in shape_ids
    }

    base_shapes = sorted(shapes)
    to_process = set(shapes)
    while to_process:
        left = to_process.pop()
        for right in base_shapes:
            step_new = shapes[left].step + shapes[right].step + 1
            new_id = _stack(left, right)
            if new_id not in shapes:
                to_process.add(new_id)
                shapes[new_id] = ShapeInfo(step_new, left, right)
            elif step_new < shapes[new_id].step:
                shapes[new_id] = ShapeInfo(step_new, left, right)

    for left in sorted(shapes):
        for right in base_shapes:
            step_new = shapes[right].step + shapes[left].step + 1
            new_id = _stack(right, left)
            if new_id not in shapes:
                shapes[new_id] = ShapeInfo(step_new, right, left)
    return shapes


def _rotate90(shape: int) -> int:
    return ((shape & 0x7777) << 1) | ((shape & 0x8888) >> 3)


def _rotate180(shape: int) -> int:
    return ((shape & 0x3333) << 2) | ((shape & 0xCCCC) >> 2)


def _rotate270(shape: int) -> int:
    return ((shape & 0x1111) << 3) | ((shape & 0xEEEE) >> 1)


def _mirror(shape: int) -> int:
    return ((shape & 0x5555) << 1) | ((shape & 0xAAAA) >> 1)


def _fall(shape: int) -> int:
    layer1 = shape & 0x000F
    layer2 = shape & 0x00F0
    layer3 = shape & 0x0F00
    layer4 = shape & 0xF000
    layer5 = shape & 0xF_0000
    layer4 = (layer4 | layer5) if layer4 else (layer5 >> 4)
    layer3 = (layer3 | layer4) if layer3 else (layer4 >> 4)
    layer2 = (layer2 | layer3) if layer2 else (layer3 >> 4)
    layer1 = (layer1 | layer2) if layer1 else (layer2 >> 4)
    return layer1


def _stack_raw(top: int, bottom: int) -> int:
    if (top << 12) & bottom:
        return bottom
    if (top << 8) & bottom:
        return (top << 12) | bottom
    if (top << 4) & bottom:
        return (top << 8) | bottom
    if top & bottom:
        return (top << 4) | bottom
    return top | bottom


def _stack(top: int, bottom: int) -> int:
    return _stack_raw(top, bottom) & 0xFFFF


def _get_family(shape: int) -> set[int]:
    return {
        shape,
        _rotate90(shape),
        _rotate180(shape),
        _rotate270(shape),
        _mirror(shape),
        _mirror(_rotate90(shape)),
        _mirror(_rotate180(shape)),
        _mirror(_rotate270(shape)),
    }
