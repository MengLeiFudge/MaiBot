from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemeCommand:
    action: str
    argument: str = ""
    primary_text: str = ""
    admin_only: bool = False


COMMAND_ALIASES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("start_webui", ("开启管理后台", "打开管理后台", "启动管理后台"), True),
    ("stop_webui", ("关闭管理后台", "停止管理后台"), True),
    ("list_emotions", ("查看图库", "图库", "查看表情"), False),
    ("upload_meme", ("添加表情", "上传表情"), True),
    ("restore_default_memes", ("恢复默认表情包", "恢复默认表情"), True),
    ("clear_category", ("清空指定类型", "清空类型"), True),
    ("clear_all", ("清空全部", "清空全部表情"), True),
    ("delete_category", ("删除类型本身", "删除类型"), True),
    ("sync_status", ("同步状态",), False),
    ("sync_to_remote", ("同步到云端",), True),
    ("library_stats", ("图库统计", "表情统计"), False),
    ("sync_from_remote", ("从云端同步",), True),
    ("overwrite_to_remote", ("覆盖到云端",), True),
    ("overwrite_from_remote", ("从云端覆盖",), True),
)
COMMAND_PATTERN = r"^(?:@\S+\s*)?表情管理(?:\s*\S[\s\S]*)?$"


def parse_command(text: str) -> MemeCommand | None:
    normalized = re.sub(r"^@\S+\s*", "", str(text or "").strip(), count=1)
    match = re.fullmatch(r"表情管理(?:\s*(\S[\s\S]*))?", normalized)
    if match is None:
        return None
    rest = re.sub(r"\s+", " ", str(match.group(1) or "").strip())
    if not rest:
        return MemeCommand("list_emotions", primary_text="查看图库")
    compact = re.sub(r"\s+", "", rest)
    for action, aliases, admin_only in COMMAND_ALIASES:
        for alias in aliases:
            if compact == re.sub(r"\s+", "", alias):
                return MemeCommand(action, "", aliases[0], admin_only)
    prefix_matches = sorted(
        (
            (re.sub(r"\s+", "", alias), action, aliases[0], admin_only, alias)
            for action, aliases, admin_only in COMMAND_ALIASES
            for alias in aliases
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for compact_alias, action, primary, admin_only, alias in prefix_matches:
        if compact.startswith(compact_alias):
            # Preserve spaces in a category name while accepting compact command aliases.
            argument = rest[len(alias) :].strip()
            return MemeCommand(action, argument, primary, admin_only)
    return None
