from __future__ import annotations

import re


_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(?:菜单|帮助|指令)(?!(?:\s*\d+\s*)$)(?:\s*.+)?$",
        r"^用量$",
        r"^表情管理(?:\s*\S[\s\S]*)?$",
        r"^通知清理文件$",
        r"^(?:来点)?(?:[美色涩蛇]图|混合).*$",
        r"^[开关](?:群色图|图片显示)$",
        r"^jm\s*\d+$",
        r"^.*(?:factorio|异星|太空时代|space\s*age|spaceage).*(?:下载|安装包).*(?:链接|地址)?$",
        r"^(?:棉花糖|棉花)\s*生图[\s\S]*$",
        r"^(?:(?:查|查询|查看|看)?(?:一下)?(?:我(?:的)?|当前)?(?:生图)?积分(?:余额|情况|多少)?|积分排行(?:榜)?|(?:生图|画图|棉花糖生图|棉花生图)(?:模型说明|模型|价格)|切换\s*生图\s*模型[\s\S]*|生图\s*模型\s+\S+[\s\S]*)$",
        r"^(?:[养摸抓捕][鲲鱼]|属性|洗练.+\d+|挑战|(?:查看)?boss(?:属性)?|等级排行(?:榜)?|(?:财富|萌泪币|金钱)排行(?:榜)?|道具|背包|命名.*|商城|(?:购买|买|出售|卖)(?:改名卡|洗练卡|挑战券|查看卡)\d*|签到|设置重置时间\s*\d+|[开关]新赛季提示|(?:更改|修改)(?:萌泪币|等级)\d+|赠送全部\s*\d+|(?:查看|进击)\s*@.+|赠送\s*@.+\d+)$",
        r"^(?:落樱之都|更新日志|玩法|注册.+|改名.+|个人信息|加经验[0-9]+|嘤[0-9]+|恢复|回复|加[0-9]+(?:力量|智力|体质|敏捷|魅力))$",
    )
)
_LEADING_ASCII_COMMAND = re.compile(r"^[a-zA-Z]{1,16}\b")


def looks_like_command(text: str) -> bool:
    """Conservatively keep fixed commands out of passive repeat state."""

    stripped = str(text or "").strip()
    if not stripped:
        return False
    if stripped.startswith(("/", "!", "！", ".", "。")):
        return True
    if _LEADING_ASCII_COMMAND.match(stripped):
        return True
    return any(pattern.fullmatch(stripped) for pattern in _COMMAND_PATTERNS)
