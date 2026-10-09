#!/usr/bin/env python3
"""在 MaiBot 启动前强制应用夜凛最小插件允许策略。"""

from pathlib import Path

import os
import re
import sys
import tempfile
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugins"
ALLOWED_PLUGINS = frozenset({"napcat_adapter", "qqbot_knowledge", "qqbot_poke", "qqbot_identity", "qqbot_visual"})
PLUGIN_MANAGEMENT_CONFIG = PROJECT_ROOT / "src" / "plugins" / "built_in" / "plugin_management" / "config.toml"
PLUGIN_SECTION_RE = re.compile(
    r"^(?P<header>\[plugin\][^\S\r\n]*(?:\r?\n|$))(?P<body>.*?)(?=^\[|\Z)",
    re.MULTILINE | re.DOTALL,
)
ENABLED_RE = re.compile(r"^(?P<prefix>\s*enabled\s*=\s*)(?:true|false)(?P<suffix>\s*(?:#.*)?)$", re.MULTILINE)


def disabled_content(path: Path) -> tuple[str, bool]:
    if not path.exists():
        return "[plugin]\nenabled = false\n", True

    original = path.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(original)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"无法解析插件配置 {path}: {exc}") from exc

    plugin = parsed.get("plugin")
    if isinstance(plugin, dict) and plugin.get("enabled") is False:
        return original, False

    section_match = PLUGIN_SECTION_RE.search(original)
    if section_match is None:
        separator = "" if not original or original.endswith(("\n", "\r")) else "\n"
        updated = f"{original}{separator}\n[plugin]\nenabled = false\n"
    else:
        body = section_match.group("body")
        enabled_matches = list(ENABLED_RE.finditer(body))
        if len(enabled_matches) > 1:
            raise RuntimeError(f"插件配置含有重复的 plugin.enabled，拒绝改写: {path}")
        if enabled_matches:
            updated_body = ENABLED_RE.sub(r"\g<prefix>false\g<suffix>", body, count=1)
        else:
            updated_body = f"enabled = false\n{body}"
        updated = f"{original[:section_match.start('body')]}{updated_body}{original[section_match.end('body'):]}"

    try:
        verified = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"策略生成了无效配置 {path}: {exc}") from exc
    verified_plugin = verified.get("plugin")
    if not isinstance(verified_plugin, dict) or verified_plugin.get("enabled") is not False:
        raise RuntimeError(f"无法确认插件已禁用: {path}")
    return updated, True


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as temporary_file:
            temporary_file.write(content)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    targets = [
        directory / "config.toml"
        for directory in sorted(PLUGIN_ROOT.iterdir(), key=lambda item: item.name)
        if directory.is_dir() and directory.name not in ALLOWED_PLUGINS
    ]
    targets.append(PLUGIN_MANAGEMENT_CONFIG)

    corrections: list[Path] = []
    for target in targets:
        content, changed = disabled_content(target)
        if changed:
            write_atomic(target, content)
            corrections.append(target.relative_to(PROJECT_ROOT))

    if corrections:
        print("夜凛插件策略已纠正以下配置：")
        for corrected in corrections:
            print(f"- {corrected.as_posix()}")
    else:
        print("夜凛插件策略已满足，无需修改。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as exc:
        print(f"夜凛插件策略执行失败：{exc}", file=sys.stderr)
        sys.exit(1)
