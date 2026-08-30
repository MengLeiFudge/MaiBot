from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .storage import RuntimeJsonStore
from .storage import read_json_file


logger = logging.getLogger(__name__)
RIGHTCODES_DRAW_BASE_URL = "https://www.rightapi.ai/draw"
RIGHTCODES_DRAW_TASK_BASE_URL = "https://www.rightapi.ai"
RIGHTCODES_DRAW_USER_AGENT = "QQBot-RightCodes/1.0"
RIGHTCODES_DRAW_DEFAULT_MODEL = "gpt-image-2"
RIGHTCODES_DRAW_POINT_PRICE_MULTIPLIER = 1000
RIGHTCODES_DRAW_MODEL_ORDER = (
    "gpt-image-2",
    "gpt-image-2-vip",
    "nano-banana-2-lite",
    "nano-banana-pro",
)
RIGHTCODES_DRAW_MODELS = set(RIGHTCODES_DRAW_MODEL_ORDER)
RIGHTCODES_DRAW_MODEL_PRICES = {
    "gpt-image-2": Decimal("0.04"),
    "gpt-image-2-vip": Decimal("0.13"),
    "nano-banana-2-lite": Decimal("0.05"),
    "nano-banana-pro": Decimal("0.18"),
}
RIGHTCODES_DRAW_MODEL_DESCRIPTIONS = {
    "gpt-image-2": "OpenAI 画图模型，上游支持 1K",
    "gpt-image-2-vip": "OpenAI 官方直连，上游当前支持 1K，官方已停止 2K、4K",
    "nano-banana-2-lite": "即 gemini-3.1-flash-lite-image，上游支持 1K",
    "nano-banana-pro": "即 gemini-3-pro-image-preview，上游支持 1K、2K、4K",
}
_DRAW_POINTS_LOCK = threading.Lock()
_DRAW_POINTS_QUERY_RE = re.compile(
    r"^(?:(?:查|查询|查看|看)(?:一下)?)?(?:我(?:的)?|当前)?(?:生图)?积分(?:余额|情况|多少)?$"
)
_DRAW_POINTS_ENGLISH_QUERY_RE = re.compile(r"^(?:balance|points?)$", re.IGNORECASE)
_DRAW_POINTS_MUTATION_RE = re.compile(
    r"(?:加|增加|扣|扣除|减|减少|改|修改|设置|设定|送|赠|赠送|充值|充).{0,16}积分"
    r"|积分.{0,16}(?:加|增加|扣|扣除|减|减少|改|修改|设置|设定|送|赠|赠送|充值|充)"
)
_DRAW_MODEL_SWITCH_PRIMARY_RE = re.compile(r"^切换\s*生图\s*模型\s*(.*)$", re.IGNORECASE)
_DRAW_MODEL_SWITCH_ALIAS_RE = re.compile(r"^生图\s*模型\s*(.+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RightCodesDrawRequest:
    prompt: str
    model: str = RIGHTCODES_DRAW_DEFAULT_MODEL
    image_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RightCodesDrawResult:
    image_url: str
    total_seconds: float


@dataclass(frozen=True, slots=True)
class RightCodesDrawPointBalance:
    user_id: str
    points: int
    model: str
    multiplier: int


@dataclass(frozen=True, slots=True)
class RightCodesDrawQuotaResult:
    allowed: bool
    user_id: str
    model: str
    cost_points: int
    balance_before: int
    balance_after: int
    multiplier: int
    price: str


class RightCodesDrawTimeoutError(TimeoutError):
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"RightCodes 生图超过 {timeout_seconds:.0f} 秒未返回")


class AsyncDrawHttpClient(Protocol):
    async def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> Any:
        ...

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> Any:
        ...


class RightCodesDrawClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = RIGHTCODES_DRAW_BASE_URL,
        task_base_url: str = RIGHTCODES_DRAW_TASK_BASE_URL,
        timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 2.0,
        http_client: AsyncDrawHttpClient | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.task_base_url = task_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = max(0.0, poll_interval_seconds)
        self.http_client = http_client

    async def draw(self, request: RightCodesDrawRequest) -> RightCodesDrawResult:
        if not self.api_key:
            raise ValueError("缺少 RightCodes 生图 API Key")
        started = time.perf_counter()
        deadline = started + self.timeout_seconds
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": RIGHTCODES_DRAW_USER_AGENT,
        }
        data = await self._post_json(
            f"{self.base_url}/v1/images/generations",
            headers=headers,
            json={
                "model": request.model,
                "prompt": request.prompt,
                "image": list(request.image_urls),
                "n": 1,
                "size": "1:1",
                "imageSize": "1K",
                "async": True,
            },
            timeout=self.timeout_seconds,
        )
        image_url = extract_image_url_from_object(data)
        if image_url:
            return RightCodesDrawResult(image_url=image_url, total_seconds=time.perf_counter() - started)
        task_id = extract_rightcodes_draw_task_id(data)
        if not task_id:
            raise RuntimeError(extract_rightcodes_task_error(data) or "RightCodes 生图没有返回任务 ID")
        last_status = extract_rightcodes_task_status(data) or "submitted"
        progress = extract_rightcodes_task_progress(data)
        last_logged_at = started
        logger.info(
            "[QQBotFeatures] RightCodes draw task submitted: model=%s task_id=%s status=%s progress=%s",
            request.model,
            task_id,
            last_status,
            progress,
        )

        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise RightCodesDrawTimeoutError(self.timeout_seconds)
            await asyncio.sleep(min(self.poll_interval_seconds, remaining))
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise RightCodesDrawTimeoutError(self.timeout_seconds)
            data = await self._get_json(
                f"{self.task_base_url}/v1/tasks/{task_id}",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                    "User-Agent": RIGHTCODES_DRAW_USER_AGENT,
                },
                timeout=remaining,
            )
            image_url = extract_image_url_from_object(data)
            if image_url:
                logger.info(
                    "[QQBotFeatures] RightCodes draw task completed: model=%s task_id=%s elapsed=%.2fs",
                    request.model,
                    task_id,
                    time.perf_counter() - started,
                )
                return RightCodesDrawResult(image_url=image_url, total_seconds=time.perf_counter() - started)
            status = extract_rightcodes_task_status(data)
            progress = extract_rightcodes_task_progress(data)
            now = time.perf_counter()
            if status != last_status or now - last_logged_at >= 30.0:
                logger.info(
                    "[QQBotFeatures] RightCodes draw task pending: model=%s task_id=%s status=%s progress=%s elapsed=%.2fs",
                    request.model,
                    task_id,
                    status or "unknown",
                    progress,
                    now - started,
                )
                last_status = status
                last_logged_at = now
            if status == "failed":
                raise RuntimeError(extract_rightcodes_task_error(data) or "RightCodes 生图任务失败")
            if status == "completed":
                raise RuntimeError("RightCodes 生图任务完成但没有返回图片")

    async def _post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> object:
        if self.http_client is not None:
            return await self.http_client.post_json(url, headers=headers, json=json, timeout=timeout)
        return await post_json(url, headers, json, timeout)

    async def _get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> object:
        if self.http_client is not None:
            return await self.http_client.get_json(url, headers=headers, timeout=timeout)
        return await get_json(url, headers, timeout)


class RightCodesDrawQuotaStore:
    def __init__(
        self,
        data_root: Path,
        *,
        multiplier: int = RIGHTCODES_DRAW_POINT_PRICE_MULTIPLIER,
    ) -> None:
        self.data_root = Path(data_root)
        self.multiplier = max(1, int(multiplier))
        self.path = self.data_root / "ai" / "draw_points.json"
        self.store = RuntimeJsonStore(self.data_root)

    def record_group_message(self, user_id: int | str, *, amount: int = 1) -> int:
        user_key = str(user_id).strip()
        if not user_key or amount <= 0:
            return 0
        with _DRAW_POINTS_LOCK:
            payload = self._read()
            users = get_users_payload(payload)
            user_payload = get_user_payload(users, user_key)
            points = int(user_payload.get("points", 0) or 0) + int(amount)
            user_payload["points"] = points
            users[user_key] = user_payload
            payload["users"] = users
            self._write(payload)
            return points

    def get_balance(self, user_id: int | str) -> RightCodesDrawPointBalance:
        user_key = str(user_id).strip()
        if not user_key:
            return RightCodesDrawPointBalance("", 0, RIGHTCODES_DRAW_DEFAULT_MODEL, self.multiplier)
        with _DRAW_POINTS_LOCK:
            payload = self._read()
            users = get_users_payload(payload)
            user_payload = get_user_payload(users, user_key)
        return RightCodesDrawPointBalance(
            user_id=user_key,
            points=int(user_payload.get("points", 0) or 0),
            model=normalize_rightcodes_draw_model(user_payload.get("model")),
            multiplier=self.multiplier,
        )

    def set_model(self, user_id: int | str, model: str) -> RightCodesDrawPointBalance:
        user_key = str(user_id).strip()
        model_key = str(model or "").strip().lower()
        if not user_key:
            raise ValueError("缺少 QQ 用户 ID")
        if model_key not in RIGHTCODES_DRAW_MODELS:
            raise ValueError(f"不支持的生图模型: {model}")
        with _DRAW_POINTS_LOCK:
            payload = self._read()
            users = get_users_payload(payload)
            user_payload = get_user_payload(users, user_key)
            user_payload["model"] = model_key
            users[user_key] = user_payload
            payload["users"] = users
            self._write(payload)
            return RightCodesDrawPointBalance(
                user_id=user_key,
                points=int(user_payload.get("points", 0) or 0),
                model=model_key,
                multiplier=self.multiplier,
            )

    def get_points_ranking(self, *, limit: int = 10) -> tuple[RightCodesDrawPointBalance, ...]:
        with _DRAW_POINTS_LOCK:
            payload = self._read()
            users = get_users_payload(payload)
        balances = [
            RightCodesDrawPointBalance(
                user_id=user_id,
                points=int(user_payload.get("points", 0) or 0),
                model=normalize_rightcodes_draw_model(user_payload.get("model")),
                multiplier=self.multiplier,
            )
            for user_id, user_payload in users.items()
        ]
        balances.sort(key=lambda item: (-item.points, sortable_user_id(item.user_id)))
        return tuple(balances[: max(0, int(limit))])

    def reserve(
        self,
        user_id: int | str,
        *,
        model: str = RIGHTCODES_DRAW_DEFAULT_MODEL,
    ) -> RightCodesDrawQuotaResult:
        user_key = str(user_id).strip()
        model = normalize_rightcodes_draw_model(model)
        cost_points = calculate_rightcodes_draw_model_points(model, multiplier=self.multiplier)
        price = format_rightcodes_draw_model_price(model)
        if not user_key:
            return RightCodesDrawQuotaResult(False, "", model, cost_points, 0, 0, self.multiplier, price)
        with _DRAW_POINTS_LOCK:
            payload = self._read()
            users = get_users_payload(payload)
            user_payload = get_user_payload(users, user_key)
            balance = int(user_payload.get("points", 0) or 0)
            if balance < cost_points:
                return RightCodesDrawQuotaResult(
                    False, user_key, model, cost_points, balance, balance, self.multiplier, price
                )
            user_payload["points"] = balance - cost_points
            users[user_key] = user_payload
            payload["users"] = users
            self._write(payload)
            return RightCodesDrawQuotaResult(
                True,
                user_key,
                model,
                cost_points,
                balance,
                balance - cost_points,
                self.multiplier,
                price,
            )

    def refund(self, reservation: RightCodesDrawQuotaResult) -> None:
        if not reservation.allowed or not reservation.user_id:
            return
        with _DRAW_POINTS_LOCK:
            payload = self._read()
            users = get_users_payload(payload)
            user_payload = get_user_payload(users, reservation.user_id)
            if reservation.cost_points > 0:
                points = int(user_payload.get("points", 0) or 0)
                user_payload["points"] = points + reservation.cost_points
            users[reservation.user_id] = user_payload
            payload["users"] = users
            self._write(payload)

    def _read(self) -> dict[str, object]:
        raw = self.store.read("rightcodes.draw_points", {"schema_version": 2, "users": {}})
        if not isinstance(raw, dict):
            raw = {"schema_version": 2, "users": {}}
        normalized = normalize_draw_points_payload(raw)
        if normalized != raw:
            self.store.write("rightcodes.draw_points", normalized)
        raw = normalized
        if not self.path.exists():
            return raw
        fingerprint = fingerprint_file(self.path)
        imports = self.store.read("rightcodes.draw_points_legacy_imports", {"files": {}})
        imported_files = imports.get("files") if isinstance(imports, dict) else {}
        if isinstance(imported_files, dict) and imported_files.get(str(self.path)) == fingerprint:
            return raw
        legacy_raw = read_json_file(self.path, {"schema_version": 1, "users": {}})
        merged = merge_draw_points_payload(raw, legacy_raw)
        if merged != raw:
            self.store.write("rightcodes.draw_points", merged)
        if not isinstance(imported_files, dict):
            imported_files = {}
        imported_files[str(self.path)] = fingerprint
        self.store.write("rightcodes.draw_points_legacy_imports", {"files": imported_files})
        return merged

    def _write(self, payload: dict[str, object]) -> None:
        payload = normalize_draw_points_payload(payload)
        self.store.write("rightcodes.draw_points", payload)


def parse_rightcodes_draw_command(text: str) -> RightCodesDrawRequest | None:
    normalized = text.strip()
    rest = extract_rightcodes_draw_prompt(normalized)
    if rest is None or not rest:
        return None
    if extract_removed_rightcodes_draw_temporary_model(normalized) is not None:
        return None
    return RightCodesDrawRequest(prompt=rest)


def looks_like_rightcodes_draw_invocation(text: str) -> bool:
    return extract_rightcodes_draw_prompt(text.strip()) is not None


def looks_like_rightcodes_draw_suggestion(text: str) -> bool:
    return extract_natural_draw_prompt(text.strip()) is not None


def looks_like_rightcodes_draw_feature_request(text: str, *, is_direct_or_private: bool = False) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if looks_like_rightcodes_draw_suggestion(normalized):
        return is_direct_or_private
    return (
        looks_like_rightcodes_draw_invocation(normalized)
        or looks_like_rightcodes_draw_points_mutation_request(normalized)
        or looks_like_rightcodes_draw_points_query(normalized)
        or looks_like_rightcodes_draw_points_ranking(normalized)
        or looks_like_rightcodes_draw_help_command(normalized)
        or looks_like_rightcodes_draw_model_switch(normalized)
    )


def extract_rightcodes_draw_prompt(text: str) -> str | None:
    command_match = re.match(r"^(?:棉花糖|棉花)\s*生图([\s\S]*)$", text)
    if command_match is not None:
        return command_match.group(1).strip()
    return None


def extract_removed_rightcodes_draw_temporary_model(text: str) -> str | None:
    rest = extract_rightcodes_draw_prompt(text.strip())
    if not rest:
        return None
    bracket_match = re.match(r"^\[([^\]]+)\](?:\s+|$)", rest)
    if bracket_match is not None:
        candidate = bracket_match.group(1).strip().lower()
        return candidate if candidate in RIGHTCODES_DRAW_MODELS else None
    candidate = rest.split(maxsplit=1)[0].strip().lower()
    return candidate if candidate in RIGHTCODES_DRAW_MODELS else None


def extract_natural_draw_prompt(text: str) -> str | None:
    natural_match = re.match(r"^生成\s*(.+?)(?:的)?(?:图片|图像|图)\s*$", text)
    if natural_match is not None:
        return natural_match.group(1).strip()
    return None


def format_rightcodes_draw_suggestion_message() -> str:
    return "你是不是想用生图功能？指令是：棉花糖生图 提示词。这个功能会消耗生图积分。"


def looks_like_rightcodes_draw_command(text: str) -> bool:
    return parse_rightcodes_draw_command(text) is not None


def format_rightcodes_draw_missing_prompt_message() -> str:
    return "生图需要文字提示词。用法：棉花糖生图 提示词。需要换模型时，先发送：切换生图模型 模型名。"


def format_rightcodes_draw_temporary_model_removed(model: str) -> str:
    return (
        f"生图命令不再支持临时指定模型 {model}，本次没有扣积分。"
        f"请先发送“切换生图模型 {model}”，再发送“棉花糖生图 提示词”。"
    )


def looks_like_rightcodes_draw_help_command(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text.strip())
    return normalized in {
        "生图模型说明",
        "生图模型",
        "生图价格",
        "画图模型说明",
        "画图模型",
        "画图价格",
        "棉花糖生图模型说明",
        "棉花糖生图模型",
        "棉花糖生图价格",
        "棉花生图模型说明",
        "棉花生图模型",
        "棉花生图价格",
    }


def looks_like_rightcodes_draw_points_query(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if _DRAW_POINTS_ENGLISH_QUERY_RE.fullmatch(normalized):
        return True
    compact = re.sub(r"\s+", "", normalized)
    return _DRAW_POINTS_QUERY_RE.fullmatch(compact) is not None


def looks_like_rightcodes_draw_points_ranking(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.strip())
    return compact in {"积分排行", "积分排行榜"}


def looks_like_rightcodes_draw_points_mutation_request(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.strip())
    if not compact or "积分" not in compact:
        return False
    return _DRAW_POINTS_MUTATION_RE.search(compact) is not None


def extract_rightcodes_draw_model_switch(text: str) -> str | None:
    normalized = text.strip()
    match = _DRAW_MODEL_SWITCH_PRIMARY_RE.fullmatch(normalized)
    if match is not None:
        return match.group(1).strip()
    match = _DRAW_MODEL_SWITCH_ALIAS_RE.fullmatch(normalized)
    if match is not None:
        return match.group(1).strip()
    return None


def looks_like_rightcodes_draw_model_switch(text: str) -> bool:
    return extract_rightcodes_draw_model_switch(text) is not None


def parse_rightcodes_draw_model_switch(text: str) -> str | None:
    candidate = extract_rightcodes_draw_model_switch(text)
    if candidate is None:
        return None
    model = candidate.lower()
    return model if model in RIGHTCODES_DRAW_MODELS else None


def format_rightcodes_draw_model_help(
    current_model: str = RIGHTCODES_DRAW_DEFAULT_MODEL,
    *,
    multiplier: int = RIGHTCODES_DRAW_POINT_PRICE_MULTIPLIER,
) -> str:
    current_model = normalize_rightcodes_draw_model(current_model)
    lines = [f"当前生图模型：{current_model}", "可用模型："]
    for model in RIGHTCODES_DRAW_MODEL_ORDER:
        description = RIGHTCODES_DRAW_MODEL_DESCRIPTIONS[model]
        price = format_rightcodes_draw_model_price(model)
        current_mark = "（当前）" if model == current_model else ""
        lines.append(
            f"· {model}{current_mark}：${price}/次，"
            f"{calculate_rightcodes_draw_model_points(model, multiplier=multiplier)} 积分。{description}"
        )
    lines.extend(
        [
            "",
            "切换模型：切换生图模型 模型名",
            "隐藏别名：生图模型 模型名",
            "切换后生图：棉花糖生图 提示词",
        ]
    )
    return "\n".join(lines)


def format_rightcodes_draw_points_status(balance: RightCodesDrawPointBalance) -> str:
    cost_points = calculate_rightcodes_draw_model_points(balance.model, multiplier=balance.multiplier)
    return "\n".join(
        [
            f"当前生图积分：{balance.points}",
            f"当前生图模型：{balance.model}",
            f"当前模型消耗：{cost_points} 积分/次",
            "",
            "查看模型：生图模型",
            "切换模型：切换生图模型 模型名",
        ]
    )


def format_rightcodes_draw_points_ranking(
    ranking: tuple[RightCodesDrawPointBalance, ...],
    *,
    resolve_display_name: Callable[[str], str] | None = None,
) -> str:
    if not ranking:
        return "全群还没有生图积分记录。"
    lines = ["全群生图积分排行榜："]
    for index, balance in enumerate(ranking, start=1):
        display_name = resolve_display_name(balance.user_id) if resolve_display_name is not None else balance.user_id
        identity = display_name if display_name != balance.user_id else f"QQ {mask_qq_user_id(balance.user_id)}"
        lines.append(f"{index}. {identity}：{balance.points} 积分")
    return "\n".join(lines)


def format_rightcodes_draw_model_switch_success(balance: RightCodesDrawPointBalance) -> str:
    cost_points = calculate_rightcodes_draw_model_points(balance.model, multiplier=balance.multiplier)
    description = RIGHTCODES_DRAW_MODEL_DESCRIPTIONS[balance.model]
    return "\n".join(
        [
            f"已切换生图模型：{balance.model}",
            f"单次消耗：{cost_points} 积分",
            description,
            "之后发送“棉花糖生图 提示词”就会使用这个模型。",
        ]
    )


def format_rightcodes_draw_model_switch_invalid(candidate: str) -> str:
    candidate = str(candidate or "").strip()
    first_line = f"不支持这个生图模型：{candidate}" if candidate else "请指定要切换的生图模型。"
    return "\n".join(
        [
            first_line,
            "查看模型：生图模型",
            "切换用法：切换生图模型 模型名",
        ]
    )


def format_rightcodes_draw_points_mutation_denied() -> str:
    return "生图积分只能通过群消息自动累计，并在生图时自动扣除；普通聊天不能手动加分或改分。"


def format_draw_start_message(quota: RightCodesDrawQuotaResult) -> str:
    return (
        "收到，棉花糖开始生图任务啦！"
        f"本次使用 {quota.model}，扣 {quota.cost_points} 积分，"
        f"剩余 {quota.balance_after} 积分。"
    )


def format_draw_quota_exceeded_message(quota: RightCodesDrawQuotaResult) -> str:
    return (
        f"积分不够啦：{quota.model} 需要 {quota.cost_points} 积分"
        f"（价格 ${quota.price} x 倍率 {quota.multiplier}），"
        f"你现在有 {quota.balance_before} 积分。"
        "可发送“生图模型”查看价格，或用“切换生图模型 模型名”切换后重试。"
    )


def format_rightcodes_draw_success(
    result: RightCodesDrawResult,
    *,
    model: str,
    image_count: int = 1,
) -> str:
    return (
        "✨ 生成成功！\n"
        f"📊 耗时: {result.total_seconds:.2f}s\n"
        f"🖼️ 数量: {image_count}张\n"
        f"🤖 模型: {model}"
    )


def format_rightcodes_draw_failure(exc: Exception) -> str:
    return (
        f"❌ 生成失败: {extract_rightcodes_draw_error_message(exc)}。"
        "本次扣除的积分已退回。可发送“生图模型”查看模型，"
        "或用“切换生图模型 模型名”切换后重试。"
    )


def format_rightcodes_draw_timeout(timeout_seconds: float) -> str:
    return (
        f"❌ 生成失败: RightCodes 生图超过 {timeout_seconds:.0f} 秒还没返回，"
        "本次扣除的积分已退回。可发送“生图模型”查看模型，"
        "或用“切换生图模型 模型名”切换后重试。"
    )


def extract_rightcodes_draw_error_message(exc: Exception) -> str:
    if isinstance(exc, RightCodesDrawTimeoutError):
        return f"RightCodes 生图超过 {exc.timeout_seconds:.0f} 秒未返回"
    if isinstance(exc, TimeoutError):
        return "RightCodes 生图请求超时"
    if isinstance(exc, HTTPError):
        detail = read_http_error_detail(exc)
        return detail or f"上游返回 HTTP {exc.code}"
    message = str(exc).strip()
    return message or type(exc).__name__


def calculate_rightcodes_draw_model_points(
    model: str,
    *,
    multiplier: int = RIGHTCODES_DRAW_POINT_PRICE_MULTIPLIER,
) -> int:
    points = get_rightcodes_draw_model_price(model) * Decimal(str(multiplier))
    return int(points.to_integral_value(rounding=ROUND_CEILING))


def get_rightcodes_draw_model_price(model: str) -> Decimal:
    return RIGHTCODES_DRAW_MODEL_PRICES[normalize_rightcodes_draw_model(model)]


def format_rightcodes_draw_model_price(model: str) -> str:
    return f"{get_rightcodes_draw_model_price(model):.2f}"


async def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: float,
) -> object:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers=headers, method="POST")
    return await run_urlopen_json(request, timeout)


async def get_json(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> object:
    request = Request(url, headers=headers, method="GET")
    return await run_urlopen_json(request, timeout)


async def run_urlopen_json(request: Request, timeout: float) -> object:
    def read_response() -> object:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            if status >= 400:
                raise RuntimeError(f"RightCodes draw request failed: {status}")
            body = response.read().decode("utf-8")
        return json.loads(body)

    return await asyncio.to_thread(read_response)


def read_http_error_detail(exc: HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:200]
    for path in (("error", "message"), ("message",), ("detail",)):
        value: object = data
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return body[:200]


def extract_rightcodes_draw_task_id(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    value = data.get("task_id")
    return value.strip() if isinstance(value, str) else ""


def extract_rightcodes_task_status(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    value = data.get("status")
    return value.strip().lower() if isinstance(value, str) else ""


def extract_rightcodes_task_progress(data: object) -> int | None:
    if not isinstance(data, dict):
        return None
    value = data.get("progress")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def extract_rightcodes_task_error(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    message = data.get("message")
    return message.strip() if isinstance(message, str) else ""


def extract_image_url_from_object(data: object) -> str:
    if isinstance(data, dict):
        value = data.get("b64_json")
        if isinstance(value, str) and value.strip():
            return f"data:image/png;base64,{value.strip()}"
        value = data.get("url")
        if isinstance(value, str):
            extracted = extract_image_url(value)
            if extracted:
                return extracted
        for child in data.values():
            extracted = extract_image_url_from_object(child)
            if extracted:
                return extracted
    elif isinstance(data, list):
        for item in data:
            extracted = extract_image_url_from_object(item)
            if extracted:
                return extracted
    return ""


def extract_image_url(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith(("http://", "https://", "data:image/")):
        return stripped
    match = re.search(r"(https?://\S+)", stripped)
    if match:
        return match.group(1).rstrip("，,。)")
    return ""


def get_users_payload(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = payload.get("users")
    if not isinstance(raw, dict):
        return {}
    users: dict[str, dict[str, object]] = {}
    for user_id, value in raw.items():
        if str(user_id).strip() and isinstance(value, dict):
            users[str(user_id)] = dict(value)
    return users


def get_user_payload(users: dict[str, dict[str, object]], user_key: str) -> dict[str, object]:
    raw = users.get(user_key)
    if not isinstance(raw, dict):
        return {"points": 0, "model": RIGHTCODES_DRAW_DEFAULT_MODEL}
    payload: dict[str, object] = {
        "points": safe_int(raw.get("points"), 0),
        "model": normalize_rightcodes_draw_model(raw.get("model")),
    }
    return payload


def normalize_draw_points_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {
        "schema_version": max(2, safe_int(payload.get("schema_version"), 2)),
        "users": {},
    }
    users: dict[str, dict[str, object]] = {}
    for user_id, raw_user in get_users_payload(payload).items():
        users[user_id] = get_user_payload({user_id: raw_user}, user_id)
    normalized["users"] = users
    return normalized


def merge_draw_points_payload(current: dict[str, object], legacy: dict[str, object]) -> dict[str, object]:
    merged = normalize_draw_points_payload(current)
    users = get_users_payload(merged)
    for user_id, legacy_user in get_users_payload(normalize_draw_points_payload(legacy)).items():
        current_exists = user_id in users
        current_user = get_user_payload(users, user_id)
        legacy_payload = get_user_payload({user_id: legacy_user}, user_id)
        current_points = safe_int(current_user.get("points"), 0)
        legacy_points = safe_int(legacy_payload.get("points"), 0)
        if legacy_points > current_points:
            current_user["points"] = legacy_points
        if not current_exists:
            current_user["model"] = normalize_rightcodes_draw_model(legacy_payload.get("model"))
        users[user_id] = current_user
    merged["users"] = users
    return merged


def fingerprint_file(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{stat.st_size}:{stat.st_mtime_ns}:{digest}"


def safe_int(value: object, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def normalize_rightcodes_draw_model(model: object) -> str:
    candidate = str(model or "").strip().lower()
    return candidate if candidate in RIGHTCODES_DRAW_MODELS else RIGHTCODES_DRAW_DEFAULT_MODEL


def mask_qq_user_id(user_id: object) -> str:
    value = str(user_id or "").strip()
    if len(value) <= 6:
        return "*" * max(1, len(value))
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"


def sortable_user_id(user_id: str) -> tuple[int, int | str]:
    user_key = str(user_id or "").strip()
    if user_key.isdigit():
        return (0, int(user_key))
    return (1, user_key)
