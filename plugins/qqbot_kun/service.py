from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
from pathlib import Path
import random
import re

from .storage import RuntimeJsonStore
from .storage import infer_runtime_root_from_path
from .storage import read_json_file


LEGACY_USER_KEYS = {
    "openNewSeasonTip": "open_new_season_tip",
    "def": "defense",
    "allSignInTimes": "all_sign_in_times",
    "weekSignInTimes": "week_sign_in_times",
    "lastSignInDate": "last_sign_in_date",
    "gmk": "rename_card",
    "xlk": "wash_card",
    "ckk": "check_card",
    "tzq": "challenge_ticket",
    "resetTime": "reset_time",
    "lastSetTime": "last_set_time",
    "mkTime": "mk_time",
    "mkTimes": "mk_times",
    "jjTime": "jj_time",
    "jjTimes": "jj_times",
    "tzTime": "tz_time",
    "tzTimes": "tz_times",
}

LEGACY_BOSS_KEYS = {"def": "defense"}

ADMIN_EDITABLE_USER_FIELDS = (
    "name",
    "level",
    "atk",
    "defense",
    "hp",
    "money",
    "rename_card",
    "wash_card",
    "check_card",
    "challenge_ticket",
)


@dataclass(slots=True)
class KunUser:
    qq: int
    season: int = 1
    open_new_season_tip: bool = False
    name: str = ""
    level: int = 100
    atk: int = 1
    defense: int = 1
    hp: int = 1
    all_sign_in_times: int = 0
    week_sign_in_times: int = 0
    last_sign_in_date: str = ""
    favorite: list[int] = field(default_factory=lambda: [0] * 7)
    money: int = 0
    rename_card: int = 0
    wash_card: int = 0
    check_card: int = 0
    challenge_ticket: int = 0
    reset_time: int = 0
    last_set_time: int = 0
    mk_time: int = 0
    mk_times: int = 0
    jj_time: int = 0
    jj_times: int = 0
    tz_time: int = 0
    tz_times: int = 0


@dataclass(slots=True)
class KunBoss:
    name: str
    level: int
    atk: int
    defense: int
    hp: int


class KunService:
    def __init__(self, file_path: Path) -> None:
        self.file_path = Path(file_path)
        self.kun_root = self.file_path.parent
        self.boss_file_path = self.kun_root / "boss.json"
        self.now_season_path = self.kun_root / "nowSeason.txt"
        self.runtime_root = infer_runtime_root_from_path(self.file_path)
        self.legacy_kun_root = self.runtime_root / "data" / "kun"
        self.store = RuntimeJsonStore(self.runtime_root)
        self.now_season = self._load_now_season()
        self.users = self._load_users()
        self.boss = self._load_boss()

    def get_user(self, qq: int) -> KunUser | None:
        user = self.users.get(str(qq))
        if user is None:
            return None
        changed = self._season_settlement(user)
        if changed:
            self._save()
        return user

    def ensure_user(self, qq: int) -> KunUser:
        user = self.users.get(str(qq))
        if user is None:
            user = KunUser(
                qq=qq,
                season=self.now_season,
                name=self._random_chinese_name(1, 4),
                level=self._random_distribution_int(100, 2000),
            )
            self.users[str(qq)] = user
            self._save()
            return user
        changed = self._season_settlement(user)
        if changed:
            self._save()
        return user

    def mk(self, qq: int, now_millis: int, is_admin: bool) -> str:
        user = self.get_user(qq)
        if user is None:
            user = self.ensure_user(qq)
            return (
                "你孤身一人去无尽之海，发现一只小鲲崽遗弃在海岸边。\n"
                f"你看它非常可爱，便给它取名为{user.name}，决定抚养它成为最强的鲲王。\n"
                "只是这无尽之海非常广阔，你还需要抓更多的鲲喂给它，使其慢慢成长...."
            )

        target_time = self._mk_target_time(user, now_millis)
        if not is_admin and target_time != now_millis:
            return (
                f"下次摸鲲时间为{self._time_str(target_time)}，"
                f"还需等待{self._milli_second_to_str(now_millis, target_time, True)}~"
            )

        level = user.level
        name = user.name
        money = user.money
        event = self._random_int(0, 99)
        if event < 10:
            message = f"你去无尽之海，结果什么都没摸到！\n只好带着{name}回去了。"
        elif event < 20:
            sub_money = int(self._random_double(money * 0.005, money * 0.015))
            sub_money = min(min(sub_money + 1, level // 200), user.money)
            user.money = money - sub_money
            message = (
                "你去无尽之海，结果什么都没摸到！\n"
                f"一摸背包，发现萌泪币少了{sub_money}！\n"
                f"你很生气，却不知是谁偷了你的钱，只好带着{name}回去了。"
            )
        elif event < 30:
            sub_level = self._random_distribution_int(20, 50 + level // 500)
            user.level = level - sub_level
            message = (
                "你去无尽之海，忽然一只巨鲲向你咬来！\n"
                f"这时，{name}挺身而出，替你挡下了这一击，自己却受伤，等级降低了{sub_level}！\n"
                f"你立刻离开无尽之海，带着{name}回去休养了。"
            )
        else:
            add_level = (
                self._random_distribution_int(100, 250 + level // 100)
                if event < 36
                else self._random_distribution_int(20, 50 + level // 500)
            )
            add_money = (
                self._random_distribution_int(add_level * 10, add_level * 30)
                if 33 < event < 40
                else self._random_distribution_int(add_level * 2, add_level * 6)
            )
            user.level = level + add_level
            user.money = money + add_money
            if event < 34:
                message = (
                    "你去无尽之海，抓了很多很多的鲲！\n"
                    f"你将大部分鲲喂给{name}，等级提高{add_level}！\n"
                    f"剩余的鲲都被你卖掉，获得{add_money}枚萌泪币！"
                )
            elif event < 36:
                message = (
                    "你去无尽之海，不仅抓了很多鲲，还捞上来一个大宝箱！\n"
                    f"你将鲲喂给{name}，等级提高{add_level}！\n"
                    f"你又打开宝箱，发现里面竟然有{add_money}枚萌泪币！"
                )
            elif event < 40:
                message = (
                    "你去无尽之海，抓了一些鲲，还捞上来一个小宝箱！\n"
                    f"你将鲲喂给{name}，等级提高{add_level}！\n"
                    f"你又打开宝箱，发现里面竟然有{add_money}枚萌泪币！"
                )
            else:
                message = (
                    "你去无尽之海，抓了一些鲲！\n"
                    f"你将大部分鲲喂给{name}，等级提高{add_level}！\n"
                    f"剩余的鲲都被你卖掉，获得{add_money}枚萌泪币！"
                )

        if user.level > 120000:
            self._start_new_season()
            self._season_settlement(user)
        self._save()
        return message

    def build_attribute_summary(self, user: KunUser, rank_index: int, total_users: int) -> str:
        level = user.level
        atk_grade = self._grade(user.atk, level, 1, 1.5)
        def_grade = self._grade(user.defense, level, 0.6, 0.9)
        hp_grade = self._grade(user.hp, level, 4, 6)
        lines = [
            f"{user.name}正在到处游弋。",
            f"等级：{level}",
            f"血量：{user.hp}（{hp_grade}）",
            f"攻击：{user.atk}（{atk_grade}）",
            f"防御：{user.defense}（{def_grade}）",
            f"当前排名：{rank_index} / {total_users}",
        ]
        if total_users > 0:
            if rank_index == 1:
                lines.append("超过了所有人！太强了鸭！")
            elif rank_index == total_users:
                lines.append("谁都没有超过！太惨了鸭！")
            else:
                percent = f"{(total_users - rank_index) * 100.0 / total_users:.2f}"
                lines.append(f"超过了{percent}%的人！")
        lines.append(f"新赛季私聊提示：{'开启' if user.open_new_season_tip else '关闭'}")
        return "\n".join(lines)

    def build_other_attribute_summary(self, viewer: KunUser, target: KunUser) -> str:
        if viewer.check_card <= 0:
            return "TA的鲲似乎被一层迷雾笼罩....\n指令提示：【购买查看卡】"
        viewer.check_card -= 1
        self._save()
        level = target.level
        atk_grade = self._grade(target.atk, level, 1, 1.5)
        def_grade = self._grade(target.defense, level, 0.6, 0.9)
        hp_grade = self._grade(target.hp, level, 4, 6)
        return (
            "查看到对方信息如下：\n"
            f"{target.name}\n"
            f"等级：{level}\n"
            f"血量：{target.hp}（{hp_grade}）\n"
            f"攻击：{target.atk}（{atk_grade}）\n"
            f"防御：{target.defense}（{def_grade}）"
        )

    def build_bag_summary(self, user: KunUser) -> str:
        return (
            f"改名卡：{user.rename_card}张\n"
            f"洗练卡：{user.wash_card}张\n"
            f"挑战券：{user.challenge_ticket}张\n"
            f"查看卡：{user.check_card}张\n"
            f"萌泪币：{user.money}枚"
        )

    def washout(self, user: KunUser, wash_attack: bool, wash_defense: bool, wash_hp: bool, count: int) -> str:
        if not wash_attack and not wash_defense and not wash_hp:
            self._save()
            return f"{user.name}一脸懵逼的看着你，不知道你要干什么。\n指令提示：攻防血至少洗练一项"

        use_count = count * int(wash_attack) + count * int(wash_defense) + count * int(wash_hp)
        if user.wash_card < use_count:
            self._save()
            return f"{user.name}也想洗练，但是你好像没有足够的洗练卡了QAQ\n指令提示：该指令至少需要{use_count}张洗练卡"

        user.wash_card -= use_count
        lines = [f"{user.name}正在洗练...."]
        if wash_attack:
            max_num = max(self._random_distribution_double(user.level * 1.0, user.level * 1.5) for _ in range(count))
            user.atk = int(max_num)
            lines.append(f"洗练攻击{count}次，最高{user.atk}")
        if wash_defense:
            max_num = max(self._random_distribution_double(user.level * 0.6, user.level * 0.9) for _ in range(count))
            user.defense = int(max_num)
            lines.append(f"洗练防御{count}次，最高{user.defense}")
        if wash_hp:
            max_num = max(self._random_distribution_double(user.level * 4.0, user.level * 6.0) for _ in range(count))
            user.hp = int(max_num)
            lines.append(f"洗练血量{count}次，最高{user.hp}")
        self._save()
        return "\n".join(lines)

    def sign_in(self, user: KunUser, today: str, this_monday: str) -> str:
        last_sign_in_date = user.last_sign_in_date
        all_times = user.all_sign_in_times
        week_times = user.week_sign_in_times
        if last_sign_in_date == today:
            return f"共签到{all_times}次\n本周已签到{week_times}天\n明天再来签到吧！"

        user.last_sign_in_date = today
        user.all_sign_in_times = all_times + 1
        if this_monday > last_sign_in_date:
            week_times = 1
        else:
            week_times += 1
        user.week_sign_in_times = week_times
        reward = {1: 666, 2: 999, 3: 1314, 4: 1888, 5: 2888, 6: 3888, 7: 6666}.get(week_times, 0)
        user.money += reward
        self._save()
        return f"共签到{user.all_sign_in_times}次\n本周已签到{week_times}天\n获得{reward}枚萌泪币！"

    def rename_kun(self, user: KunUser, new_name: str) -> str:
        old_name = user.name
        new_name = re.sub(r"\s*", "", new_name)
        if len(new_name) > 8:
            return f"{old_name}表示这个名字实在是太长了，它根本记不住！\n指令提示：请使用8字符以内的名字"
        if not new_name:
            return "指令提示：命名xx"
        if user.rename_card <= 0:
            return f"{old_name}摇了摇头，表示不喜欢这个名字！\n指令提示：【购买改名卡】"
        user.rename_card -= 1
        user.name = new_name
        self._save()
        return f"{old_name}高兴地绕着你转了两圈，看来它很喜欢这个名字！\n以后它就叫{new_name}啦！"

    def buy_item(self, user: KunUser, item_name: str, count: int) -> str:
        if count <= 0:
            return "购买数量有误！"

        config = {
            "改名卡": ("rename_card", 18888, 1),
            "洗练卡": ("wash_card", 30, 9999),
            "挑战券": ("challenge_ticket", 100, 99),
            "查看卡": ("check_card", 100, 99),
        }
        if item_name not in config:
            return f"[{item_name}]是神马东西？可以次吗？"

        field, price, limit = config[item_name]
        item_num = getattr(user, field)
        if item_num + count > limit:
            return f"{item_name}上限为{limit}！"
        if user.money < price * count:
            return f"萌泪币：{user.money}\n不足{price * count}，无法购买！"

        setattr(user, field, item_num + count)
        user.money -= price * count
        self._save()
        return f"成功购买{item_name}×{count}！\n花费{price * count}枚萌泪币！\n现有萌泪币：{user.money}枚"

    def sell_item(self, user: KunUser, item_name: str, count: int) -> str:
        if count <= 0:
            return "出售数量有误！"

        config = {
            "改名卡": ("rename_card", 15110),
            "洗练卡": ("wash_card", 24),
            "查看卡": ("check_card", 80),
        }
        if item_name not in config:
            return f"[{item_name}]是神马东西？可以次吗？"

        field, price = config[item_name]
        item_num = getattr(user, field)
        sold_num = min(item_num, count)
        setattr(user, field, item_num - sold_num)
        user.money += price * sold_num
        self._save()
        return f"成功出售{item_name}×{sold_num}！\n获得{price * sold_num}枚萌泪币！\n现有萌泪币：{user.money}枚"

    def get_level_rank(self) -> list[KunUser]:
        self._settle_all_users()
        return sorted(self.users.values(), key=lambda item: item.level, reverse=True)

    def get_money_rank(self) -> list[KunUser]:
        self._settle_all_users()
        return sorted(self.users.values(), key=lambda item: item.money, reverse=True)

    def build_level_rank_lines(
        self,
        group_id: int = 0,
        resolve_display_name: Callable[[int, int], str] | None = None,
    ) -> list[str]:
        ranks = self.get_level_rank()
        if not ranks:
            return ["当前无人上榜！\n（摸鲲、进击均可上榜）"]
        lines = []
        for index, item in enumerate(ranks[:10], start=1):
            display_name = self._resolve_rank_display_name(group_id, item.qq, resolve_display_name)
            lines.append(f"{index} Lv.{item.level} {item.name}\n{display_name}")
        return ["\n".join(lines)]

    def build_money_rank_lines(
        self,
        group_id: int = 0,
        resolve_display_name: Callable[[int, int], str] | None = None,
    ) -> list[str]:
        ranks = self.get_money_rank()
        if not ranks:
            return ["当前无人上榜！\n（摸鲲、进击均可上榜）"]
        lines = []
        for index, item in enumerate(ranks[:10], start=1):
            display_name = self._resolve_rank_display_name(group_id, item.qq, resolve_display_name)
            lines.append(f"{index} {item.money} 枚萌泪币\n{display_name}")
        return ["\n".join(lines)]

    @staticmethod
    def _resolve_rank_display_name(
        group_id: int,
        qq: int,
        resolver: Callable[[int, int], str] | None,
    ) -> str:
        if resolver is None:
            return str(qq)
        try:
            display_name = resolver(group_id, qq)
        except Exception:
            return str(qq)
        return display_name.strip() if isinstance(display_name, str) and display_name.strip() else str(qq)

    def give_money(self, giver: KunUser, receiver: KunUser, amount: int, is_admin: bool) -> str:
        my_money = giver.money
        if amount <= 0:
            amount = min(-amount, my_money)
        if amount > my_money:
            return "钱不够！\n你在想peach？"

        giver.money -= amount
        fee = 0 if is_admin else int(amount * self._random_double(0.05, 0.15))
        receiver.money += amount - fee
        self._save()
        return f"收取手续费{fee}枚萌泪币，已赠送{amount - fee}枚萌泪币！"

    def set_reset_time(self, user: KunUser, reset_time: int, now_millis: int) -> str:
        if user.reset_time == reset_time:
            return f"当前重置时间是{reset_time}时，无需更改！"
        if now_millis - user.last_set_time < 604800000 and user.last_set_time != 0:
            return f"距上一次设置重置时间不足一周！\n请于{self._full_time_str(user.last_set_time + 604800000)}后再试！"
        user.reset_time = reset_time
        user.last_set_time = now_millis
        self._save()
        return f"当前重置时间已更改为{reset_time}时！"

    def set_new_season_tip(self, user: KunUser, open_tip: bool) -> str:
        user.open_new_season_tip = open_tip
        self._save()
        return f"已{'打开' if open_tip else '关闭'}赛季提示！"

    def attack_other(self, user: KunUser, other: KunUser, now_millis: int, is_admin: bool) -> str:
        if user.qq == other.qq:
            return f"{user.name}表示不想打自己，并向你丢了一个白眼！"
        if other.level * 1.5 < user.level:
            return f"{user.name}实在是太强了，{other.name}一看这气势，早就远远跑开了！"

        target_time = self._jj_target_time(user, now_millis)
        if not is_admin and target_time != now_millis:
            return (
                f"下次进击时间为{self._time_str(target_time)}，"
                f"还需等待{self._milli_second_to_str(now_millis, target_time, True)}~"
            )

        num1 = self._random_int(100, 110)
        num2 = self._random_int(90, 100)
        my_atk = user.atk * num1 // 100
        at_atk = other.atk * num2 // 100
        my_def = user.defense * num1 // 100
        at_def = other.defense * num2 // 100
        my_hp = user.hp * num1 // 100
        at_hp = other.hp * num2 // 100
        my_atk_times = 0
        at_atk_times = 0
        my_all_dam = 0
        at_all_dam = 0
        result_lines = [f"{user.name} VS {other.name}"]

        while True:
            my_dam = max(my_atk - at_def, 1)
            my_atk_times += 1
            my_all_dam += my_dam
            if at_hp <= my_all_dam:
                result_lines.append(f"{user.name}攻击{my_atk_times}次")
                result_lines.append(f"共造成伤害{my_all_dam}点")
                if my_atk_times > 1:
                    result_lines.append(f"{other.name}反击{at_atk_times}次")
                    result_lines.append(f"共造成伤害{at_all_dam}点")
                result_lines.append("获胜啦！")
                is_win = True
                break
            at_dam = max(at_atk - my_def, 1)
            at_atk_times += 1
            at_all_dam += at_dam
            if my_hp <= at_all_dam:
                result_lines.append(f"{user.name}攻击{my_atk_times}次")
                result_lines.append(f"共造成伤害{my_all_dam}点")
                result_lines.append(f"{other.name}反击{at_atk_times}次")
                result_lines.append(f"共造成伤害{at_all_dam}点")
                result_lines.append("失败了！")
                is_win = False
                break

        my_add = self._get_add(user.level) if is_win else int(self._get_add(user.level) * 0.7)
        at_add = int(self._get_add(other.level) * 0.25)
        user.level += my_add
        other.level += at_add
        result_lines.append("经过磨炼，")
        result_lines.append(f"{user.name}等级增加{my_add}！")
        result_lines.append(f"{other.name}等级增加{at_add}！")
        if user.level > 120000 or other.level > 120000:
            self._start_new_season()
            self._season_settlement(user)
            self._season_settlement(other)
        self._save()
        return "\n".join(result_lines)

    def build_boss_summary(self, user: KunUser) -> str:
        if user.check_card <= 0:
            return "你没有查看卡，无权查看对方信息！"
        user.check_card -= 1
        if self.boss.hp <= 0:
            self.boss = self._new_boss()
        self._save()
        self._save_boss()
        return f"Boss {self.boss.name}\n攻击：{self.boss.atk}\n防御：{self.boss.defense}\n剩余血量：{self.boss.hp}"

    def challenge_boss(self, user: KunUser, now_millis: int, is_admin: bool) -> str:
        if user.challenge_ticket <= 0:
            return "你还没挑战券呢！"

        target_time = self._tz_target_time(user, now_millis)
        if not is_admin and target_time != now_millis:
            return (
                f"下次挑战时间为{self._time_str(target_time)}，"
                f"还需等待{self._milli_second_to_str(now_millis, target_time, True)}~"
            )

        user.challenge_ticket -= 1
        if self.boss.hp <= 0:
            self.boss = self._new_boss()
        boss_hp = self.boss.hp
        boss_atk = self.boss.atk
        boss_def = self.boss.defense
        my_atk = user.atk
        my_def = user.defense
        my_hp = user.hp
        my_atk_times = 0
        boss_atk_times = 0
        my_all_dam = 0
        boss_all_dam = 0

        if self._random_double(4, 14) < 7:
            attribute_increase = self._random_distribution_double(4, 10)
            if attribute_increase > 7:
                attribute_increase = 14 - attribute_increase
        else:
            attribute_increase = self._random_distribution_double(0, 14)
            if attribute_increase < 7:
                attribute_increase = 14 - attribute_increase

        my_atk = int(my_atk * attribute_increase)
        my_def = int(my_def * attribute_increase * 0.1)
        my_hp = int(my_hp * attribute_increase * 0.5)
        lines = [
            f"{user.name} VS {self.boss.name}",
            f"随机加成倍数：{attribute_increase:.2f}",
        ]

        while True:
            my_dam = max(my_atk - boss_def, 1)
            my_atk_times += 1
            my_all_dam += my_dam
            if boss_hp <= my_all_dam:
                lines.append(f"{user.name}攻击{my_atk_times}次")
                lines.append(f"共造成伤害{my_all_dam}点")
                if my_atk_times > 1:
                    lines.append(f"{self.boss.name}反击{boss_atk_times}次")
                    lines.append(f"共造成伤害{boss_all_dam}点")
                lines.append("获胜啦！")
                is_win = True
                break
            at_dam = max(boss_atk - my_def, 1)
            boss_atk_times += 1
            boss_all_dam += at_dam
            if my_hp <= boss_all_dam:
                lines.append(f"{user.name}攻击{my_atk_times}次")
                lines.append(f"共造成伤害{my_all_dam}点")
                lines.append(f"{self.boss.name}反击{boss_atk_times}次")
                lines.append(f"共造成伤害{boss_all_dam}点")
                lines.append("失败了！")
                is_win = False
                break

        self.boss.hp = boss_hp - my_all_dam
        get_money = int(my_all_dam * 2 / self.boss.level + (10000 if is_win else 1000))
        user.money += get_money
        self._save()
        self._save_boss()
        lines.append(f"获得了{get_money}枚萌泪币！")
        return "\n".join(lines)

    def migrate_legacy_data(self, legacy_root: Path) -> int:
        legacy_root = Path(legacy_root)
        imported = 0
        for user_file in sorted((legacy_root / "user").glob("*.json")):
            payload = json.loads(user_file.read_text(encoding="utf-8"))
            user = self._legacy_user_to_record(payload)
            self.users[str(user.qq)] = user
            imported += 1

        if (legacy_root / "boss.json").exists():
            boss_payload = json.loads((legacy_root / "boss.json").read_text(encoding="utf-8"))
            self.boss = self._payload_to_boss(boss_payload)
            self._save_boss()

        if (legacy_root / "nowSeason.txt").exists():
            now_season_text = (legacy_root / "nowSeason.txt").read_text(encoding="utf-8")
            self.now_season = self._parse_now_season_text(now_season_text)
            self._save_now_season()
        else:
            self._save_now_season()

        self._save()
        return imported

    def handle_command(
        self,
        text: str,
        user_id: int,
        now_millis: int,
        *,
        is_group: bool,
        at_id: int | None,
        is_admin: bool,
        group_id: int = 0,
        resolve_display_name: Callable[[int, int], str] | None = None,
    ) -> str | None:
        text = text.strip()
        if at_id is None:
            if re.fullmatch(r"[养摸抓捕][鲲鱼]", text):
                if is_group:
                    return "养鲲要私聊我哦！"
                return self.mk(user_id, now_millis=now_millis, is_admin=is_admin)
            if text == "属性":
                if is_group:
                    return None
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                ranks = self.get_level_rank()
                rank_index = next(index + 1 for index, item in enumerate(ranks) if item.qq == user.qq)
                return self.build_attribute_summary(user, rank_index, len(ranks))
            if re.fullmatch(r"洗练.+[0-9]+", text):
                if is_group:
                    return None
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                wash = [("攻" in text), ("防" in text), ("血" in text)]
                count = int(re.split(r"\D+", text)[1])
                return self.washout(user, wash[0], wash[1], wash[2], count)
            if text == "挑战":
                if is_group:
                    return None
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                return self.challenge_boss(user, now_millis=now_millis, is_admin=is_admin)
            if re.fullmatch(r"(查看|)[Bb]oss(属性|)", text):
                if is_group:
                    return None
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                return self.build_boss_summary(user)
            if re.fullmatch(r"等级排行(榜|)", text):
                return "\n\n".join(
                    self.build_level_rank_lines(
                        group_id=group_id,
                        resolve_display_name=resolve_display_name,
                    )
                )
            if re.fullmatch(r"(财富|萌泪币|金钱)排行(榜|)", text):
                return "\n\n".join(
                    self.build_money_rank_lines(
                        group_id=group_id,
                        resolve_display_name=resolve_display_name,
                    )
                )
            if text in {"道具", "背包"}:
                if is_group:
                    return None
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                return self.build_bag_summary(user)
            if text.startswith("命名"):
                if is_group:
                    return None
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                return self.rename_kun(user, text[2:])
            if text == "商城":
                if is_group:
                    return None
                return "———— 商城 ————\n 道具    购买    出售\n改名卡：18888 / 15110\n洗练卡：     30 /    24\n挑战券：   100 /     -\n查看卡：   100 /    80\n"
            if re.fullmatch(r"(购买|买|出售|卖).+([0-9]+)?", text):
                if is_group:
                    return None
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                buy = text.startswith("购买") or text.startswith("买")
                begin_len = 2 if text.startswith("购买") or text.startswith("出售") else 1
                if re.fullmatch(r".+[0-9]+", text):
                    tail = re.split(r"\D+", text)[-1]
                    count = int(tail)
                    end_len = len(tail)
                else:
                    count = 1
                    end_len = 0
                item_name = text[begin_len : len(text) - end_len].strip()
                return self.buy_item(user, item_name, count) if buy else self.sell_item(user, item_name, count)
            if text == "签到":
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                return self.sign_in(user, self._date_str(now_millis), self._this_monday(now_millis))
            if re.fullmatch(r"设置重置时间 *[0-9]+", text):
                if is_group:
                    return None
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                reset_time = int(re.split(r"\D+", text)[1])
                return self.set_reset_time(user, reset_time, now_millis)
            if re.fullmatch(r"[开关]新赛季提示", text):
                if is_group:
                    return None
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                return self.set_new_season_tip(user, text.startswith("开"))
            if is_admin and re.fullmatch(r"(更改|修改).+[0-9]+", text):
                user = self.get_user(user_id)
                if user is None:
                    return self.no_kun_message()
                value = int(re.split(r"\D+", text)[1])
                key = text[2 : len(text) - len(str(value))].strip()
                return self.change(user, key, value)
            if is_admin and re.fullmatch(r"赠送全部 *[0-9]+", text):
                money = int(re.split(r"\D+", text)[1])
                return self.give_money_to_all(money)
            return None

        if text.startswith("查看"):
            viewer = self.get_user(user_id)
            if viewer is None:
                return self.no_kun_message()
            target = self.get_user(at_id)
            if target is None:
                return "TA好像还没鲲呢！"
            return self.build_other_attribute_summary(viewer, target)
        if text.startswith("进击"):
            user = self.get_user(user_id)
            if user is None:
                return self.no_kun_message()
            target = self.get_user(at_id)
            if target is None:
                return "TA好像还没鲲呢！"
            return self.attack_other(user, target, now_millis=now_millis, is_admin=is_admin)
        if "赠送" in text:
            user = self.get_user(user_id)
            if user is None:
                return self.no_kun_message()
            target = self.get_user(at_id)
            if target is None:
                return "TA好像还没鲲呢！"
            amount_match = re.search(r"([0-9]+)", text)
            if amount_match is None:
                return None
            return self.give_money(user, target, int(amount_match.group(1)), is_admin=is_admin)
        return None

    def no_kun_message(self) -> str:
        return "在无尽之海，也许你会有所发现....\n指令提示：私聊【摸鲲】"

    def change(self, user: KunUser, key: str, new_value: int) -> str:
        if key == "萌泪币":
            user.money = new_value
        elif key == "等级":
            user.level = new_value
        else:
            return f"错误的修改目标 [{key}]"
        self._save()
        return f"已修改{key}为{new_value}！"

    def give_money_to_all(self, money: int) -> str:
        for user in self.users.values():
            user.money += money
        self._save()
        return f"已赠送所有人萌泪币{money}枚！"

    def build_admin_user_snapshot(self, qq: int) -> dict[str, object] | None:
        user = self.get_user(qq)
        if user is None:
            return None
        return {
            "user": self._admin_user_to_payload(user),
            "editable_fields": list(ADMIN_EDITABLE_USER_FIELDS),
        }

    def update_admin_user_fields(
        self,
        qq: int,
        updates: dict[str, object],
    ) -> dict[str, object]:
        user = self.get_user(qq)
        if user is None:
            raise ValueError(f"Kun user not found: {qq}")

        for key, value in updates.items():
            if key not in ADMIN_EDITABLE_USER_FIELDS:
                raise ValueError(f"Unsupported Kun user field: {key}")
            if key == "name":
                user.name = str(value).strip()[:8]
                continue
            setattr(user, key, max(0, int(value)))

        self._save()
        return {
            "user": self._admin_user_to_payload(user),
            "editable_fields": list(ADMIN_EDITABLE_USER_FIELDS),
        }

    # 核心持久化逻辑：内部字段用 Python 命名，落盘时映射回 mirai 旧字段。
    def _payload_to_user(self, payload: dict[str, object]) -> KunUser:
        data: dict[str, object] = {}
        for key, value in payload.items():
            mapped = LEGACY_USER_KEYS.get(key, key)
            data[mapped] = value
        return KunUser(
            qq=int(data["qq"]),
            season=int(data.get("season", self.now_season)),
            open_new_season_tip=bool(data.get("open_new_season_tip", False)),
            name=str(data.get("name", "")),
            level=int(data.get("level", 100)),
            atk=int(data.get("atk", 1)),
            defense=int(data.get("defense", 1)),
            hp=int(data.get("hp", 1)),
            all_sign_in_times=int(data.get("all_sign_in_times", 0)),
            week_sign_in_times=int(data.get("week_sign_in_times", 0)),
            last_sign_in_date=str(data.get("last_sign_in_date", "")),
            favorite=list(data.get("favorite", [0] * 7)),
            money=int(data.get("money", 0)),
            rename_card=int(data.get("rename_card", 0)),
            wash_card=int(data.get("wash_card", 0)),
            check_card=int(data.get("check_card", 0)),
            challenge_ticket=int(data.get("challenge_ticket", 0)),
            reset_time=int(data.get("reset_time", 0)),
            last_set_time=int(data.get("last_set_time", 0)),
            mk_time=int(data.get("mk_time", 0)),
            mk_times=int(data.get("mk_times", 0)),
            jj_time=int(data.get("jj_time", 0)),
            jj_times=int(data.get("jj_times", 0)),
            tz_time=int(data.get("tz_time", 0)),
            tz_times=int(data.get("tz_times", 0)),
        )

    def _legacy_user_to_record(self, payload: dict[str, object]) -> KunUser:
        return self._payload_to_user(payload)

    def _admin_user_to_payload(self, user: KunUser) -> dict[str, object]:
        return {
            "qq": user.qq,
            "season": user.season,
            "open_new_season_tip": user.open_new_season_tip,
            "name": user.name,
            "level": user.level,
            "atk": user.atk,
            "defense": user.defense,
            "hp": user.hp,
            "money": user.money,
            "rename_card": user.rename_card,
            "wash_card": user.wash_card,
            "check_card": user.check_card,
            "challenge_ticket": user.challenge_ticket,
            "reset_time": user.reset_time,
            "last_set_time": user.last_set_time,
            "mk_time": user.mk_time,
            "mk_times": user.mk_times,
            "jj_time": user.jj_time,
            "jj_times": user.jj_times,
            "tz_time": user.tz_time,
            "tz_times": user.tz_times,
        }

    def _user_to_payload(self, user: KunUser) -> dict[str, object]:
        return {
            "qq": user.qq,
            "season": user.season,
            "openNewSeasonTip": user.open_new_season_tip,
            "name": user.name,
            "level": user.level,
            "atk": user.atk,
            "def": user.defense,
            "hp": user.hp,
            "allSignInTimes": user.all_sign_in_times,
            "weekSignInTimes": user.week_sign_in_times,
            "lastSignInDate": user.last_sign_in_date,
            "favorite": list(user.favorite),
            "money": user.money,
            "gmk": user.rename_card,
            "xlk": user.wash_card,
            "ckk": user.check_card,
            "tzq": user.challenge_ticket,
            "resetTime": user.reset_time,
            "lastSetTime": user.last_set_time,
            "mkTime": user.mk_time,
            "mkTimes": user.mk_times,
            "jjTime": user.jj_time,
            "jjTimes": user.jj_times,
            "tzTime": user.tz_time,
            "tzTimes": user.tz_times,
        }

    def _payload_to_boss(self, payload: dict[str, object]) -> KunBoss:
        data = {LEGACY_BOSS_KEYS.get(key, key): value for key, value in payload.items()}
        return KunBoss(
            name=str(data["name"]),
            level=int(data["level"]),
            atk=int(data["atk"]),
            defense=int(data.get("defense", 0)),
            hp=int(data["hp"]),
        )

    def _boss_to_payload(self, boss: KunBoss) -> dict[str, object]:
        return {
            "name": boss.name,
            "level": boss.level,
            "atk": boss.atk,
            "def": boss.defense,
            "hp": boss.hp,
        }

    def _load_users(self) -> dict[str, KunUser]:
        raw = self.store.read_with_legacy(
            "kun.users",
            {},
            lambda: self._load_legacy_json(self.file_path, self.legacy_kun_root / "users.json", {}),
        )
        return {key: self._payload_to_user(value) for key, value in raw.items()}

    def _save(self) -> None:
        payload = {key: self._user_to_payload(value) for key, value in self.users.items()}
        self.store.write("kun.users", payload)

    def _load_boss(self) -> KunBoss:
        payload = self.store.read_with_legacy(
            "kun.boss",
            {},
            lambda: self._load_legacy_json(self.boss_file_path, self.legacy_kun_root / "boss.json", {}),
        )
        if not payload:
            boss = self._new_boss()
            self._save_boss_data(boss)
            return boss
        return self._payload_to_boss(payload)

    def _save_boss(self) -> None:
        self._save_boss_data(self.boss)

    def _save_boss_data(self, boss: KunBoss) -> None:
        self.store.write("kun.boss", self._boss_to_payload(boss))

    def _load_now_season(self) -> int:
        payload = self.store.read_with_legacy(
            "kun.meta",
            {},
            self._load_legacy_now_season_payload,
        )
        value = int(payload.get("now_season", 1) or 1) if isinstance(payload, dict) else 1
        if not payload:
            self.store.write("kun.meta", {"now_season": value})
        return value

    def _load_legacy_now_season_payload(self) -> dict[str, int] | None:
        for path in (self.now_season_path, self.legacy_kun_root / "nowSeason.txt"):
            if path.exists():
                return {"now_season": self._parse_now_season_text(path.read_text(encoding="utf-8"))}
        return None

    def _load_legacy_json(self, primary_path: Path, legacy_path: Path, default: dict[str, object]) -> dict[str, object] | None:
        for path in (primary_path, legacy_path):
            if path.exists():
                return read_json_file(path, default)
        return None

    def _parse_now_season_text(self, text: str) -> int:
        match = re.search(r"1\s*=\s*(\d+)", text)
        if match:
            return int(match.group(1))
        return 1

    def _save_now_season(self) -> None:
        self.store.write("kun.meta", {"now_season": self.now_season})

    def _settle_all_users(self) -> None:
        changed = False
        for user in self.users.values():
            changed = self._season_settlement(user) or changed
        if changed:
            self._save()

    def _season_settlement(self, user: KunUser) -> bool:
        changed = False
        while user.season != self.now_season:
            old_level = user.level
            new_level = 0
            add_money = 0
            for _ in range(user.season, self.now_season):
                new_level = self._get_new_level(old_level)
                add_money += old_level // 3
            user.season = self.now_season
            user.level = new_level
            user.money += add_money
            changed = True

        level = user.level
        atk = max(int(level * 1.0), min(user.atk, int(level * 1.5)))
        defense = max(int(level * 0.6), min(user.defense, int(level * 0.9)))
        hp = max(int(level * 4.0), min(user.hp, int(level * 6.0)))
        if atk != user.atk or defense != user.defense or hp != user.hp:
            user.atk = atk
            user.defense = defense
            user.hp = hp
            changed = True
        return changed

    def _start_new_season(self) -> None:
        self.now_season += 1
        self._save_now_season()
        self.boss = self._new_boss()
        self._save_boss()

    def _new_boss(self) -> KunBoss:
        max_level = max((user.level for user in self.users.values()), default=5000)
        level = max(5000, max_level)
        return KunBoss(
            name=self._random_chinese_name(1, 4),
            level=level,
            atk=int(level * self._random_double(1.6, 2.8)),
            defense=int(level * self._random_double(0.8, 1.4)),
            hp=level * self._random_int(8000, 14000),
        )

    def _grade(self, attribute_value: int, level: int, minimum: float, maximum: float) -> str:
        if attribute_value == int(level * maximum):
            return "MAX"
        if attribute_value == int(level * minimum):
            return "MIN"
        gap = level * (maximum - minimum)
        over_part = attribute_value - level * minimum
        if over_part < gap / 8:
            grade = "E"
        elif over_part < gap * 2 / 8:
            grade = "D"
            over_part -= gap / 8
        elif over_part < gap * 3 / 8:
            grade = "C"
            over_part -= gap * 2 / 8
        elif over_part < gap * 4 / 8:
            grade = "B"
            over_part -= gap * 3 / 8
        elif over_part < gap * 5 / 8:
            grade = "A"
            over_part -= gap * 4 / 8
        elif over_part < gap * 6 / 8:
            grade = "S"
            over_part -= gap * 5 / 8
        elif over_part < gap * 7 / 8:
            grade = "SS"
            over_part -= gap * 6 / 8
        else:
            grade = "SSS"
            over_part -= gap * 7 / 8
        gap /= 8
        if over_part < gap / 3:
            return grade + "-"
        if over_part < gap * 2 / 3:
            return grade
        return grade + "+"

    def _get_add(self, level: int) -> int:
        max_level = max((user.level for user in self.users.values()), default=5000)
        fj1 = max_level * 0.4
        fj2 = max_level * 0.65
        fj3 = max_level * 0.9
        if level < fj1:
            return self._random_distribution_int(400, 800)
        if level < fj2:
            return self._random_distribution_int(200, 400)
        if level < fj3:
            return self._random_distribution_int(100, 200)
        return self._random_distribution_int(50, 100)

    def _get_new_level(self, level: int) -> int:
        new_level = 0
        index = 10
        while index > 1:
            threshold = (10 - index) * (10 - index) * 300 + 3000
            if level <= threshold:
                break
            new_level += threshold * index // 10
            level -= threshold
            index -= 1
        return new_level + level * index // 10

    def _random_int(self, minimum: int, maximum: int) -> int:
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        return random.randint(minimum, maximum)

    def _random_double(self, minimum: float, maximum: float) -> float:
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        return random.random() * (maximum - minimum) + minimum

    def _random_distribution_double(self, minimum: float, maximum: float, coverage: float = 2.32) -> float:
        if coverage <= 0:
            return (maximum + minimum) / 2
        value = random.gauss(0, 1)
        if value > coverage:
            value = coverage
        elif value < -coverage:
            value = -coverage
        value = value / (coverage * 2) + 0.5
        return (maximum - minimum) * value + minimum

    def _random_distribution_int(self, minimum: int, maximum: int, coverage: float = 2.32) -> int:
        num = self._random_distribution_double(minimum, maximum + 1.0, coverage)
        return maximum if num >= maximum + 1.0 else int(num)

    def _random_chinese_char(self) -> str:
        while True:
            high = 176 + abs(random.randrange(39))
            low = 161 + abs(random.randrange(93))
            try:
                text = bytes([high, low]).decode("gbk")
            except UnicodeDecodeError:
                continue
            if text:
                return text[0]

    def _random_chinese_name(self, minimum_length: int, maximum_length: int) -> str:
        length = self._random_int(minimum_length, maximum_length)
        return "".join(self._random_chinese_char() for _ in range(length))

    # 冷却逻辑严格沿用 mirai 旧字段，迁移后继续使用同一组时间戳和次数。
    def _mk_target_time(self, user: KunUser, now_millis: int) -> int:
        reset = self._reset_timestamp(now_millis, user.reset_time)
        if user.mk_time < reset:
            user.mk_time = now_millis
            user.mk_times = 1
            return now_millis
        target_time = user.mk_time + min(self._fibonacci(user.mk_times + 1), 180) * 60000
        if target_time > now_millis:
            return min(target_time, reset + 86400000)
        user.mk_time = now_millis
        user.mk_times += 1
        return now_millis

    def _jj_target_time(self, user: KunUser, now_millis: int) -> int:
        reset = self._reset_timestamp(now_millis, user.reset_time)
        if user.jj_time < reset:
            user.jj_time = now_millis
            user.jj_times = 1
            return now_millis
        target_time = user.jj_time + min(self._fibonacci(user.jj_times + 1), 180) * 60000
        if target_time > now_millis:
            return min(target_time, reset + 86400000)
        user.jj_time = now_millis
        user.jj_times += 1
        return now_millis

    def _tz_target_time(self, user: KunUser, now_millis: int) -> int:
        reset = self._reset_timestamp(now_millis, user.reset_time)
        if user.tz_time < reset:
            user.tz_time = now_millis
            user.tz_times = 1
            return now_millis
        target_time = user.tz_time + (3600000 * 3 if user.tz_times in {1, 2} else 3600000 * 24)
        if target_time > now_millis:
            return min(target_time, reset + 86400000)
        user.tz_time = now_millis
        user.tz_times += 1
        return now_millis

    def _reset_timestamp(self, now_millis: int, reset_hour: int) -> int:
        now = datetime.fromtimestamp(now_millis / 1000)
        reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=reset_hour)
        if reset.timestamp() * 1000 > now_millis:
            reset -= timedelta(days=1)
        return int(reset.timestamp() * 1000)

    def _fibonacci(self, index: int) -> int:
        if index <= 2:
            return 1
        a, b = 1, 1
        for _ in range(3, index + 1):
            a, b = b, a + b
        return b

    def _date_str(self, timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d")

    def _time_str(self, timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp / 1000).strftime("%H:%M:%S")

    def _full_time_str(self, timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S")

    def _this_monday(self, timestamp: int) -> str:
        date = datetime.fromtimestamp(timestamp / 1000)
        monday = date - timedelta(days=date.weekday())
        return monday.strftime("%Y-%m-%d")

    def _milli_second_to_str(self, begin_time: int, end_time: int, show_second: bool) -> str:
        second = abs(end_time - begin_time) // 1000
        if second == 0:
            return "0秒"
        if not show_second and second < 60:
            return "小于1分钟"
        parts: list[str] = []
        day, second = divmod(second, 86400)
        hour, second = divmod(second, 3600)
        minute, second = divmod(second, 60)
        if day:
            parts.append(f"{day}天")
        if hour:
            parts.append(f"{hour}时")
        if minute:
            parts.append(f"{minute}分")
        if show_second and second:
            parts.append(f"{second}秒")
        return "".join(parts) if parts else ("小于1分钟" if not show_second else "0秒")
