#!/usr/bin/env python3
"""启动前校验蓝铃本机插件配置，不改写独立实例的启用选择。"""

from pathlib import Path

import sys
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """校验所有现有插件配置的 TOML 及启用字段；无效时阻止启动。"""
    targets = sorted((PROJECT_ROOT / "plugins").glob("*/config.toml"))
    management = PROJECT_ROOT / "src/plugins/built_in/plugin_management/config.toml"
    if management.exists():
        targets.append(management)
    for path in targets:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            # 不输出解析器原文，配置可能包含本机凭据。
            raise RuntimeError(f"无法解析插件配置 {path}: {type(exc).__name__}") from None
        plugin = data.get("plugin")
        if not isinstance(plugin, dict) or type(plugin.get("enabled")) is not bool:
            raise RuntimeError(f"插件配置 {path} 必须明确设置布尔值 plugin.enabled")
    print(f"蓝铃独立插件配置校验通过，共 {len(targets)} 份；启用状态按本机配置保留。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"蓝铃插件配置校验失败：{exc}", file=sys.stderr)
        sys.exit(1)
