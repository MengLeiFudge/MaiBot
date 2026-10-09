from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import re
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener


SUB2API_DEFAULT_TIMEOUT_SECONDS = 90.0
SUB2API_DEFAULT_REFRESH_INTERVAL_SECONDS = 300.0
SUB2API_QUERY_RE = re.compile(r"^用量$", re.IGNORECASE)
SUB2API_USAGE_ALERT_THRESHOLDS = (80, 90, 95)
SUB2API_LIST_PAGE_SIZE = 100
SUB2API_USER_BREAKDOWN_LIMIT = 200
SUB2API_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
SUB2API_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
SUB2API_ANTIGRAVITY_QUOTA_GROUPS = (
    (
        "G3P",
        ("gemini-3-pro-low", "gemini-3-pro-high", "gemini-3-pro-preview"),
    ),
    ("G3F", ("gemini-3-flash",)),
    (
        "G31FI",
        ("gemini-2.5-flash-image", "gemini-3.1-flash-image", "gemini-3-pro-image"),
    ),
    (
        "Claude",
        (
            "claude-fable-5-1",
            "claude-fable-5",
            "claude-sonnet-4-5",
            "claude-opus-4-5-thinking",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-opus-4-6-thinking",
            "claude-opus-4-7",
            "claude-opus-4-8",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Sub2APIConfig:
    base_url: str
    admin_api_key: str
    timeout_seconds: float
    refresh_interval_seconds: float
    alert_group_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Sub2APICommand:
    pass


@dataclass(frozen=True, slots=True)
class Sub2APIWindowStats:
    requests: int = 0
    tokens: int = 0
    cost: float = 0.0
    user_cost: float | None = None


@dataclass(frozen=True, slots=True)
class Sub2APIUsageWindow:
    utilization: float | None = None
    resets_at: str = ""
    remaining_seconds: int | None = None
    window_stats: Sub2APIWindowStats | None = None


@dataclass(frozen=True, slots=True)
class Sub2APIQuotaWindow:
    """One platform-defined quota window and its report label."""

    label: str
    usage: Sub2APIUsageWindow | None = None


@dataclass(frozen=True, slots=True)
class Sub2APIAccountUsage:
    account_id: int
    name: str
    platform: str = ""
    account_type: str = ""
    status: str = ""
    current_concurrency: int | None = None
    last_used_at: str = ""
    source: str = ""
    updated_at: str = ""
    five_hour: Sub2APIUsageWindow | None = None
    seven_day: Sub2APIUsageWindow | None = None
    error: str = ""
    quota_windows: tuple[Sub2APIQuotaWindow, ...] = ()


@dataclass(frozen=True, slots=True)
class Sub2APIUserUsage:
    """One user's global actual-cost totals over fixed Shanghai business windows."""

    user_id: int
    username: str = ""
    email: str = ""
    current_day_actual_cost: float = 0.0
    current_week_actual_cost: float = 0.0
    thirty_day_actual_cost: float = 0.0


@dataclass(frozen=True, slots=True)
class Sub2APIAccountUserUsage:
    """One user's actual cost inside a single account's active seven-day cycle."""

    user_id: int
    username: str = ""
    email: str = ""
    actual_cost: float = 0.0


@dataclass(frozen=True, slots=True)
class Sub2APIAccountSevenDayRanking:
    """Cached per-user ranking and refresh state for one stable account ID."""

    account_id: int
    users: tuple[Sub2APIAccountUserUsage, ...] = ()
    refreshed_at: datetime | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class Sub2APIUsageSnapshot:
    """One immutable cache view of account quotas and user consumption rankings."""

    accounts: tuple[Sub2APIAccountUsage, ...] = ()
    account_seven_day_rankings: tuple[Sub2APIAccountSevenDayRanking, ...] = ()
    users: tuple[Sub2APIUserUsage, ...] = ()
    accounts_refreshed_at: datetime | None = None
    users_refreshed_at: datetime | None = None
    accounts_error: str = ""
    users_error: str = ""


@dataclass(frozen=True, slots=True)
class Sub2APIUsageAlert:
    usage: Sub2APIAccountUsage
    threshold: int
    utilization: float


class Sub2APIUsageCache:
    def __init__(self) -> None:
        self._snapshot: Sub2APIUsageSnapshot | None = None

    def get_latest(self) -> Sub2APIUsageSnapshot | None:
        return self._snapshot

    def set(self, snapshot: Sub2APIUsageSnapshot) -> None:
        self._snapshot = snapshot


class AsyncSub2APIHttpClient(Protocol):
    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> Any:
        ...


def load_sub2api_config(config: object) -> Sub2APIConfig:
    return Sub2APIConfig(
        base_url=str(get_config_value(config, "sub2api_base_url", "") or "").strip().rstrip("/"),
        admin_api_key=str(get_config_value(config, "sub2api_admin_api_key", "") or "").strip(),
        timeout_seconds=float(get_config_value(config, "sub2api_timeout_seconds", SUB2API_DEFAULT_TIMEOUT_SECONDS) or SUB2API_DEFAULT_TIMEOUT_SECONDS),
        refresh_interval_seconds=max(
            60.0,
            float(
                get_config_value(
                    config,
                    "sub2api_refresh_interval_seconds",
                    SUB2API_DEFAULT_REFRESH_INTERVAL_SECONDS,
                )
                or SUB2API_DEFAULT_REFRESH_INTERVAL_SECONDS
            ),
        ),
        alert_group_ids=parse_sub2api_group_ids(get_config_value(config, "sub2api_alert_group_ids", "")),
    )


def get_config_value(config: object, key: str, default: object = None) -> object:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            value = getter(key)
            return default if value is None else value
    return getattr(config, key, default)


def parse_sub2api_usage_command(text: str) -> Sub2APICommand | None:
    normalized = normalize_command_text(text)
    if not normalized:
        return None
    if SUB2API_QUERY_RE.match(normalized):
        return Sub2APICommand()
    return None


def normalize_command_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def normalize_cache_key(account_name: str) -> str:
    return str(account_name or "").strip().casefold()


def looks_like_sub2api_usage_command(text: str) -> bool:
    return parse_sub2api_usage_command(text) is not None


def parse_sub2api_group_ids(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        values = raw.replace("，", ",").split(",")
    else:
        try:
            values = list(raw)
        except TypeError:
            values = [raw]
    group_ids = []
    seen = set()
    for value in values:
        group_id = str(value or "").strip()
        if not group_id or not group_id.isdigit() or group_id in seen:
            continue
        group_ids.append(group_id)
        seen.add(group_id)
    return tuple(group_ids)


def update_sub2api_usage_alert_state(
    usages: list[Sub2APIAccountUsage],
    alerted_thresholds_by_account: dict[str, set[int]],
) -> list[Sub2APIUsageAlert]:
    alerts: list[Sub2APIUsageAlert] = []
    current_keys = set()
    for usage in usages:
        if not supports_sub2api_account_seven_day_ranking(usage):
            continue
        account_key = sub2api_alert_account_key(usage)
        current_keys.add(account_key)
        utilization = usage.five_hour.utilization if usage.five_hour is not None else None
        if utilization is None:
            continue
        reached = {threshold for threshold in SUB2API_USAGE_ALERT_THRESHOLDS if utilization >= threshold}
        previous = alerted_thresholds_by_account.get(account_key, set())
        newly_reached = reached - previous
        if newly_reached:
            alerts.append(
                Sub2APIUsageAlert(
                    usage=usage,
                    threshold=max(newly_reached),
                    utilization=utilization,
                )
            )
        alerted_thresholds_by_account[account_key] = reached
    for account_key in set(alerted_thresholds_by_account) - current_keys:
        alerted_thresholds_by_account.pop(account_key, None)
    return alerts


def sub2api_alert_account_key(usage: Sub2APIAccountUsage) -> str:
    if usage.account_id:
        return f"id:{usage.account_id}"
    return f"name:{normalize_cache_key(usage.name)}"


def supports_sub2api_account_seven_day_ranking(usage: Sub2APIAccountUsage) -> bool:
    """Return whether an account owns OpenAI's fixed 5h/7d quota cycle."""
    return (
        usage.platform.strip().casefold() == "openai"
        and usage.account_type.strip().casefold() == "oauth"
        and usage.status.strip().casefold() != "inactive"
    )


def is_disabled_sub2api_account(account: dict[str, Any]) -> bool:
    """Identify accounts explicitly disabled by status or the scheduling switch."""
    account_payload = ensure_dict(account.get("account") or account)
    status = account_payload.get("status") or account.get("status") or ""
    schedulable = account_payload.get("schedulable", account.get("schedulable"))
    return str(status).strip().casefold() == "inactive" or schedulable is False


def supports_sub2api_usage_endpoint(account: dict[str, Any]) -> bool:
    """Mirror the account types for which Sub2API exposes upstream quota data."""
    account_payload = ensure_dict(account.get("account") or account)
    platform = str(account_payload.get("platform") or account.get("platform") or "").strip().casefold()
    account_type = str(
        account_payload.get("type")
        or account_payload.get("account_type")
        or account.get("type")
        or account.get("account_type")
        or ""
    ).strip().casefold()
    if platform == "gemini":
        return True
    if platform == "anthropic":
        return account_type in {"oauth", "setup-token"}
    return platform in {"antigravity", "grok", "openai"} and account_type == "oauth"


class Sub2APIClient:
    def __init__(
        self,
        *,
        base_url: str,
        admin_api_key: str,
        timeout_seconds: float = SUB2API_DEFAULT_TIMEOUT_SECONDS,
        http_client: AsyncSub2APIHttpClient | None = None,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.admin_api_key = str(admin_api_key or "").strip()
        self.timeout_seconds = float(timeout_seconds or SUB2API_DEFAULT_TIMEOUT_SECONDS)
        self.http_client = http_client

    async def get_account_usage(self, *, force_refresh: bool = False) -> list[Sub2APIAccountUsage]:
        if not self.base_url:
            raise ValueError("Sub2API 地址还没配置")
        if not self.admin_api_key:
            raise ValueError("Sub2API Admin API Key 还没配置")
        accounts = await self.list_accounts()
        results: list[Sub2APIAccountUsage] = []
        for account in accounts:
            if is_disabled_sub2api_account(account):
                continue
            account_payload = ensure_dict(account.get("account") or account)
            account_id = int(account_payload.get("id") or account.get("id") or 0)
            if account_id <= 0:
                continue
            if not supports_sub2api_usage_endpoint(account):
                results.append(build_account_usage(account, {}))
                continue
            try:
                usage = await self.fetch_usage(account_id, force_refresh=force_refresh)
            except Exception as exc:
                results.append(build_account_usage(account, {"error": str(exc)}))
                continue
            results.append(build_account_usage(account, usage))
        return results

    async def list_accounts(self) -> list[dict[str, Any]]:
        return await self._list_paginated("accounts", sort_by="name")

    async def list_users(self) -> list[dict[str, Any]]:
        return await self._list_paginated("users", sort_by="id")

    async def get_user_usage_ranking(
        self,
        *,
        now: datetime | None = None,
    ) -> list[Sub2APIUserUsage]:
        """Fetch fixed 08:00 business windows before one shared daily trend, then rank users."""
        if not self.base_url:
            raise ValueError("Sub2API 地址还没配置")
        if not self.admin_api_key:
            raise ValueError("Sub2API Admin API Key 还没配置")
        users = await self.list_users()
        if len(users) > SUB2API_USER_BREAKDOWN_LIMIT:
            raise RuntimeError(
                "Sub2API 用户消费接口单次最多返回 200 位用户，"
                f"当前共有 {len(users)} 位用户，无法安全输出完整消费榜。"
            )
        end_time = normalize_sub2api_datetime(now or datetime.now(timezone.utc))
        day_started_at = current_day_window_start(end_time)
        week_started_at = current_week_window_start(end_time)
        thirty_day_started_at = thirty_day_window_start(end_time)
        window_starts = {
            "day": day_started_at,
            "week": week_started_at,
            "thirty_day": thirty_day_started_at,
        }
        hourly_costs_by_window: dict[str, dict[int, float]] = {}
        for window_name, window_started_at in window_starts.items():
            hourly_costs_by_window[window_name] = await self.fetch_hourly_user_costs(
                date_value=window_started_at.date(),
                start_hour=window_started_at,
                end_time=end_time,
            )
        earliest_full_day = thirty_day_started_at.date() + timedelta(days=1)
        daily_costs_by_date = await self.fetch_daily_user_costs(
            start_date=earliest_full_day,
            end_date=end_time.date(),
        )
        current_day_costs = build_rolling_user_costs(
            window_started_at=day_started_at,
            end_time=end_time,
            hourly_costs=hourly_costs_by_window["day"],
            daily_costs_by_date=daily_costs_by_date,
        )
        current_week_costs = build_rolling_user_costs(
            window_started_at=week_started_at,
            end_time=end_time,
            hourly_costs=hourly_costs_by_window["week"],
            daily_costs_by_date=daily_costs_by_date,
        )
        thirty_day_costs = build_rolling_user_costs(
            window_started_at=thirty_day_started_at,
            end_time=end_time,
            hourly_costs=hourly_costs_by_window["thirty_day"],
            daily_costs_by_date=daily_costs_by_date,
        )
        return build_user_usage_ranking(
            users,
            current_day_costs,
            current_week_costs,
            thirty_day_costs,
        )

    async def get_account_seven_day_ranking(
        self,
        account: Sub2APIAccountUsage,
        users: tuple[Sub2APIUserUsage, ...],
        *,
        now: datetime | None = None,
    ) -> tuple[Sub2APIAccountUserUsage, ...]:
        """Rank users within one account's active seven-day quota cycle."""
        if not self.base_url:
            raise ValueError("Sub2API 地址还没配置")
        if not self.admin_api_key:
            raise ValueError("Sub2API Admin API Key 还没配置")
        if not users:
            return ()
        end_time = normalize_sub2api_datetime(now or datetime.now(timezone.utc))
        window = account.seven_day
        resets_at = parse_datetime(window.resets_at) if window is not None else None
        if resets_at is None:
            raise RuntimeError(f"Sub2API 账号 {account.name or account.account_id} 缺少 7d 重置时间")
        resets_at = resets_at.astimezone(SUB2API_TIMEZONE)
        if end_time >= resets_at:
            raise RuntimeError(f"Sub2API 账号 {account.name or account.account_id} 的 7d 重置时间已过期")
        window_started_at = resets_at - timedelta(days=7)
        costs = {usage.user_id: 0.0 for usage in users}
        first_hour = floor_to_hour(window_started_at)
        if first_hour.date() == window_started_at.date() and first_hour <= end_time:
            hourly_costs = await self.fetch_hourly_user_costs(
                date_value=window_started_at.date(),
                start_hour=first_hour,
                end_time=end_time,
                account_id=account.account_id,
            )
            for user_id in costs:
                costs[user_id] += hourly_costs.get(user_id, 0.0)
        full_days_start = window_started_at.date() + timedelta(days=1)
        if full_days_start <= end_time.date():
            rows = await self.fetch_user_breakdown(
                start_date=full_days_start,
                end_date=end_time.date(),
                account_id=account.account_id,
            )
            full_day_costs = build_user_actual_costs(rows)
            for user_id in costs:
                costs[user_id] += full_day_costs.get(user_id, 0.0)
        return build_account_user_ranking(users, costs)

    async def fetch_user_breakdown(
        self,
        *,
        start_date: date,
        end_date: date,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch inclusive Shanghai-date user totals, optionally for one account."""
        params = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "timezone": "Asia/Shanghai",
            "limit": str(SUB2API_USER_BREAKDOWN_LIMIT),
            "sort_by": "actual_cost",
        }
        if account_id is not None:
            params["account_id"] = str(account_id)
        query = urlencode(params)
        payload = await self._get_json(f"{self.base_url}/api/v1/admin/dashboard/user-breakdown?{query}")
        data = ensure_dict(payload).get("data", {})
        users = data.get("users", []) if isinstance(data, dict) else []
        return [user for user in users if isinstance(user, dict)] if isinstance(users, list) else []

    async def fetch_hourly_user_costs(
        self,
        *,
        date_value: date,
        start_hour: datetime,
        end_time: datetime,
        account_id: int | None = None,
    ) -> dict[int, float]:
        """Sum selected hourly user buckets for all accounts or one account."""
        trend = await self._fetch_user_cost_trend(
            start_date=date_value,
            end_date=date_value,
            granularity="hour",
            account_id=account_id,
        )
        costs: dict[int, float] = {}
        for row in trend:
            bucket = parse_sub2api_hour_bucket(row.get("date"))
            if bucket is None or bucket < start_hour or bucket > end_time:
                continue
            user_id = optional_int(row.get("user_id"))
            if user_id is None or user_id <= 0:
                continue
            costs[user_id] = costs.get(user_id, 0.0) + (optional_float(row.get("actual_cost")) or 0.0)
        return costs

    async def fetch_daily_user_costs(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> dict[date, dict[int, float]]:
        """Index per-user daily costs so several fixed windows can share one response."""
        trend = await self._fetch_user_cost_trend(
            start_date=start_date,
            end_date=end_date,
            granularity="day",
        )
        costs_by_date: dict[date, dict[int, float]] = {}
        for row in trend:
            bucket = parse_sub2api_hour_bucket(row.get("date"))
            if bucket is None or bucket.date() < start_date or bucket.date() > end_date:
                continue
            user_id = optional_int(row.get("user_id"))
            if user_id is None or user_id <= 0:
                continue
            daily_costs = costs_by_date.setdefault(bucket.date(), {})
            daily_costs[user_id] = daily_costs.get(user_id, 0.0) + (optional_float(row.get("actual_cost")) or 0.0)
        return costs_by_date

    async def _fetch_user_cost_trend(
        self,
        *,
        start_date: date,
        end_date: date,
        granularity: str,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch raw user cost buckets with only the requested trend payload enabled."""
        params = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "granularity": granularity,
            "include_stats": "false",
            "include_trend": "false",
            "include_model_stats": "false",
            "include_group_stats": "false",
            "include_users_trend": "true",
        }
        if account_id is not None:
            params["account_id"] = str(account_id)
        query = urlencode(params)
        payload = await self._get_json(f"{self.base_url}/api/v1/admin/dashboard/snapshot-v2?{query}")
        data = ensure_dict(payload).get("data", {})
        trend = data.get("users_trend", []) if isinstance(data, dict) else []
        return [row for row in trend if isinstance(row, dict)] if isinstance(trend, list) else []

    async def _list_paginated(self, resource: str, *, sort_by: str) -> list[dict[str, Any]]:
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            query = urlencode(
                {
                    "page": str(page),
                    "page_size": str(SUB2API_LIST_PAGE_SIZE),
                    "sort_by": sort_by,
                    "sort_order": "asc",
                }
            )
            payload = await self._get_json(f"{self.base_url}/api/v1/admin/{resource}?{query}")
            data = ensure_dict(payload).get("data", {})
            page_items = data.get("items", []) if isinstance(data, dict) else []
            if not isinstance(page_items, list):
                break
            items.extend(item for item in page_items if isinstance(item, dict))
            total = optional_int(data.get("total")) if isinstance(data, dict) else None
            if (
                not page_items
                or (total is not None and len(items) >= total)
                or (total is None and len(page_items) < SUB2API_LIST_PAGE_SIZE)
            ):
                break
            page += 1
        return items

    async def fetch_usage(self, account_id: int, *, force_refresh: bool = False) -> dict[str, Any]:
        params = {"source": "active" if force_refresh else "passive"}
        if force_refresh:
            params["force"] = "true"
        query = urlencode(params)
        payload = await self._get_json(f"{self.base_url}/api/v1/admin/accounts/{account_id}/usage?{query}")
        data = ensure_dict(payload).get("data", {})
        return data if isinstance(data, dict) else {}

    async def _get_json(self, url: str) -> Any:
        headers = build_sub2api_headers(self.admin_api_key)
        if self.http_client is not None:
            return await self.http_client.get_json(url, headers=headers, timeout=self.timeout_seconds)
        return await get_json(url, headers=headers, timeout=self.timeout_seconds)


def build_sub2api_headers(admin_api_key: str) -> dict[str, str]:
    return {
        "x-api-key": admin_api_key,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": SUB2API_USER_AGENT,
    }


async def get_json(url: str, *, headers: dict[str, str], timeout: float) -> Any:
    def request_json() -> Any:
        request = Request(url, headers=headers, method="GET")
        # MaiBot runs in WSL and inherits the workspace HTTP(S) proxy. Using the
        # environment-aware opener avoids depending on a transient WSL DNS route.
        opener = build_opener()
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(format_sub2api_http_error(exc.code, detail)) from exc
        return json.loads(raw)

    return await asyncio.to_thread(request_json)


def format_sub2api_http_error(status_code: int, detail: str) -> str:
    if status_code == 403 and ("error-1010" in detail or "browser's signature" in detail or "Error 1010" in detail):
        return (
            "Sub2API 请求被 Cloudflare 拦截：HTTP 403 Error 1010，"
            "这是浏览器指纹/WAF 拦截，尚未进入 Admin API Key 鉴权。"
            "请在 Cloudflare 对 Sub2API Admin API 路径放行机器人服务器出口 IP，"
            "或把 sub2api_base_url 改为不经过 Cloudflare 的内网/源站地址。"
        )
    return f"Sub2API 请求失败：HTTP {status_code} {detail}"


def build_account_usage(account: dict[str, Any], usage: dict[str, Any]) -> Sub2APIAccountUsage:
    account_payload = ensure_dict(account.get("account") or account)
    platform = str(account_payload.get("platform") or account.get("platform") or "")
    account_type = str(
        account_payload.get("type")
        or account_payload.get("account_type")
        or account.get("type")
        or account.get("account_type")
        or ""
    )
    five_hour = parse_usage_window(usage.get("five_hour"))
    seven_day = parse_usage_window(usage.get("seven_day"))
    return Sub2APIAccountUsage(
        account_id=int(account_payload.get("id") or account.get("id") or 0),
        name=str(account_payload.get("name") or account.get("name") or ""),
        platform=platform,
        account_type=account_type,
        status=str(account_payload.get("status") or account.get("status") or ""),
        current_concurrency=optional_int(account.get("current_concurrency")),
        last_used_at=str(account_payload.get("last_used_at") or account.get("last_used_at") or ""),
        source=str(usage.get("source") or ""),
        updated_at=str(usage.get("updated_at") or ""),
        five_hour=five_hour,
        seven_day=seven_day,
        quota_windows=build_account_quota_windows(
            platform,
            account_type,
            usage,
            five_hour,
            seven_day,
        ),
        error=str(usage.get("error") or ""),
    )


def build_account_quota_windows(
    platform: str,
    account_type: str,
    usage: dict[str, Any],
    five_hour: Sub2APIUsageWindow | None,
    seven_day: Sub2APIUsageWindow | None,
) -> tuple[Sub2APIQuotaWindow, ...]:
    """Normalize only quota windows that the account's platform actually owns."""
    normalized_platform = platform.strip().casefold()
    normalized_type = account_type.strip().casefold()
    if normalized_platform == "openai":
        if normalized_type != "oauth":
            return ()
        return (
            Sub2APIQuotaWindow(label="5h", usage=five_hour),
            Sub2APIQuotaWindow(label="7d", usage=seven_day),
        )
    if normalized_platform == "antigravity":
        return build_antigravity_quota_windows(usage.get("antigravity_quota"))
    if normalized_platform == "grok":
        return build_grok_quota_windows(usage)
    if normalized_platform == "anthropic":
        return parse_present_quota_windows(
            usage,
            (
                ("5h", "five_hour"),
                ("7d", "seven_day"),
                ("7d S", "seven_day_sonnet"),
                ("7d F", "seven_day_fable"),
            ),
        )
    if normalized_platform == "gemini":
        shared_daily = parse_usage_window(usage.get("gemini_shared_daily"))
        if shared_daily is not None:
            return (Sub2APIQuotaWindow(label="1d", usage=shared_daily),)
        return parse_present_quota_windows(
            usage,
            (
                ("Pro", "gemini_pro_daily"),
                ("Flash", "gemini_flash_daily"),
            ),
        )
    return ()


def parse_present_quota_windows(
    usage: dict[str, Any],
    fields: tuple[tuple[str, str], ...],
) -> tuple[Sub2APIQuotaWindow, ...]:
    windows: list[Sub2APIQuotaWindow] = []
    for label, field_name in fields:
        window = parse_usage_window(usage.get(field_name))
        if window is not None:
            windows.append(Sub2APIQuotaWindow(label=label, usage=window))
    return tuple(windows)


def build_antigravity_quota_windows(value: object) -> tuple[Sub2APIQuotaWindow, ...]:
    """Aggregate Antigravity model quotas exactly as the Sub2API account page does."""
    if not isinstance(value, dict):
        return ()
    windows: list[Sub2APIQuotaWindow] = []
    for label, model_names in SUB2API_ANTIGRAVITY_QUOTA_GROUPS:
        model_windows = [
            parse_usage_window(value.get(model_name))
            for model_name in model_names
            if isinstance(value.get(model_name), dict)
        ]
        present_windows = [window for window in model_windows if window is not None]
        if not present_windows:
            continue
        utilizations = [window.utilization for window in present_windows if window.utilization is not None]
        reset_times = [window.resets_at for window in present_windows if window.resets_at]
        remaining_values = [
            window.remaining_seconds
            for window in present_windows
            if window.remaining_seconds is not None
        ]
        windows.append(
            Sub2APIQuotaWindow(
                label=label,
                usage=Sub2APIUsageWindow(
                    utilization=max(utilizations) if utilizations else None,
                    resets_at=min(reset_times) if reset_times else "",
                    remaining_seconds=min(remaining_values) if remaining_values else None,
                ),
            )
        )
    return tuple(windows)


def build_grok_quota_windows(usage: dict[str, Any]) -> tuple[Sub2APIQuotaWindow, ...]:
    """Build Grok's actual 24h free-tier or billing-period quota windows."""
    local_24h = parse_window_stats(usage.get("grok_local_usage_24h"))
    token_limit = optional_float(usage.get("grok_free_token_limit"))
    if local_24h is not None and token_limit is not None and token_limit > 0:
        utilization = min(100.0, max(0.0, local_24h.tokens / token_limit * 100.0))
        return (
            Sub2APIQuotaWindow(
                label="24h",
                usage=Sub2APIUsageWindow(
                    utilization=utilization,
                    window_stats=local_24h,
                ),
            ),
        )

    billing = ensure_dict(usage.get("grok_billing"))
    if not billing:
        return ()
    windows: list[Sub2APIQuotaWindow] = []
    period_type = str(billing.get("period_type") or "").strip().casefold()
    weekly_utilization = optional_float(billing.get("usage_percent"))
    if period_type == "weekly" and weekly_utilization is not None:
        weekly_stats = parse_window_stats(usage.get("grok_local_usage_7d"))
        if weekly_stats is None:
            seven_day = parse_usage_window(usage.get("seven_day"))
            weekly_stats = seven_day.window_stats if seven_day is not None else None
        windows.append(
            Sub2APIQuotaWindow(
                label="7d",
                usage=Sub2APIUsageWindow(
                    utilization=min(100.0, max(0.0, weekly_utilization)),
                    resets_at=str(billing.get("period_end") or ""),
                    window_stats=weekly_stats,
                ),
            )
        )

    monthly_utilization = optional_float(billing.get("used_percent"))
    monthly_limit_cents = optional_float(billing.get("monthly_limit_cents"))
    if monthly_utilization is None and monthly_limit_cents is not None and monthly_limit_cents > 0:
        used_cents = optional_float(billing.get("used_cents")) or 0.0
        monthly_utilization = used_cents / monthly_limit_cents * 100.0
    if monthly_utilization is not None and not (
        period_type == "weekly" and monthly_limit_cents is None
    ):
        monthly_stats = parse_window_stats(usage.get("grok_local_usage_monthly"))
        if monthly_stats is None:
            thirty_day = parse_usage_window(usage.get("thirty_day"))
            monthly_stats = thirty_day.window_stats if thirty_day is not None else None
        windows.append(
            Sub2APIQuotaWindow(
                label="30d",
                usage=Sub2APIUsageWindow(
                    utilization=min(100.0, max(0.0, monthly_utilization)),
                    resets_at=str(
                        billing.get("billing_period_end")
                        or billing.get("period_end")
                        or ""
                    ),
                    window_stats=monthly_stats,
                ),
            )
        )
    return tuple(windows)


def parse_usage_window(value: object) -> Sub2APIUsageWindow | None:
    if not isinstance(value, dict):
        return None
    return Sub2APIUsageWindow(
        utilization=optional_float(value.get("utilization")),
        resets_at=str(value.get("resets_at") or value.get("reset_time") or ""),
        remaining_seconds=optional_int(value.get("remaining_seconds")),
        window_stats=parse_window_stats(value.get("window_stats")),
    )


def parse_window_stats(value: object) -> Sub2APIWindowStats | None:
    if not isinstance(value, dict):
        return None
    return Sub2APIWindowStats(
        requests=optional_int(value.get("requests")) or 0,
        tokens=optional_int(value.get("tokens")) or 0,
        cost=optional_float(value.get("cost")) or 0.0,
        user_cost=optional_float(value.get("user_cost")),
    )


def normalize_sub2api_datetime(value: datetime) -> datetime:
    """Interpret naive datetimes as UTC and convert all boundaries to Shanghai time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SUB2API_TIMEZONE)


def floor_to_hour(value: datetime) -> datetime:
    """Return the whole-hour boundary at or before a window start."""
    return value.replace(minute=0, second=0, microsecond=0)


def current_day_window_start(now: datetime) -> datetime:
    """Return the latest Asia/Shanghai 08:00 that has already arrived."""
    local_now = normalize_sub2api_datetime(now)
    today_boundary = local_now.replace(hour=8, minute=0, second=0, microsecond=0)
    if local_now >= today_boundary:
        return today_boundary
    return today_boundary - timedelta(days=1)


def current_week_window_start(now: datetime) -> datetime:
    """Return the latest Monday 08:00 Asia/Shanghai that has already arrived."""
    day_started_at = current_day_window_start(now)
    return day_started_at - timedelta(days=day_started_at.weekday())


def thirty_day_window_start(now: datetime) -> datetime:
    """Return the current-day 08:00 boundary minus 30 calendar days."""
    return current_day_window_start(now) - timedelta(days=30)


def build_rolling_user_costs(
    *,
    window_started_at: datetime,
    end_time: datetime,
    hourly_costs: dict[int, float],
    daily_costs_by_date: dict[date, dict[int, float]],
) -> dict[int, float]:
    """Combine a partial start day with shared complete-date user totals."""
    full_days_start = window_started_at.date() + timedelta(days=1)
    costs = dict(hourly_costs)
    for bucket_date, daily_costs in daily_costs_by_date.items():
        if bucket_date < full_days_start or bucket_date > end_time.date():
            continue
        for user_id, cost in daily_costs.items():
            costs[user_id] = costs.get(user_id, 0.0) + cost
    return costs


def build_user_usage_ranking(
    users: list[dict[str, Any]],
    current_day_costs: dict[int, float],
    current_week_costs: dict[int, float],
    thirty_day_costs: dict[int, float],
) -> list[Sub2APIUserUsage]:
    """Build the visible ranking from pre-aggregated fixed-window costs."""
    results: list[Sub2APIUserUsage] = []
    seen_user_ids: set[int] = set()
    for user in users:
        user_id = optional_int(user.get("id"))
        if user_id is None or user_id <= 0 or user_id in seen_user_ids:
            continue
        seen_user_ids.add(user_id)
        current_day_actual_cost = current_day_costs.get(user_id, 0.0)
        current_week_actual_cost = current_week_costs.get(user_id, 0.0)
        thirty_day_actual_cost = thirty_day_costs.get(user_id, 0.0)
        if not any(
            (
                current_day_actual_cost,
                current_week_actual_cost,
                thirty_day_actual_cost,
            )
        ):
            continue
        results.append(
            Sub2APIUserUsage(
                user_id=user_id,
                username=str(user.get("username") or "").strip(),
                email=str(user.get("email") or "").strip(),
                current_day_actual_cost=current_day_actual_cost,
                current_week_actual_cost=current_week_actual_cost,
                thirty_day_actual_cost=thirty_day_actual_cost,
            )
        )
    return sorted(
        results,
        key=lambda usage: (
            -usage.current_day_actual_cost,
            -usage.current_week_actual_cost,
            -usage.thirty_day_actual_cost,
            usage.user_id,
        ),
    )


def build_account_user_ranking(
    users: tuple[Sub2APIUserUsage, ...],
    costs: dict[int, float],
) -> tuple[Sub2APIAccountUserUsage, ...]:
    """Build one account's complete nonzero ranking from known users and costs."""
    ranking = (
        Sub2APIAccountUserUsage(
            user_id=usage.user_id,
            username=usage.username,
            email=usage.email,
            actual_cost=costs.get(usage.user_id, 0.0),
        )
        for usage in users
        if costs.get(usage.user_id, 0.0) > 0.0
    )
    return tuple(sorted(ranking, key=lambda usage: (-usage.actual_cost, usage.user_id)))


def find_account_seven_day_ranking(
    rankings: tuple[Sub2APIAccountSevenDayRanking, ...],
    account_id: int,
) -> Sub2APIAccountSevenDayRanking | None:
    """Find cached cycle data by stable account identity rather than display name."""
    return next((ranking for ranking in rankings if ranking.account_id == account_id), None)


def retain_failed_account_ranking(
    account_id: int,
    previous: Sub2APIAccountSevenDayRanking | None,
    error: str,
) -> Sub2APIAccountSevenDayRanking:
    """Attach an error while retaining only the same account's complete ranking."""
    cached = previous if previous is not None and previous.account_id == account_id else None
    return Sub2APIAccountSevenDayRanking(
        account_id=account_id,
        users=cached.users if cached is not None else (),
        refreshed_at=cached.refreshed_at if cached is not None else None,
        error=error,
    )


def build_user_actual_costs(rows: list[dict[str, Any]]) -> dict[int, float]:
    costs: dict[int, float] = {}
    for row in rows:
        user_id = optional_int(row.get("user_id"))
        if user_id is None or user_id <= 0:
            continue
        costs[user_id] = optional_float(row.get("actual_cost")) or 0.0
    return costs


def format_sub2api_usage_response(snapshot: Sub2APIUsageSnapshot) -> str:
    """Format the cached per-account and global rankings when image rendering fails."""
    lines = [f"Sub2API 用量：账号 {len(snapshot.accounts)} 个，用户 {len(snapshot.users)} 个"]
    if snapshot.accounts_refreshed_at:
        lines.append(f"账号刷新：{format_datetime(snapshot.accounts_refreshed_at)}")
    if snapshot.accounts_error:
        lines.append(f"账号刷新失败：{snapshot.accounts_error}")
    for index, account in enumerate(snapshot.accounts, start=1):
        account_name = account.name or str(account.account_id)
        lines.extend(format_sub2api_usage_message(account, title=f"{index}. {account_name}").splitlines())
        if supports_sub2api_account_seven_day_ranking(account):
            ranking = find_account_seven_day_ranking(
                snapshot.account_seven_day_rankings,
                account.account_id,
            )
            lines.append(f"{account_name} 当前账号7d周期消费榜：")
            if ranking is None or ranking.refreshed_at is None:
                lines.append("刷新：暂无成功数据")
            else:
                lines.append(f"刷新：{format_datetime(ranking.refreshed_at)}")
            if ranking is not None and ranking.error:
                if ranking.refreshed_at is None:
                    lines.append(f"刷新失败：{ranking.error}")
                else:
                    lines.append(f"刷新失败，已保留上次成功缓存：{ranking.error}")
            ranking_users = ranking.users if ranking is not None else ()
            if not ranking_users:
                lines.append("当前账号7d周期内暂无消费用户。")
            for user_index, usage in enumerate(ranking_users, start=1):
                lines.append(
                    f"{user_index}. {format_sub2api_user_name(usage)}："
                    f"${usage.actual_cost:.2f}"
                )
    lines.append("全账号消费榜（当日 / 本周 / 30d，Asia/Shanghai 08:00 边界）：")
    if snapshot.users_refreshed_at:
        lines.append(f"用户刷新：{format_datetime(snapshot.users_refreshed_at)}")
    if snapshot.users_error:
        lines.append(f"用户刷新失败：{snapshot.users_error}")
    if not snapshot.users:
        lines.append("当前统计周期内暂无消费用户。")
    for index, usage in enumerate(snapshot.users, start=1):
        lines.append(
            f"{index}. {format_sub2api_user_name(usage)}："
            f"当日 ${usage.current_day_actual_cost:.2f}，"
            f"本周 ${usage.current_week_actual_cost:.2f}，"
            f"30d ${usage.thirty_day_actual_cost:.2f}"
        )
    return "\n".join(lines)


def format_sub2api_user_name(usage: Sub2APIUserUsage | Sub2APIAccountUserUsage) -> str:
    """Resolve a safe display label shared by global and per-account ranking rows."""
    username = usage.username.strip()
    return username or mask_sub2api_email(usage.email) or f"用户 {usage.user_id}"


def mask_sub2api_email(email: str) -> str:
    text = str(email or "").strip()
    if not text:
        return ""
    local, separator, domain = text.partition("@")
    if not separator or not local:
        return "*" * len(text)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        mask_count = (len(local) + 2) // 3
        visible_count = len(local) - mask_count
        prefix_count = (visible_count + 1) // 2
        suffix_count = visible_count - prefix_count
        suffix = local[-suffix_count:] if suffix_count else ""
        masked_local = f"{local[:prefix_count]}{'*' * mask_count}{suffix}"
    return f"{masked_local}@{domain}"


def format_sub2api_usage_alert_message(alert: Sub2APIUsageAlert) -> str:
    usage = alert.usage
    lines = [f"Sub2API 5h 用量提醒：{usage.name or usage.account_id} 已达到 {alert.threshold}%"]
    lines.append(f"当前 5h：{format_usage_window(usage.five_hour)}")
    if usage.seven_day is not None:
        lines.append(f"7d：{format_usage_window(usage.seven_day)}")
    if usage.updated_at:
        lines.append(f"更新时间：{format_time_text(usage.updated_at)}")
    return "\n".join(lines)


def format_sub2api_usage_message(usage: Sub2APIAccountUsage, *, title: str | None = None) -> str:
    lines = [title or f"Sub2API 用量：{usage.name or usage.account_id}"]
    meta = []
    if usage.platform:
        meta.append(usage.platform)
    if usage.account_type:
        meta.append(usage.account_type)
    if usage.status:
        meta.append(usage.status)
    if usage.current_concurrency is not None:
        meta.append(f"并发 {usage.current_concurrency}")
    if meta:
        lines.append(f"状态：{' / '.join(meta)}")
    for quota_window in usage.quota_windows:
        lines.append(f"{quota_window.label}：{format_usage_window(quota_window.usage)}")
    if usage.last_used_at:
        lines.append(f"最近使用：{format_time_text(usage.last_used_at)}")
    if usage.updated_at:
        lines.append(f"更新时间：{format_time_text(usage.updated_at)}")
    if usage.source:
        lines.append(f"来源：{usage.source}")
    if usage.error:
        lines.append(f"提示：{usage.error}")
    return "\n".join(lines)


def format_usage_window(window: Sub2APIUsageWindow | None) -> str:
    if window is None:
        return "无数据"
    parts = []
    if window.utilization is not None:
        parts.append(f"{window.utilization:.0f}%")
    if window.window_stats is not None:
        stats = window.window_stats
        if stats.tokens:
            parts.append(f"{format_compact_number(stats.tokens)} tokens")
        if stats.requests:
            parts.append(f"{stats.requests} 次")
        if stats.cost:
            parts.append(f"${stats.cost:.2f}")
    remaining = format_remaining_seconds(window.remaining_seconds)
    if remaining:
        parts.append(f"重置剩 {remaining}")
    elif window.resets_at:
        parts.append(f"重置 {format_time_text(window.resets_at)}")
    return "，".join(parts) if parts else "无数据"


def format_remaining_seconds(value: int | None) -> str:
    if value is None or value < 0:
        return ""
    minutes = value // 60
    hours, minute = divmod(minutes, 60)
    days, hour = divmod(hours, 24)
    if days > 0:
        return f"{days}天{hour}小时"
    if hour > 0:
        return f"{hour}小时{minute}分"
    return f"{minute}分"


def format_time_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = parse_datetime(text)
    if parsed is None:
        return text
    local = parsed.astimezone(SUB2API_TIMEZONE)
    return local.strftime("%Y-%m-%d %H:%M")


def format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SUB2API_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_sub2api_hour_bucket(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SUB2API_TIMEZONE)
    return parsed.astimezone(SUB2API_TIMEZONE)


def format_compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def ensure_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
