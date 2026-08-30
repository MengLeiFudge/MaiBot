from __future__ import annotations

from collections.abc import Callable

import hashlib
import random
import time


REREAD_COOLDOWN_SECONDS = 120.0
REREAD_DUPLICATE_WINDOW_SECONDS = 3.0


class RereadRepeatState:
    """Track probabilistic repeat runs independently for each group."""

    def __init__(
        self,
        *,
        cooldown_seconds: float = REREAD_COOLDOWN_SECONDS,
        duplicate_window_seconds: float = REREAD_DUPLICATE_WINDOW_SECONDS,
        rng: random.Random | None = None,
        now_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._groups: dict[str, dict[str, object]] = {}
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._duplicate_window_seconds = max(0.0, float(duplicate_window_seconds))
        self._rng = rng
        self._now_func = now_func

    def observe(
        self,
        group_id: str,
        text: str,
        *,
        message_id: str = "",
        sender_id: str = "",
        event_timestamp: float = 0.0,
    ) -> bool:
        normalized = normalize_reread_key(text)
        if not normalized:
            return False
        state = self._groups.setdefault(
            group_id,
            {
                "last_key": "",
                "consecutive_count": 0,
                "repeated_current_run": False,
                "cooldown_key": "",
                "cooldown_until": 0.0,
                "last_message_id": "",
                "recent_observed": {},
            },
        )
        if message_id and state.get("last_message_id") == message_id:
            return False
        state["last_message_id"] = message_id

        now = self._now_func()
        if self._is_duplicate_observation(
            state,
            sender_id=str(sender_id or ""),
            normalized=normalized,
            now=now,
        ):
            return False

        if state.get("last_key") != normalized:
            state["last_key"] = normalized
            state["consecutive_count"] = 1
            state["repeated_current_run"] = False
            return False

        consecutive_count = int(state.get("consecutive_count") or 0) + 1
        state["consecutive_count"] = consecutive_count
        in_cooldown = (
            state.get("cooldown_key") == normalized
            and now < float(state.get("cooldown_until") or 0.0)
        )
        if bool(state.get("repeated_current_run")) or in_cooldown:
            return False
        if self._sample(
            group_id=group_id,
            normalized=normalized,
            sender_id=sender_id,
            message_id=message_id,
            event_timestamp=event_timestamp,
            consecutive_count=consecutive_count,
        ) >= reread_probability(consecutive_count):
            return False

        state["repeated_current_run"] = True
        state["cooldown_key"] = normalized
        state["cooldown_until"] = now + self._cooldown_seconds
        return True

    def _sample(
        self,
        *,
        group_id: str,
        normalized: str,
        sender_id: str,
        message_id: str,
        event_timestamp: float,
        consecutive_count: int,
    ) -> float:
        if self._rng is not None:
            return self._rng.random()
        timestamp_key = f"{event_timestamp:.6f}" if event_timestamp > 0 else "0"
        raw = "\x1f".join(
            (
                group_id,
                normalized,
                str(sender_id or ""),
                str(message_id or ""),
                timestamp_key,
                str(consecutive_count),
            )
        )
        value = int.from_bytes(hashlib.sha256(raw.encode("utf-8")).digest()[:8], "big")
        return value / float(1 << 64)

    def _is_duplicate_observation(
        self,
        state: dict[str, object],
        *,
        sender_id: str,
        normalized: str,
        now: float,
    ) -> bool:
        if not sender_id or self._duplicate_window_seconds <= 0.0:
            return False
        recent = state.setdefault("recent_observed", {})
        if not isinstance(recent, dict):
            recent = {}
            state["recent_observed"] = recent
        stale_before = now - self._duplicate_window_seconds
        for key, observed_at in list(recent.items()):
            if float(observed_at) < stale_before:
                recent.pop(key, None)
        fingerprint = (sender_id, normalized)
        observed_at = recent.get(fingerprint)
        recent[fingerprint] = now
        return observed_at is not None and now - float(observed_at) <= self._duplicate_window_seconds


def normalize_reread_key(text: str) -> str:
    return " ".join(str(text).split()).strip()


def reread_probability(consecutive_count: int) -> float:
    if consecutive_count < 2:
        return 0.0
    return min(0.8, 0.2 + (consecutive_count - 2) * 0.15)
