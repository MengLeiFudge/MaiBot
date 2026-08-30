from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def require_api_result(result: Any, label: str) -> Any:
    """Normalize MaiBot SDK API results and reject Host or OneBot failures.

    SDK 2.7 unwraps successful ``api.call`` responses before returning them to
    plugins. Failed Host calls remain ``{"success": false, ...}``; older test
    doubles may still return the full ``{"success": true, "result": ...}``
    envelope. OneBot adapter APIs return their raw status dictionary.
    """

    payload = result
    if isinstance(result, Mapping) and "success" in result:
        if not bool(result.get("success")):
            raise RuntimeError(f"{label}失败")
        if "result" in result:
            payload = result.get("result")

    if isinstance(payload, Mapping) and ("status" in payload or "retcode" in payload):
        status = str(payload.get("status") or "ok").lower()
        try:
            retcode = int(payload.get("retcode") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{label}返回无效 retcode") from exc
        if status not in {"ok", "success"} or retcode != 0:
            raise RuntimeError(f"{label}失败")
    return payload
