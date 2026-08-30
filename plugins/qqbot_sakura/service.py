from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re

from .storage import RuntimeJsonStore, infer_runtime_root_from_path, read_json_file


@dataclass(slots=True)
class SakuraPlayer:
    qq: int
    name: str
    level: int = 1
    exp: int = 0
    max_exp: int = 100
    hp: int = 100
    max_hp: int = 100
    mp: int = 100
    max_mp: int = 100
    money: int = 0
    phy_atk: int = 20
    mag_atk: int = 20
    phy_def: int = 5
    mag_def: int = 5
    speed: int = 100
    points: int = 0
    strength: int = 0
    intelligence: int = 0
    constitution: int = 0
    agility: int = 0
    charm: int = 0


class SakuraService:
    def __init__(self, file_path: Path) -> None:
        self.file_path = Path(file_path)
        self.runtime_root = infer_runtime_root_from_path(self.file_path)
        self.legacy_players_path = self.runtime_root / "data" / "sakura" / "players.json"
        self.store = RuntimeJsonStore(self.runtime_root)
        self.players = self._load()

    def register_player(self, qq: int, name: str) -> SakuraPlayer:
        player = SakuraPlayer(qq=qq, name=name)
        self.players[str(qq)] = player
        self._save()
        return player

    def get_player(self, qq: int) -> SakuraPlayer | None:
        return self.players.get(str(qq))

    def rename_player(self, player: SakuraPlayer, new_name: str) -> str:
        player.name = new_name
        self._save()
        return f"已更改昵称为{new_name}"

    def add_exp(self, player: SakuraPlayer, amount: int) -> str:
        player.exp += amount
        while player.exp >= player.max_exp:
            player.exp -= player.max_exp
            player.level += 1
            player.max_exp += 100
            player.max_hp = player.hp = player.level * 100
            player.max_mp = player.mp = player.level * 100
            player.points += 5
        self._save()
        return f"获得经验{amount}"

    def add_money(self, player: SakuraPlayer, amount: int) -> str:
        player.money += amount
        self._save()
        return f"获得樱币{amount}"

    def add_points(self, player: SakuraPlayer, point_type: str, amount: int) -> str:
        if player.points < amount:
            return "剩余可分配点数不足"
        mapping = {
            "力量": "strength",
            "智力": "intelligence",
            "体质": "constitution",
            "敏捷": "agility",
            "魅力": "charm",
        }
        field = mapping[point_type]
        setattr(player, field, getattr(player, field) + amount)
        player.points -= amount
        self._save()
        return f"已为{point_type}加点{amount}"

    def reset_player(self, player: SakuraPlayer) -> str:
        player.hp = player.max_hp
        player.mp = player.max_mp
        self._save()
        return "状态已恢复"

    @staticmethod
    def build_profile_summary(player: SakuraPlayer) -> str:
        return (
            f"Lv.{player.level} {player.name}\n"
            f"生命：{player.hp}/{player.max_hp}\n"
            f"魔力：{player.mp}/{player.max_mp}\n"
            f"经验：{player.exp}/{player.max_exp}\n"
            f"樱币：{player.money}"
        )

    def handle_command(self, text: str, user_id: int) -> str | None:
        text = text.strip()
        player = self.get_player(user_id)
        if text == "落樱之都":
            return (
                "-===🌸落樱之都🌸===-\n"
                "个人信息◇人物加点\n"
                "我的背包◇我的任务\n"
                "装备强化◇落樱商城\n"
                "单人副本◇魔塔挑战\n"
                "多人副本◇竞技战斗\n"
                "注册xxx / 改名xxx / 个人信息 / 加点"
            )
        if text == "更新日志":
            return "目前只是做了个框架，需要继续迁移副本、商城、排行等内容。"
        if text == "玩法":
            return "当前已迁移角色注册、改名、个人信息、经验、樱币、加点、恢复等基础玩法。"
        if text.startswith("注册"):
            name = text[2:].strip()
            if not name:
                return "要有名字哦！"
            if player:
                return "已有角色，无法创建！"
            player = self.register_player(user_id, name[:10])
            return f"已创建角色【{player.name}】！"
        if player is None:
            is_player_command = (
                text.startswith("改名")
                or text == "个人信息"
                or bool(re.fullmatch(r"加经验[0-9]+|嘤[0-9]+|恢复|回复", text))
                or bool(re.fullmatch(r"加[0-9]+(?:力量|智力|体质|敏捷|魅力)", text))
            )
            return "请先发送“注册角色名”创建角色。" if is_player_command else None
        if text.startswith("改名"):
            return self.rename_player(player, text[2:].strip()[:10])
        if text == "个人信息":
            return self.build_profile_summary(player)
        if match := re.fullmatch(r"加经验([0-9]+)", text):
            return self.add_exp(player, int(match.group(1)))
        if match := re.fullmatch(r"嘤([0-9]+)", text):
            return self.add_money(player, int(match.group(1)))
        if text in {"恢复", "回复"}:
            return self.reset_player(player)
        if match := re.fullmatch(r"加([0-9]+)(力量|智力|体质|敏捷|魅力)", text):
            return self.add_points(player, match.group(2), int(match.group(1)))
        return None

    def _load(self) -> dict[str, SakuraPlayer]:
        raw = self.store.read_with_legacy(
            "sakura.players",
            {},
            self._load_legacy_players,
        )
        if not isinstance(raw, dict):
            raise ValueError("落樱之都玩家状态必须是对象")
        return {str(key): SakuraPlayer(**value) for key, value in raw.items()}

    def _save(self) -> None:
        self.store.write(
            "sakura.players",
            {key: asdict(value) for key, value in self.players.items()},
        )

    def _load_legacy_players(self) -> dict[str, object] | None:
        for path in (self.file_path, self.legacy_players_path):
            if path.exists():
                return read_json_file(path, {})
        return None
