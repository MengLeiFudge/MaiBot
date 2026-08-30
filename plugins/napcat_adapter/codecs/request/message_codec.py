"""NapCat OneBot request event codec."""

from __future__ import annotations

from hashlib import sha1
from typing import Any, Dict, Optional
from uuid import uuid4

import time

from ...services import NapCatQueryService
from ...types import NapCatPayload, NapCatPayloadDict
from ..notice.enricher import NapCatNoticeEntityResolver
from ..notice.helpers import build_payload_digest


class NapCatRequestCodec:
    """Convert OneBot request events into structured Host messages."""

    def __init__(self, query_service: NapCatQueryService) -> None:
        """Initialize the request codec.

        Args:
            query_service: QQ entity query service.
        """
        self._entity_resolver = NapCatNoticeEntityResolver(query_service)

    async def build_request_message_dict(self, payload: NapCatPayload) -> Optional[NapCatPayloadDict]:
        """Build a Host ``MessageDict`` for a OneBot ``request`` event.

        The visible text deliberately contains no request flag, comment, or other
        user-controlled request data. Business hooks consume the complete payload
        from ``additional_config`` and abort normal chat processing.

        Args:
            payload: Raw OneBot request payload.

        Returns:
            Optional[NapCatPayloadDict]: A structured notification, or ``None``
            when the required request type is absent.
        """
        request_type = str(payload.get("request_type") or "").strip()
        if not request_type:
            return None

        request_sub_type = str(payload.get("sub_type") or "").strip()
        self_id = str(payload.get("self_id") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        group_id = str(payload.get("group_id") or "").strip()

        user_info = await self._entity_resolver.build_user_info(group_id=group_id, user_id=user_id)
        group_info = await self._entity_resolver.build_group_info(group_id)
        request_text = "[NapCat request event]"

        additional_config: Dict[str, Any] = {
            "self_id": self_id,
            "napcat_request_type": request_type,
            "napcat_request_sub_type": request_sub_type,
            "napcat_request_payload": dict(payload),
        }
        if group_id:
            additional_config["platform_io_target_group_id"] = group_id
        elif user_id:
            additional_config["platform_io_target_user_id"] = user_id

        message_info: Dict[str, Any] = {"user_info": user_info, "additional_config": additional_config}
        if group_info is not None:
            message_info["group_info"] = group_info

        timestamp_seconds = payload.get("time")
        if not isinstance(timestamp_seconds, (int, float)):
            timestamp_seconds = time.time()

        return {
            "message_id": f"napcat-request-{uuid4().hex}",
            "timestamp": str(float(timestamp_seconds)),
            "platform": "qq",
            "message_info": message_info,
            "raw_message": [{"type": "text", "data": request_text}],
            "is_mentioned": False,
            "is_at": False,
            "is_emoji": False,
            "is_picture": False,
            "is_command": False,
            "is_notify": True,
            "session_id": "",
            "processed_plain_text": request_text,
            "display_message": request_text,
        }

    def build_request_dedupe_key(self, payload: NapCatPayload) -> Optional[str]:
        """Build a stable, non-sensitive dedupe key for a request event.

        Args:
            payload: Raw OneBot request payload.

        Returns:
            Optional[str]: A flag-derived key when a flag exists, otherwise a
            bounded digest of the complete payload. Missing request types return
            ``None``.
        """
        request_type = str(payload.get("request_type") or "").strip()
        if not request_type:
            return None

        request_sub_type = str(payload.get("sub_type") or "").strip()
        prefix = f"request:{request_type}"
        if request_sub_type:
            prefix = f"{prefix}:{request_sub_type}"

        flag = str(payload.get("flag") or "").strip()
        if flag:
            flag_digest = sha1(flag.encode("utf-8")).hexdigest()
            return f"{prefix}:flag:{flag_digest}"
        return f"{prefix}:payload:{build_payload_digest(payload)}"
