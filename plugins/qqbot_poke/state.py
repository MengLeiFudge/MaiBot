from __future__ import annotations

from dataclasses import dataclass

import time


POKE_STATE_CALM = "CALM"
POKE_STATE_ANNOYED = "ANNOYED"
POKE_STATE_ARMED = "ARMED"
POKE_STATE_MUTE = "MUTE"


@dataclass(frozen=True, slots=True)
class PokeObservation:
    """一次群+机器人维度的拍击压力观测。"""

    group_id: str
    self_id: str
    sender_id: str
    count: int
    observed_at: float
    state: str
    generation: int
    mute_until: float = 0.0


class PokeStateMachine:
    """只存内存的群拍击状态机，插件重载即清空。"""

    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        mute_threshold: int = 9,
        mute_state_seconds: float = 30.0,
    ) -> None:
        self.window_seconds = max(1.0, float(window_seconds))
        self.mute_threshold = max(3, int(mute_threshold))
        self.mute_state_seconds = max(1.0, float(mute_state_seconds))
        self._timestamps: dict[tuple[str, str], list[float]] = {}
        self._mute_until: dict[tuple[str, str], float] = {}
        self._generations: dict[tuple[str, str], int] = {}
        self._ai_leases: dict[tuple[str, str], float] = {}
        self._ai_cooldowns: dict[tuple[str, str], float] = {}
        self._successful_mute_cooldowns: dict[tuple[str, str], float] = {}

    def record(
        self,
        group_id: str,
        self_id: str,
        sender_id: str,
        *,
        now: float | None = None,
    ) -> PokeObservation:
        """记录拍击并返回聚合所有群成员后的压力状态。"""

        key = self._key(group_id, self_id)
        sender = sender_id.strip()
        if key is None or not sender:
            raise ValueError("拍击状态缺少 group_id、self_id 或 sender_id")
        current = time.monotonic() if now is None else float(now)
        self._prune(current)
        mute_until = self._mute_until.get(key, 0.0)
        if mute_until > current:
            return PokeObservation(
                group_id=key[0],
                self_id=key[1],
                sender_id=sender,
                count=self.mute_threshold,
                observed_at=current,
                state=POKE_STATE_MUTE,
                generation=self._generations.get(key, 0),
                mute_until=mute_until,
            )

        stamps = [stamp for stamp in self._timestamps.get(key, []) if 0 <= current - stamp <= self.window_seconds]
        stamps.append(current)
        self._timestamps[key] = stamps[-16:]
        count = len(self._timestamps[key])
        state = self._pressure_state(count)
        if state == POKE_STATE_MUTE:
            mute_until = self.enter_mute(key[0], key[1], now=current)
        return PokeObservation(
            group_id=key[0],
            self_id=key[1],
            sender_id=sender,
            count=count,
            observed_at=current,
            state=state,
            generation=self._generations.get(key, 0),
            mute_until=mute_until,
        )

    def enter_mute(self, group_id: str, self_id: str, *, now: float | None = None) -> float:
        """进入 MUTE，清空旧压力并作废在途 AI 请求。"""

        key = self._key(group_id, self_id)
        if key is None:
            raise ValueError("MUTE 状态缺少 group_id 或 self_id")
        current = time.monotonic() if now is None else float(now)
        until = current + self.mute_state_seconds
        self._mute_until[key] = until
        self._generations[key] = self._generations.get(key, 0) + 1
        self._timestamps.pop(key, None)
        self._ai_leases.pop(key, None)
        self._ai_cooldowns.pop(key, None)
        return until

    def acquire_ai(
        self,
        group_id: str,
        self_id: str,
        *,
        now: float | None = None,
        lease_seconds: float = 120.0,
    ) -> bool:
        """尝试为当前群和机器人获取唯一 AI 请求租约。"""

        key = self._key(group_id, self_id)
        if key is None:
            return False
        current = time.monotonic() if now is None else float(now)
        self._prune(current)
        if self._mute_until.get(key, 0.0) > current:
            return False
        if self._ai_leases.get(key, 0.0) > current:
            return False
        if self._ai_cooldowns.get(key, 0.0) > current:
            return False
        self._ai_leases[key] = current + max(1.0, float(lease_seconds))
        return True

    def release_ai(
        self,
        group_id: str,
        self_id: str,
        *,
        now: float | None = None,
        cooldown_seconds: float = 3.0,
    ) -> None:
        """释放 AI 租约并设置短冷却。"""

        key = self._key(group_id, self_id)
        if key is None:
            return
        current = time.monotonic() if now is None else float(now)
        self._ai_leases.pop(key, None)
        self._ai_cooldowns[key] = current + max(0.0, float(cooldown_seconds))

    def start_successful_mute_cooldown(
        self,
        group_id: str,
        self_id: str,
        *,
        now: float | None = None,
        cooldown_seconds: float = 90.0,
    ) -> None:
        """记录成功禁言后的群+机器人冷却。"""

        key = self._key(group_id, self_id)
        if key is None:
            raise ValueError("禁言冷却缺少 group_id 或 self_id")
        current = time.monotonic() if now is None else float(now)
        self._successful_mute_cooldowns[key] = current + max(0.0, float(cooldown_seconds))

    def is_successful_mute_cooldown(
        self,
        group_id: str,
        self_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        """判断当前群和机器人是否仍处于成功禁言冷却。"""

        key = self._key(group_id, self_id)
        if key is None:
            return False
        current = time.monotonic() if now is None else float(now)
        self._prune(current)
        return self._successful_mute_cooldowns.get(key, 0.0) > current

    def is_current(self, group_id: str, self_id: str, generation: int) -> bool:
        """检查在途请求是否仍属于当前状态代际。"""

        key = self._key(group_id, self_id)
        return key is not None and self._generations.get(key, 0) == generation

    def _pressure_state(self, count: int) -> str:
        if count >= self.mute_threshold:
            return POKE_STATE_MUTE
        if count >= 6:
            return POKE_STATE_ARMED
        if count >= 3:
            return POKE_STATE_ANNOYED
        return POKE_STATE_CALM

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, stamps in self._timestamps.items()
            if not any(0 <= now - stamp <= self.window_seconds for stamp in stamps)
        ]
        for key in expired:
            self._timestamps.pop(key, None)
        for state in (
            self._mute_until,
            self._ai_leases,
            self._ai_cooldowns,
            self._successful_mute_cooldowns,
        ):
            for key, until in list(state.items()):
                if until <= now:
                    state.pop(key, None)

    @staticmethod
    def _key(group_id: str, self_id: str) -> tuple[str, str] | None:
        group = group_id.strip()
        account = self_id.strip()
        return (group, account) if group and account else None
